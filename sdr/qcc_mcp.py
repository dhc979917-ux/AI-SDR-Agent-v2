from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import threading
import time
from typing import Any

import httpx


class QccError(Exception):
    def __init__(self, message: str, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind


@dataclass
class QccSearchResult:
    status: str
    candidates: list[dict[str, Any]]
    query: str
    raw: dict[str, Any]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _expiry(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _sse_json(text: str) -> dict[str, Any]:
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    if not data:
        raise QccError("企查查返回空事件流", "protocol")
    try:
        return json.loads(data[-1])
    except ValueError as exc:
        raise QccError("企查查事件流不是合法 JSON", "protocol") from exc


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code in (401, 403):
        raise QccError("企查查鉴权失败，请检查 MCP token", "auth")
    if response.status_code == 429:
        raise QccError("企查查请求频率受限，请稍后重试", "rate_limit")
    if response.status_code >= 500:
        raise QccError("企查查服务暂时不可用", "upstream")
    if response.status_code == 202:
        return {}
    if response.status_code != 200:
        raise QccError(f"企查查请求失败（HTTP {response.status_code}）", "upstream")
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()
    if "text/event-stream" in content_type or text.startswith("event:") or "\ndata:" in text:
        return _sse_json(text)
    try:
        return response.json()
    except ValueError as exc:
        raise QccError("企查查返回不是合法 JSON", "protocol") from exc


class QccCache:
    _lock = threading.Lock()

    def __init__(self, path: Path, ttl_days: int = 30):
        self.path = Path(path)
        self.ttl_days = max(0, int(ttl_days))

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> Any | None:
        if self.ttl_days <= 0:
            return None
        with QccCache._lock:
            item = self._load().get(key)
            if not isinstance(item, dict):
                return None
            try:
                expires = datetime.fromisoformat(item["expires_at"])
            except (KeyError, TypeError, ValueError):
                return None
            if expires < datetime.now(timezone.utc):
                return None
            return item.get("value")

    def set(self, key: str, value: Any) -> None:
        if self.ttl_days <= 0:
            return
        with QccCache._lock:
            payload = self._load()
            payload[key] = {
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=self.ttl_days)).isoformat(timespec="seconds"),
                "value": value,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            os.replace(temp, self.path)


class QccRateLimiter:
    def __init__(self, min_interval: float = 0.5):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if not self.min_interval:
            return
        with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


GLOBAL_RATE_LIMITER = QccRateLimiter(0.5)


class QccMcpClient:
    def __init__(self, url: str, token: str, timeout: float = 8.0, cache: QccCache | None = None,
                 transport: httpx.BaseTransport | None = None, min_interval: float = 0.5):
        if not url or not token:
            raise QccError("未配置企查查 MCP 地址或 token", "config")
        self.url = url
        self.token = token
        self.timeout = float(timeout)
        self.cache = cache
        self.transport = transport
        self.rate_limiter = QccRateLimiter(min_interval) if min_interval == 0 else GLOBAL_RATE_LIMITER
        self.session_id: str | None = None
        self.initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: QccError | None = None
        for attempt in range(2):
            self.rate_limiter.wait()
            try:
                with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=3.0), follow_redirects=False,
                                  trust_env=False, transport=self.transport) as client:
                    response = client.post(self.url, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                last_error = QccError("企查查请求超时", "timeout")
                if attempt == 0:
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                last_error = QccError("企查查网络连接失败", "network")
                if attempt == 0:
                    continue
                raise last_error from exc
            if not self.session_id:
                self.session_id = response.headers.get("mcp-session-id")
            try:
                body = _response_json(response)
            except QccError as exc:
                if attempt == 0 and exc.kind in ("timeout", "network", "rate_limit", "upstream"):
                    last_error = exc
                    time.sleep(0.8)
                    continue
                raise
            if "error" in body:
                error = body["error"] or {}
                raise QccError(str(error.get("message") or "企查查 MCP 返回错误"), "protocol")
            return body
        raise last_error or QccError("企查查请求失败", "upstream")

    def initialize(self) -> None:
        if self.initialized:
            return
        body = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ai-sdr-agent", "version": "0.3.0"},
            },
        })
        if "result" not in body:
            raise QccError("企查查 MCP 初始化失败", "protocol")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        body = self._post({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        result = body.get("result") or {}
        if result.get("isError"):
            raise QccError("企查查工具返回错误", "upstream")
        content = result.get("content") or []
        text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
        if not text:
            raise QccError("企查查工具返回空内容", "protocol")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise QccError("企查查工具返回不是合法 JSON", "protocol") from exc
        if not isinstance(parsed, dict):
            raise QccError("企查查工具返回格式不正确", "protocol")
        return parsed

    @staticmethod
    def _candidate(item: dict[str, Any], query: str) -> dict[str, Any]:
        name = str(item.get("企业名称") or "").strip()
        credit = str(item.get("统一社会信用代码") or "").strip()
        short = str(item.get("企业简称") or "").strip()
        return {
            "id": credit or name,
            "credit_code": credit,
            "name": name,
            "brand": short,
            "aliases": [short] if short else [],
            "role": "unknown",
            "role_hint": "品牌主体候选" if short and short in query else "主体待确认",
            "area": "",
            "industry": "",
            "registered_area": "",
            "registered_address": "",
            "company_type": "",
            "legal_representative": "、".join(item.get("法定代表人名称") or []) if isinstance(item.get("法定代表人名称"), list) else str(item.get("法定代表人名称") or ""),
            "established_at": str(item.get("成立日期") or ""),
            "status": str(item.get("状态") or ""),
            "source_url": "",
            "source_title": "企查查 MCP",
            "source_type": "qcc_mcp",
            "observed_at": _today(),
            "valid_until": _expiry(30),
            "data_mode": "real",
            "verified_registry": True,
            "relation_supported": False,
            "provider_position": 0,
        }

    def search_company(self, query: str) -> QccSearchResult:
        query = query.strip()
        cache_key = "search:" + query
        if self.cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict):
                candidates = cached.get("candidates", [])
                for index, candidate in enumerate(candidates):
                    candidate.setdefault("provider_position", index + 1)
                return QccSearchResult(cached.get("status", "none"), candidates, query, cached.get("raw", {}))
        payload = self.call_tool("get_company_by_query", {"searchKey": query})
        items = payload.get("企业信息") or []
        if not isinstance(items, list):
            items = []
        candidates = [self._candidate(item, query) for item in items if isinstance(item, dict) and item.get("企业名称")]
        for index, candidate in enumerate(candidates):
            candidate["provider_position"] = index + 1
        match = str(payload.get("匹配结果") or "")
        if "唯一" in match and len(candidates) == 1:
            status = "unique"
        elif candidates:
            status = "multiple"
        else:
            status = "none"
        result = {"status": status, "candidates": candidates[:5], "raw": payload}
        if self.cache:
            self.cache.set(cache_key, result)
        return QccSearchResult(status, candidates[:5], query, payload)

    def company_detail(self, credit_code: str) -> dict[str, Any]:
        credit_code = credit_code.strip()
        cache_key = "detail:" + credit_code
        if self.cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict):
                return cached
        payload = self.call_tool("get_company_registration_info", {"searchKey": credit_code})
        if self.cache:
            self.cache.set(cache_key, payload)
        return payload
