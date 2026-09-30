#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/.tools/browser-use"
command -v uv >/dev/null || { echo '需要先安装 uv。' >&2; exit 1; }
mkdir -p "$RUNTIME"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.tools/uv-cache}"
uv venv --python 3.11 --allow-existing "$RUNTIME/.venv"
uv pip sync --python "$RUNTIME/.venv/bin/python" "$ROOT/scripts/browser_use/requirements.lock"
ANONYMIZED_TELEMETRY=false BROWSER_USE_CLOUD_SYNC=false "$RUNTIME/.venv/bin/python" -c \
  'from importlib.metadata import version; from browser_use import Browser; print("browser-use " + version("browser-use") + " 已安装（本地 Chrome，无云端或模型调用）")'
