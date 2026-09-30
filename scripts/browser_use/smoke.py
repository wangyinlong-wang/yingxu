"""Exercise real browser-use Actor/CDP against a localhost fixture; never a platform post.

Run with .tools/browser-use/.venv/bin/python scripts/browser_use/smoke.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.update(ANONYMIZED_TELEMETRY="false", BROWSER_USE_CLOUD_SYNC="false",
                  BROWSER_USE_LOGGING_LEVEL="error", CDP_LOGGING_LEVEL="ERROR", NO_PROXY="*")
for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(variable, None)

from app.integrations import browser_runtime as runtime


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = (ROOT / "backend/tests/fixtures/browser_creator.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


async def smoke(port, directory):
    from browser_use import Browser

    browser = Browser(executable_path=os.environ.get("PUBLISHER_CHROME_PATH",
                      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      user_data_dir=str(directory / "profile"), headless=True, use_cloud=False,
                      enable_default_extensions=False, auto_download_pdfs=False, permissions=[])
    try:
        await browser.start()
        page = await browser.new_page(f"http://127.0.0.1:{port}/")
        await runtime.fill(page, runtime.PLATFORMS["douyin"]["title"], "测试标题")
        await runtime.fill(page, runtime.PLATFORMS["douyin"]["description"], "只在本地测试，不发布 #测试")
        for suffix, selector in ((".mp4", runtime.VIDEO_INPUT), (".png", runtime.COVER_INPUT)):
            path = directory / ("fixture" + suffix)
            data = b"LOCAL ACTOR FILE SELECTION TEST (not encoded media)" + suffix.encode()
            path.write_bytes(data)
            await runtime.upload(page, selector, path)
            assert await runtime.file_hash(page, suffix) == hashlib.sha256(data).hexdigest()
        # Xiaohongshu advertises extensions instead of a video MIME type.
        await page.evaluate('''() => {
            document.querySelector('input[accept*="video"]').accept='.mp4,.mov,.flv';
        }''')
        await runtime.upload(page, runtime.VIDEO_INPUT, directory / "fixture.mp4")
        assert await runtime.file_hash(page, ".mp4") == hashlib.sha256((directory / "fixture.mp4").read_bytes()).hexdigest()
        assert await runtime.declaration(page)
        state = await runtime.snapshot(page)
        dy = runtime.matching_work("douyin", state, "测试标题", "123456789")
        metrics = runtime.parse_metrics(dy["pairs"])
        assert metrics["views"] == 12000 and metrics["likes"] == 0 and metrics["comments"] is None, metrics
        xhs = runtime.matching_work("xiaohongshu", state, "小红书测试", "1234567890abcdef")
        assert runtime.parse_metrics(xhs["pairs"])["views"] == 3200
        # Reproduce the actual creator-card nesting and unlabeled SVG metrics.
        icons = json.loads((ROOT / "backend/tests/fixtures/xhs_metric_icons.json").read_text())
        await page.evaluate('''(icons) => {
            const card=document.createElement('div'); card.className='note-card';
            card.innerHTML='<span class="note-card__title">图标指标测试</span>'
                +'<a href="https://www.xiaohongshu.com/explore/abcdef1234567890">查看</a>';
            for(const item of icons){
                const metric=document.createElement('div');metric.className='note-card__stat';
                const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
                for(const d of item.icon.split('|')){
                    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
                    path.setAttribute('d',d);svg.append(path);
                }
                metric.append(svg,document.createTextNode(item.value));card.append(metric);
            }
            document.body.append(card);
        }''', icons)
        icon_work = runtime.matching_work("xiaohongshu", await runtime.snapshot(page), "图标指标测试")
        assert all(runtime.parse_metrics(icon_work["pairs"])[key] == 0 for key in runtime.METRICS)
        # Exercise the full guarded submit with its button below the viewport.
        # This host override exists only in this isolated localhost smoke process.
        runtime.PLATFORMS["douyin"]["host"] = "127.0.0.1"
        verify_page = runtime.verify_page

        def verify_local_page(request, state):
            assert state["url"].startswith(f"http://127.0.0.1:{port}/")
            return verify_page(request, {**state, "url": state["url"].replace("http://", "https://", 1)})

        runtime.verify_page = verify_local_page
        await page.evaluate('''() => {
            const button=document.querySelector('button');button.style.marginTop='200vh';
            button.addEventListener('click',()=>{
                document.querySelector('a[href*="douyin.com/video"]').href='https://www.douyin.com/video/987654321';
                const alert=document.createElement('div');alert.setAttribute('role','alert');
                alert.textContent='发布成功';document.body.append(alert);
            });
            window.scrollTo(0,0);
        }''')
        video_hash = hashlib.sha256((directory / "fixture.mp4").read_bytes()).hexdigest()
        evidence = {"video_sha256": video_hash,
                    "cover_sha256": hashlib.sha256((directory / "fixture.png").read_bytes()).hexdigest()}
        submitted = await runtime.submit(page, {"platform": "douyin", "account_identifier": "fixture-account",
            "title": "测试标题", "description": "只在本地测试，不发布 #测试",
            "video_sha256": video_hash, "evidence": evidence})
        assert submitted["status"] == "PUBLISHED" and submitted["work_id"] == "987654321", submitted
        assert await page.evaluate('() => document.querySelector("#clicked").textContent') == "1"
        print(json.dumps({"ok": True, "engine": "browser-use", "target": "localhost fixture",
                          "checks": ["Chrome launch", "fill and readback", "file upload and SHA-256",
                                     "AI checkbox read", "douyin and xhs work matching", "metric zero/null/units",
                                     "single offscreen guarded submit"]}, ensure_ascii=False))
    finally:
        await browser.kill()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="yingxu-browser-smoke-") as temp:
            asyncio.run(smoke(server.server_port, Path(temp)))
    finally:
        server.shutdown()
        server.server_close()
