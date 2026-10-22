#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11 或更新版本"'
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
if ! .venv/bin/python -c 'import imageio_ffmpeg, cryptography' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python scripts/init_admin.py --if-empty
exec .venv/bin/python -m sdr "$@"
