"""Start the demo server on a platform-provided port with optional demo seeding."""
from pathlib import Path
import os
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from sdr.app import create_app
from sdr.auth import Auth
from sdr.config import Settings
from sdr.models import UserCreate
from sdr.store import Store
from scripts.seed_demo import seed


def ensure_admin(settings):
    store = Store(settings.db_path)
    with store.connection() as db:
        row = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            return row["id"]
    username = os.getenv("SDR_DEMO_ADMIN_USER", "admin").strip().lower()
    password = os.getenv("SDR_DEMO_ADMIN_PASSWORD", "")
    if not password:
        password = secrets.token_urlsafe(18)
        print(f"[demo] SDR_DEMO_ADMIN_PASSWORD 未设置，临时管理员密码：{password}", flush=True)
    name = os.getenv("SDR_DEMO_ADMIN_NAME", "Demo Admin")
    Auth(store, settings).create_user(UserCreate(username=username, name=name, password=password, role="admin"))
    print(f"[demo] 管理员账号：{username}", flush=True)
    return username


def main():
    settings = Settings()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.cookie_secure:
        raise SystemExit("对外监听必须设置 SDR_COOKIE_SECURE=true，并通过 HTTPS 访问")
    owner = ensure_admin(settings)
    if os.getenv("SDR_SEED_DEMO", "false").lower() == "true":
        count = seed(settings, owner)
        if count:
            print(f"[demo] 已生成 {count} 条演示分析", flush=True)
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port,
                proxy_headers=os.getenv("SDR_PROXY_HEADERS", "true").lower() == "true",
                forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
                access_log=os.getenv("SDR_ACCESS_LOG", "false").lower() == "true")


if __name__ == "__main__":
    main()
