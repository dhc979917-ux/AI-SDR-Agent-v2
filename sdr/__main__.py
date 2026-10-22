import argparse
import uvicorn
from .app import create_app
from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="酷家乐 · AI SDR 工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    settings = Settings()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.cookie_secure:
        parser.error("对外监听需配置 SDR_COOKIE_SECURE=true，并通过HTTPS反向代理访问；本地运行使用默认127.0.0.1。")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
