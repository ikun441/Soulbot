#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if ! command -v git >/dev/null 2>&1; then
  echo "提示：未安装 Git；可以启动，但更新项目时需要安装 Git。"
fi
if [[ -n "${SOULBOT_PYTHON:-}" ]]; then
  exec "$SOULBOT_PYTHON" scripts/bootstrap.py "$@"
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python scripts/bootstrap.py "$@"
fi
for candidate in python3.11 python3.12 python3.13 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))' 2>/dev/null; then
    exec "$candidate" scripts/bootstrap.py "$@"
  fi
done
echo "未找到 Python 3.11–3.13。请先安装 Python（见 README.md），或设置 SOULBOT_PYTHON 指向已有解释器。"
exit 1
