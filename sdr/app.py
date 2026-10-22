from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import hmac
import io
import json
import tempfile
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .auth import Auth, COOKIE, LoginError
from .audio import AudioService, AudioError
from .workspace import Workspace, Forbidden, require_analysis, require_customer, require_admin
from .queue import review_display, priority_key
from .engine import Engine
from .entities import source_valid
from .models import (AnalyzeRequest, ReviewRequest, BatchPreviewRequest, BatchCommitRequest, Lead,
                     LoginRequest, UserCreate, UserUpdate, PasswordChange, CustomerCreate, CustomerUpdate,
                     AssignmentRequest, TranscriptConfirm, AudioIdentityRequest, AudioCustomerConfirm)
from .review import ReviewService, ReviewError
from .scoring import TIERS, LABELS, DIMENSIONS
from .store import Store, Conflict, NotFound, dumps
from .model_settings import ModelSettingsService,ModelSettingsUpdate,ModelTestRequest,ModelConfigError


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.db_path)
    model_settings=ModelSettingsService(store,settings)
    settings.model_settings=model_settings
    engine = Engine(store, settings)
    reviews = ReviewService(store, settings, guarded=True)
    auth, workspace, audio = Auth(store, settings), Workspace(store), AudioService(store, settings, engine)
    samples = json.loads((ROOT / "data/samples.json").read_text())

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        audio.recover()
        yield
        engine.pool.shutdown(wait=False, cancel_futures=True)
        audio.pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="酷家乐 · AI SDR 工作台", version="0.4.0", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.settings = store, engine, settings
    app.state.auth, app.state.audio = auth, audio
    app.state.model_settings=model_settings

    def actor(request: Request):
        session = auth.session(request.cookies.get(COOKIE))
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
            raise HTTPException(403, '会话校验失败，请刷新后重试')
        request.state.identity = session
        return session['user']['id']

    def admin(user=Depends(actor)):
        with store.connection() as db:
            require_admin(db, user)
        return user

    def check_analysis(aid, user):
        with store.connection() as db:
            require_analysis(db, aid, user)

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Same-origin browser UI. Non-browser API clients may omit Origin.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "不接受跨站写入请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(NotFound)
    async def not_found(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ReviewError)
    async def review_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(AudioError)
    async def audio_error(_, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    @app.exception_handler(ModelConfigError)
    async def model_config_error(_,exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        if request.url.path.startswith('/api/model-settings'):
            return JSONResponse({'detail':'模型设置格式无效，请检查厂商、模型ID、API Key及配置版本'},status_code=422)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request,exc)

    @app.exception_handler(Forbidden)
    async def forbidden(_, exc):
        return JSONResponse({'detail': str(exc)}, status_code=403)

    @app.exception_handler(LoginError)
    async def login_error(_, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": "0.2.0"}

    @app.post('/api/auth/login')
    def login(body: LoginRequest, request: Request):
        token, session = auth.login(body.username, body.password, request.client.host if request.client else 'unknown')
        # Replace any existing browser session; passwords never enter client storage.
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse(session)
        response.set_cookie(COOKIE, token, max_age=settings.session_hours*3600, httponly=True,
                            secure=settings.cookie_secure, samesite='strict', path='/')
        return response

    @app.get('/api/auth/me')
    def me(request: Request, user=Depends(actor)):
        return request.state.identity

    @app.post('/api/auth/logout')
    def logout(request: Request, user=Depends(actor)):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'ok':True})
        response.delete_cookie(COOKIE, path='/', secure=settings.cookie_secure, httponly=True, samesite='strict')
        return response

    @app.post('/api/auth/password')
    def password(body: PasswordChange, user=Depends(actor)):
        auth.change_password(user, body.current_password, body.new_password)
        response = JSONResponse({'ok':True})
        response.delete_cookie(COOKIE, path='/')
        return response

    @app.get('/api/admin/users')
    def users(user=Depends(admin)):
        return auth.users(user)

    @app.post('/api/admin/users', status_code=201)
    def create_user(body: UserCreate, user=Depends(admin)):
        return auth.create_user(body, user)

    @app.patch('/api/admin/users/{uid}')
    def update_user(uid: str, body: UserUpdate, user=Depends(admin)):
        return auth.update_user(uid, body, user)

    @app.get('/api/customers')
    def customers(user=Depends(actor)):
        return workspace.customers(user)

    @app.post('/api/customers', status_code=201)
    def create_customer(body: CustomerCreate, user=Depends(actor)):
        return workspace.create(body, user)

    @app.patch('/api/customers/{cid}')
    def update_customer(cid: str, body: CustomerUpdate, user=Depends(actor)):
        return workspace.update_customer(cid, body, user)

    @app.delete('/api/customers/{cid}')
    def delete_customer(cid: str, expected_revision: int = Query(ge=0), user=Depends(actor)):
        return workspace.delete_customer(cid, expected_revision, user)

    @app.post('/api/admin/assignments')
    def assign(body: AssignmentRequest, user=Depends(admin)):
        return workspace.assign(body, user)

    @app.get('/api/customers/{cid}/events')
    def customer_events(cid: str, user=Depends(actor)):
        return workspace.events(user, cid)

    @app.get('/api/admin/events')
    def admin_events(user=Depends(admin)):
        return workspace.events(user)

    @app.get('/api/transcriptions')
    def transcriptions(user=Depends(actor)):
        return audio.listing(user)

    @app.post('/api/transcriptions', status_code=202)
    async def upload_audio(request: Request, filename: str = Query(min_length=1,max_length=180),
                           idempotency_key: str = Query(min_length=8,max_length=100),
                           customer_id: str | None = None, customer_mode: Literal['existing','ai','auto'] = 'existing',
                           auto_analyze: bool = False,
                           data_mode: Literal['real','synthetic','snapshot_demo'] = 'real', user=Depends(actor)):
        selected=settings.resolved(user)
        if not selected.asr_key and selected.asr_protocol!='funasr_local':
            raise HTTPException(422, '请先在「我的模型」中配置录音转写模型及API Key，或选择FunASR本地离线转写')
        if (customer_mode == 'existing') != bool(customer_id):
            raise HTTPException(422, '请选择已有客户，或选择AI识别/自动建档且不预设客户')
        if customer_mode == 'ai' and not selected.api_key:
            raise HTTPException(422, '尚未配置客户识别模型，请先选择已有客户')
        if auto_analyze and customer_mode == 'ai':
            raise HTTPException(422, '批量自动分析请选择已有客户或自动建档')
        if customer_mode == 'auto':
            auto_analyze = True
        if customer_id:
            with store.connection() as db:
                require_customer(db, customer_id, user)
        # Stream to a private temporary file instead of retaining a large upload in RAM.
        from starlette.concurrency import run_in_threadpool
        with tempfile.TemporaryFile(dir=audio.directory) as source:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.audio_max_bytes:
                    raise HTTPException(413, f'音频不能超过{settings.audio_max_bytes/1024/1024:g} MB')
                await run_in_threadpool(source.write, chunk)
            row, created = await run_in_threadpool(audio.create, source, filename, customer_id, data_mode, idempotency_key, user, customer_mode, auto_analyze)
        if created:
            audio.pool.submit(audio.run, row['id'])
        return {**row, 'created':created}

    @app.get('/api/transcriptions/{tid}')
    def get_transcription(tid: str, user=Depends(actor)):
        return audio.get(tid, user)

    @app.get('/api/transcriptions/{tid}/audio')
    def get_audio(tid: str, user=Depends(actor)):
        row = audio.get(tid, user)
        if not audio.path(tid).is_file():
            raise HTTPException(404, '录音文件不可用')
        return FileResponse(audio.path(tid), media_type=row['mime'])

    @app.post('/api/transcriptions/{tid}/confirm')
    def confirm_audio(tid: str, body: TranscriptConfirm, user=Depends(actor)):
        row = audio.confirm(tid, body, user)
        if row.get('customer_id') and row.get('role_status') == 'ready' and row.get('status') == 'confirmed':
            audio.auto_finish(tid, user, settings.resolved(user))
            row = audio.get(tid, user)
        return row

    @app.post('/api/transcriptions/{tid}/retry')
    def retry_audio(tid: str, user=Depends(actor)):
        selected=settings.resolved(user)
        if not selected.asr_key and selected.asr_protocol!='funasr_local':
            raise HTTPException(422, '尚未配置语音识别密钥，或选择FunASR本地离线转写')
        return audio.retry(tid, user)

    @app.post('/api/transcriptions/{tid}/auto-analyze')
    def auto_analyze_audio(tid: str, user=Depends(actor)):
        row = audio.get(tid, user)
        eligible = row.get('customer_id') and row.get('role_status') == 'ready' and row.get('status') in ('transcribed', 'confirmed')
        if not row.get('auto_analyze') and not eligible:
            raise HTTPException(422, '该录音尚未满足自动分析条件，请先确认客户和角色校对稿')
        audio.auto_finish(tid, user, settings.resolved(user))
        return audio.get(tid, user)

    @app.post('/api/transcriptions/{tid}/identify')
    def identify_audio_customer(tid: str, body: AudioIdentityRequest, user=Depends(actor)):
        return audio.identify(tid, body, user)

    @app.post('/api/transcriptions/{tid}/roles')
    def identify_audio_roles(tid: str, body: AudioIdentityRequest, user=Depends(actor)):
        return audio.roles(tid, body, user)

    @app.post('/api/transcriptions/{tid}/customer')
    def confirm_audio_customer(tid: str, body: AudioCustomerConfirm, user=Depends(actor)):
        row = audio.confirm_customer(tid, body, user)
        if row.get('customer_id') and row.get('role_status') == 'ready' and row.get('status') in ('transcribed', 'confirmed'):
            audio.auto_finish(tid, user, settings.resolved(user))
            row = audio.get(tid, user)
        return row

    @app.delete('/api/transcriptions/{tid}')
    def delete_audio(tid: str, user=Depends(actor)):
        return audio.delete(tid, user)

    @app.get("/api/config")
    def config(request: Request, user=Depends(actor)):
        return {**settings.resolved(user).public(), 'user':request.state.identity['user'], "actor": user, "tiers": TIERS, "labels": LABELS, "dimensions": DIMENSIONS}

    @app.get('/api/model-settings')
    def get_model_settings(user=Depends(actor)):
        return model_settings.public(user)

    @app.put('/api/model-settings')
    def save_model_settings(body:ModelSettingsUpdate,user=Depends(actor)):
        return model_settings.update(user,body)

    @app.post('/api/model-settings/test')
    def test_model_settings(body:ModelTestRequest,user=Depends(actor)):
        return model_settings.test(user,body)

    @app.get("/api/samples")
    def get_samples(user=Depends(actor)):
        return samples

    def summary(row):
        result = row["result"] or {}
        lead = result.get("lead") or {}
        return {"id": row["id"], 'customer_id':row['customer_id'], 'review_display':review_display(row), "title": row["request"]["title"], "mode": row["request"]["mode"],
                "data_mode": row["request"]["data_mode"], "status": row["status"], "phase": row["phase"],
                "created_at": row["created_at"], "revision": row["revision"], "intent": (result.get("intent") or {}).get("level"),
                "score": (result.get("intent") or {}).get("interval"), "company": lead.get("company_name") or (result.get("entity") or {}).get("mention"),
                "industry": lead.get("industry"), "review_status": result.get("review", {}).get("status"),
                "review_stage": result.get("review", {}).get("stage"), "delivery_status": result.get("delivery_status"),
                "review_reasons": result.get("review_reasons", []), "quality": result.get("quality", {}).get("overall"),
                "do_not_contact": result.get("extraction", {}).get("do_not_contact", False)}

    @app.get("/api/analyses")
    def analyses(user=Depends(actor)):
        return sorted([summary(row) for row in store.listing(None, user)], key=priority_key)

    @app.delete('/api/analyses/{aid}')
    def delete_analysis(aid: str, expected_revision: int = Query(ge=0), user=Depends(actor)):
        return store.delete(aid, expected_revision, user)

    @app.post('/api/analyses/{aid}/cancel')
    def cancel_analysis(aid: str, user=Depends(actor)):
        check_analysis(aid, user)
        row = store.cancel(aid, user)
        return {"id": row["id"], "status": row["status"], "phase": row["phase"]}

    @app.post("/api/analyses", status_code=202)
    def analyze(body: AnalyzeRequest, user=Depends(actor)):
        if body.mode == "llm" and not settings.resolved(user).api_key:
            raise HTTPException(422, "请先在「我的模型」配置文字分析模型，或使用离线规则模式。")
        if sum(r["status"] in ("queued", "running") for r in store.listing(500)) >= settings.max_pending:
            raise HTTPException(429, "任务较多，请稍后重试。")
        if not any(line.strip().startswith(("客户：", "客户:", "用户：", "顾客：")) for line in body.transcript.splitlines()):
            # Still accept uncertain roles for auditable manual handling.
            pass
        data = body.model_dump()
        fingerprint = hashlib.sha256(dumps({k: v for k, v in data.items() if k != "idempotency_key"}).encode()).hexdigest()
        record, created = store.create(data, fingerprint, user, authorize=True)
        if created:
            engine.submit(record["id"])
        return {"id": record["id"], "created": created, "status": record["status"]}

    @app.get("/api/analyses/{aid}")
    def get_analysis(aid: str, user=Depends(actor)):
        check_analysis(aid, user)
        record = store.get(aid)
        record.pop("idem", None)
        record["events"] = store.events(aid)
        record['review_display'] = review_display(record)
        return record

    @app.post("/api/reviews/{aid}")
    def review(aid: str, body: ReviewRequest, user=Depends(actor)):
        check_analysis(aid, user)
        return summary(reviews.apply(aid, body, user))

    @app.post("/api/reviews/batch/preview")
    def preview(body: BatchPreviewRequest, user=Depends(actor)):
        return reviews.preview(body.model_dump(), user)

    @app.post("/api/reviews/batch/commit")
    def commit(body: BatchCommitRequest, user=Depends(actor)):
        return {"items": [summary(r) for r in reviews.commit(body.preview_id, user)]}

    @app.get("/api/feedback")
    def feedback(user=Depends(actor)):
        return store.feedback(user)

    @app.post("/api/feedback/{fid}/publish")
    def publish(fid: str, user=Depends(admin)):
        return reviews.publish_feedback(fid, user)

    @app.post("/api/feedback/{fid}/revoke")
    def revoke(fid: str, user=Depends(admin)):
        return reviews.revoke_feedback(fid, user)

    @app.get("/api/metrics")
    def metrics(user=Depends(actor)):
        rows = store.listing(None, user)
        completed = [r for r in rows if r["status"] == "completed"]
        results = [r["result"] for r in completed]
        distribution = {label: sum(r["intent"]["level"] == label for r in results) for label in ("high", "medium", "low", None)}
        return {"total": len(rows), "completed": len(completed), "failed": sum(r["status"] in ("failed", "interrupted") for r in rows),
                "pending": sum(r.get("review", {}).get("status") in ("pending", "in_review", "deferred") for r in results),
                "high": distribution["high"], "ready": sum(r["delivery_status"] == "ready" for r in results),
                "distribution": {str(k): v for k, v in distribution.items()},
                "average_ms": round(sum(r.get("elapsed_ms", 0) for r in results) / len(results)) if results else 0,
                "tokens": sum(r.get("provider", {}).get("tokens", 0) for r in results),
                "feedback_count": len(store.feedback(user)), "data_note": "运行统计；不是准确率评测"}

    @app.get("/api/exports/{aid}")
    def export(aid: str, kind: str = "candidate", user=Depends(actor)):
        check_analysis(aid, user)
        row = store.get(aid)
        result = row["result"] or {}
        if kind not in ("candidate", "qualified"):
            raise HTTPException(422, "导出类型必须为 candidate 或 qualified")
        if kind == "qualified" and (row["request"]["data_mode"] != "real" or result.get("delivery_status") != "ready" or not result.get("production_eligible")):
            raise HTTPException(409, "该记录尚不满足真实 SQL 交付门槛；演示数据只能导出候选。")
        if kind == "qualified" and result.get("entity", {}).get("status") in ("verified", "confirmed") and not source_valid(result.get("entity", {}).get("selected") or {}, "real"):
            raise HTTPException(409, "企业核验来源已失效，请重新核验后导出正式 SQL。")
        lead = result.get("lead")
        if lead:
            Lead.model_validate(lead)
        payload = {**(lead or {}), "_meta": {"analysis_id": aid, "revision": row["revision"], "export_kind": kind,
                   'customer_id':row['customer_id'], 'transcription_id':row['transcription_id'],
                   'audio_source':row['request'].get('audio_source'),
                   "demo": row["request"]["data_mode"] != "real", "data_mode": row["request"]["data_mode"],
                   "lead_available": bool(lead), "review": result.get("review"), "delivery_status": result.get("delivery_status"),
                   "evidence": result.get("extraction", {}).get("fields", {}), "company_resolution": result.get("entity"),
                   "signals": result.get("extraction", {}).get("signals", {}), "validation_issues": result.get("validation_issues", []),
                   "do_not_contact": result.get("extraction", {}).get("do_not_contact", False),
                   "scorecard": result.get("intent"), "auto_roles": result.get("auto_roles"), "role_pending_count": result.get("role_pending_count"), "confidence_method": "heuristic_min_v1", "review_reasons": result.get("review_reasons"),
                   "versions": result.get("versions"), "provider": result.get("provider"), "error": row["error"]}}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{aid}.json"'})

    @app.get("/api/export.csv")
    def export_csv(user=Depends(actor)):
        file = io.StringIO()
        writer = csv.writer(file)
        keys = ["id", "title", "company", "intent", "review_status", "data_mode"]
        writer.writerow(keys)
        for row in store.listing(None, user):
            values = summary(row)
            def safe(v):
                value = "" if v is None else str(v)
                return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
            writer.writerow([safe(values[k]) for k in keys])
        return Response("\ufeff" + file.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sdr-candidates.csv"'})

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
