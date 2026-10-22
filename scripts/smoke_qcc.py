"""Read-only QCC MCP smoke test. Never prints credentials or full transcripts."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.config import Settings
from sdr.qcc_mcp import QccCache, QccError, QccMcpClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="木里木外")
    parser.add_argument("--credit", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    settings = Settings(entity_provider="qcc")
    url, token = settings.qcc_credentials()
    if not url or not token:
        print("QCC MCP 未配置：请设置 QCC_MCP_URL/QCC_MCP_TOKEN 或 var/qcc-mcp.json", file=sys.stderr)
        return 2
    cache = QccCache(settings.qcc_cache_path, settings.qcc_cache_days)
    client = QccMcpClient(url, token, timeout=settings.qcc_timeout, cache=cache)
    try:
        search = client.search_company(args.query)
        credit = args.credit or (search.candidates[0]["credit_code"] if search.candidates else "")
        detail = client.company_detail(credit) if credit else {}
    except QccError as exc:
        print(f"QCC MCP 失败：{exc} ({exc.kind})", file=sys.stderr)
        return 1
    payload = {
        "query": args.query,
        "status": search.status,
        "candidates": [
            {"name": item.get("name"), "credit_code": item.get("credit_code"),
             "status": item.get("status"), "legal_representative": item.get("legal_representative")}
            for item in search.candidates
        ],
        "detail": {key: detail.get(key) for key in ("企业名称", "统一社会信用代码", "登记状态", "所属地区", "国标行业", "注册地址")},
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"query={args.query} status={search.status} candidates={len(search.candidates)}")
        for item in search.candidates:
            print(f"- {item.get('name')} | {item.get('credit_code')} | {item.get('status')} | {item.get('legal_representative')}")
        if detail:
            print("detail:", payload["detail"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
