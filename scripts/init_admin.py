"""Create the first administrator; save a unique random credential locally, never print it."""
from pathlib import Path
import argparse
import os
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.auth import Auth
from sdr.config import Settings
from sdr.models import UserCreate
from sdr.store import Store


def main():
    parser = argparse.ArgumentParser(description='初始化首个管理员，已有账号时拒绝覆盖')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--name', default='管理员')
    parser.add_argument('--if-empty', action='store_true', help='已有账号时正常返回，不修改账号')
    args = parser.parse_args()
    settings = Settings()
    store = Store(settings.db_path)
    with store.connection() as db:
        if db.execute('SELECT count(*) FROM users').fetchone()[0]:
            if args.if_empty:
                return
            parser.error('已有账号；请登录管理员在账号管理页创建员工')
    password = secrets.token_urlsafe(18)
    user = UserCreate(username=args.username, name=args.name, password=password, role='admin')
    output = settings.db_path.parent / '初始管理员账号.txt'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        Auth(store, settings).create_user(user)
        with os.fdopen(fd, 'w') as f:
            f.write(f'酷家乐 AI SDR 工作台\n地址：http://127.0.0.1:8765\n账号：{user.username}\n密码：{password}\n\n请登录后在「账号与安全」中修改密码。此文件仅本机保存，不包含在交付包中。\n')
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    print(f'管理员已创建，初始凭据保存在：{output.resolve()}')


if __name__ == '__main__':
    main()
