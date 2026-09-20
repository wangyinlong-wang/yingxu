#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[FAIL] 未找到 Python 3；请先安装 Python 3.11～3.13。" >&2
  exit 1
fi

if [ "${1:-}" = "--check" ]; then
  exec python3 scripts/control.py env
fi

exec python3 scripts/control.py deploy --open
