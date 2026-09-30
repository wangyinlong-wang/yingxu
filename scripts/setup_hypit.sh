#!/usr/bin/env bash
# Optional runtime kept outside the Python/frontend dependency trees.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
python_bin="$project_root/backend/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo '请先按 README 完成后端安装。' >&2
  exit 1
fi
export npm_config_cache="${npm_config_cache:-$project_root/.tools/npm-cache}"
npm install --prefix "$project_root/.tools/hypit" --save-exact '@hypit/hypit@0.2.10'
runtime_root=$("$python_bin" - <<'PY'
import json, os, shutil, sys
from pathlib import Path
sys.path.insert(0, 'backend')
from app.config import AppConfig
root = AppConfig().data_dir.resolve() / 'hypit'
root.mkdir(parents=True, exist_ok=True)
profile = root / 'runtime.json'
# Preserve the user's profile on later installations.
if not profile.exists():
    chrome = os.environ.get('PUBLISHER_HYPIT_CHROME_PATH')
    if not chrome and sys.platform == 'darwin':
        candidate = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        chrome = str(candidate) if candidate.is_file() else None
    if not chrome:
        chrome = shutil.which('chromium') or shutil.which('google-chrome')
    browser = {'chromePath': chrome} if chrome else {'browserCacheDirectory': str(root / 'chrome')}
    profile.write_text(json.dumps({
        'format': 'hypit.runtime-local@1', 'dataRoot': str(root / 'runtime'),
        'endpoints': {
            'media.local': {'use': '@hypit/provider-media-local', 'config': {'defaultConcurrency': 1}},
            'hyperframes.local': {'use': '@hypit/provider-hyperframes-local', 'config': {
                'workers': 1, 'defaultConcurrency': 1, **browser}},
        },
    }, indent=2) + '\n')
print(root)
PY
)
export HYPIT_STATE_HOME="$runtime_root/host"
hypit_cli="$project_root/.tools/hypit/node_modules/.bin/hypit"
"$hypit_cli" packages install '@hyperframes/engine@0.7.101'
"$hypit_cli" packages install '@hyperframes/producer@0.7.101'
"$hypit_cli" programs prepare --runtime "$runtime_root/runtime.json" --endpoint hyperframes.local
"$hypit_cli" doctor --runtime "$runtime_root/runtime.json"
echo 'Hypit 已准备完成，可在设置中选择 Hypit 本地渲染。'
