"""Executed by the isolated browser-use Python. No LLM, cloud API or stored credentials.

Platform DOMs change: ambiguous controls or missing evidence stop for human handling.
Only this module knows the creator-page selectors; durable state belongs to the backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

PLATFORMS = {
    "douyin": {
        "host": "creator.douyin.com",
        "home": "https://creator.douyin.com/creator-micro/home",
        "upload": "https://creator.douyin.com/creator-micro/content/upload",
        "manage": "https://creator.douyin.com/creator-micro/content/manage",
        "identity": r"抖音号\s*[:：]\s*([A-Za-z0-9_.-]+)",
        "title": 'input[placeholder*="标题"]',
        "description": '[contenteditable="true"][data-placeholder*="描述"],'
                       '[contenteditable="true"][data-placeholder*="作品"],'
                       '[contenteditable="true"][class*="editor"],textarea[placeholder*="描述"]',
    },
    "xiaohongshu": {
        "host": "creator.xiaohongshu.com",
        "home": "https://creator.xiaohongshu.com/",
        "upload": "https://creator.xiaohongshu.com/publish/publish?source=official",
        "manage": "https://creator.xiaohongshu.com/new/note-manager",
        "identity": r"(?:小红书号|小红书账号)\s*[:：]\s*([A-Za-z0-9_.-]+)",
        "title": 'input[placeholder*="标题"]',
        "description": '[contenteditable="true"][role="textbox"],'
                       '[contenteditable="true"].tiptap,[contenteditable="true"].ql-editor,'
                       'textarea[placeholder*="正文"]',
    },
}
METRICS = {
    "views": ("播放量", "观看量", "阅读量", "播放", "观看", "阅读"),
    "likes": ("点赞量", "点赞数", "点赞", "赞"),
    "comments": ("评论量", "评论数", "评论"),
    "favorites": ("收藏量", "收藏数", "收藏"),
    "shares": ("分享量", "分享数", "分享", "转发量", "转发"),
}
AI_LABELS = {"内容由AI生成", "包含AI生成内容", "含AI合成内容", "作品含AI生成内容", "笔记含AI生成内容"}
VIDEO_INPUT = 'input[type="file"][accept*="video"],input[type="file"][accept*=".mp4"]'
COVER_INPUT = 'input[type="file"][accept*="image"],input[type="file"][accept*=".png"]'
# Visually checked against the creator note manager, 2026-09-21. Match the complete
# rendered SVG path, not the position of a number; unknown icons remain missing.
XHS_METRIC_ICONS = {
    "6dea39a3066198df3f32bf28c5ba1fd21cd4ef3cfed9639aa1a9cdf2784d1609": "阅读量",
    "c314fd37ee2273dfdd07cdf2a1c1c94175ed02077e913408a45510d5d9a8e02e": "评论",
    "458830fd364727b85b69ec3b6f407f0e4bc40e6083bb22b18e06d4a28e965a54": "点赞",
    "6d1a511b8ff35cc2ef6432557f6168784d201ef544aefb5b3685441bf3fcff8d": "收藏",
    "2c6b226ac8247f2ffa2b60665f6b23d10494e40e6eba0ee6947928dbfb0102a0": "分享",
}


class BrowserError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def clean(value):
    return " ".join(str(value or "").split())


def number(value):
    text = str(value or "").replace(",", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|亿|[kKwW])?", text)
    if not match:
        return None
    factor = {"万": 10000, "亿": 100000000, "k": 1000, "w": 10000}.get((match[2] or "").lower(), 1)
    result = float(match[1]) * factor
    return int(result) if result.is_integer() and 0 <= result <= 10**15 else None


def parse_metrics(pairs):
    """Only label/value pairs in the verified work region, never caption numbers."""
    pairs = [{**pair, "label": XHS_METRIC_ICONS.get(hashlib.sha256(pair["icon"].encode()).hexdigest(), "")}
             if pair.get("icon") else pair for pair in pairs]
    values, raw = {}, {}
    for field, labels in METRICS.items():
        matches = [(clean(pair.get("label")), clean(pair.get("value"))) for pair in pairs
                   if clean(pair.get("label")) in labels]
        parsed = {number(value) for _, value in matches if number(value) is not None}
        values[field] = next(iter(parsed)) if len(parsed) == 1 else None
        raw[field] = [{"label": label, "value": value} for label, value in matches]
    return {**values, "raw": raw}


def work_link(platform, value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        return None
    if platform == "douyin" and parsed.hostname in {"www.douyin.com", "douyin.com"}:
        match = re.fullmatch(r"/video/(\d+)/?", parsed.path)
        if match:
            return f"https://www.douyin.com/video/{match[1]}", match[1]
    if platform == "douyin" and parsed.hostname == "creator.douyin.com":
        match = re.fullmatch(r"/creator-micro/work-management/work-detail/(\d+)/?", parsed.path)
        if match:
            return f"https://www.douyin.com/video/{match[1]}", match[1]
    if platform == "xiaohongshu" and parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        match = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-fA-F]{16,32})/?", parsed.path)
        if match:
            return f"https://www.xiaohongshu.com/explore/{match[1]}", match[1]
    return None


def verified_identity(platform, state, expected=None):
    config = PLATFORMS[platform]
    url = urlsplit(state.get("url", ""))
    if url.scheme != "https" or url.hostname != config["host"]:
        raise BrowserError("BROWSER_PAGE_CHANGED", "请在对应平台创作服务平台操作，未执行当前页面动作")
    if state.get("challenge"):
        raise BrowserError("BROWSER_USER_ACTION_REQUIRED", "请在 Chrome 完成登录或验证码后重新检查")
    identities = set(re.findall(config["identity"], "\n".join(state.get("identity_regions", []))))
    if len(identities) != 1:
        raise BrowserError("ACCOUNT_VERIFICATION_REQUIRED", "页面未显示唯一的账号标识，请打开创作者账号信息页核验")
    identity = next(iter(identities))
    if expected and identity != expected:
        raise BrowserError("ACCOUNT_MISMATCH", "Chrome 当前账号与绑定的账号标识不一致")
    return identity


def verify_page(request, state):
    # The editor and works list omit numeric identity. Read it from a fresh home tab
    # in this same Chrome context; do not confuse the visible nickname with the ID.
    config = PLATFORMS[request["platform"]]
    if not re.search(config["identity"], "\n".join(state.get("identity_regions", []))):
        state = {**state, "identity_regions": request.get("_identity_regions", [])}
    return verified_identity(request["platform"], state, request.get("account_identifier"))


async def verify_session(browser, page, request):
    state = await snapshot(page)
    try:
        identity = verified_identity(request["platform"], state, request.get("account_identifier"))
    except BrowserError as error:
        if error.code != "ACCOUNT_VERIFICATION_REQUIRED":
            raise
        home = await browser.new_page(PLATFORMS[request["platform"]]["home"])
        try:
            for attempt in range(15):
                state = await snapshot(home)
                try:
                    identity = verified_identity(request["platform"], state, request.get("account_identifier"))
                    break
                except BrowserError as error:
                    if error.code not in {"ACCOUNT_VERIFICATION_REQUIRED", "BROWSER_PAGE_CHANGED"} or attempt == 14:
                        raise
                    await asyncio.sleep(0.5)
        finally:
            await browser.close_page(home)
    request["_identity_regions"] = state["identity_regions"]
    return identity


# This reads rendered DOM only. No private platform APIs, cookies, storage, or page instructions.
SNAPSHOT = r"""() => {
 const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
 const text = e => (e.innerText || e.textContent || '').trim();
 const regions = [...document.querySelectorAll('header,[class*="user-info"],[class*="userInfo"],'
   +'[class*="account-info"],[class*="accountInfo"],[class*="creator-info"],[class*="creatorInfo"],'
   +'[class^="unique_id-"],.others.description-text')]
   .filter(visible).map(text).filter(t => t.length < 1000);
 const dialogs = [...document.querySelectorAll('[role="dialog"],[class*="login"],[class*="captcha"],'
   +'[class*="verify"]')].filter(visible).map(text);
 const pairs = region => {
   const result = [];
   for (const e of region.querySelectorAll('[class^="metric-item-"]')) {
     const label = e.querySelector('[class^="metric-label-"],[class^="metric-item__title-"]');
     const value = e.querySelector('[class^="metric-value-"],[class^="metric-item__value-"]');
     if (label && value && visible(label) && visible(value)) result.push({label:text(label),value:text(value)});
   }
   for (const e of region.querySelectorAll('.note-card__stat')) {
     if (visible(e)) result.push({icon:[...e.querySelectorAll('svg path')].map(n=>n.getAttribute('d')).join('|'),
       value:text(e)});
   }
   for (const e of region.querySelectorAll('div,span,li,dt,label')) {
     if (!visible(e) || e.children.length) continue;
     const label = text(e);
     if (!/^(播放量|观看量|阅读量|播放|观看|阅读|点赞量|点赞数|点赞|赞|评论量|评论数|评论|收藏量|收藏数|收藏|分享量|分享数|分享|转发量|转发)$/.test(label)) continue;
     for (const other of [e.previousElementSibling, e.nextElementSibling]) {
       if (other && visible(other) && /^[\d,.]+\s*(万|亿|[kKwW])?$/.test(text(other)))
         result.push({label, value:text(other)});
     }
   }
 const heads = [...(region.closest('table') || region).querySelectorAll('th')].map(text);
   for (const row of (region.matches('tr') ? [region] : region.querySelectorAll('tr'))) {
     const cells = [...row.querySelectorAll('td')];
     cells.forEach((cell,i) => { if (heads[i] && visible(cell)) result.push({label:heads[i],value:text(cell)}); });
   }
   return result;
 };
 const cards = [...document.querySelectorAll('tr,[class^="video-card-content-"],.video-card,[class*="videoCard"],'
   +'.note-card,[class*="note-item"],[class*="noteItem"],[class*="content-item"],[class*="contentItem"]')].filter(visible);
 const works = cards.map(e => {
   const links = [...e.querySelectorAll('a[href]')].filter(visible).map(a=>a.href);
   if (location.hostname === 'creator.xiaohongshu.com' && e.matches('.note-card')) {
     try {
       const id = JSON.parse(e.getAttribute('data-impression')).noteTarget.value.noteId;
       if (/^[a-fA-F0-9]{24}$/.test(id)) links.push('https://www.xiaohongshu.com/explore/'+id);
     } catch (_) { /* Unknown card metadata cannot establish identity. */ }
   }
   return {texts:[...e.querySelectorAll('a,h1,h2,h3,h4,[class*="title"]')].filter(visible).map(text),
     links, pairs:pairs(e)};
 });
 if (location.hostname === 'creator.douyin.com' && /^\/creator-micro\/work-management\/work-detail\/\d+\/?$/.test(location.pathname)) {
   works.push({texts:[...document.querySelectorAll('[class^="video-info-"] [class^="info-title-text-"]')]
     .filter(visible).map(text),links:[location.href],pairs:pairs(document.body)});
 }
 return {url:location.href, identity_regions:regions, challenge:dialogs.some(t=>/扫码登录|手机号登录|验证码登录|请完成验证|拖动滑块|安全验证/.test(t)),
   works, success:[...document.querySelectorAll('[role="alert"],[class*="success"],[class*="toast"]')]
     .filter(visible).some(e=>/^(发布成功|笔记发布成功|已成功发布|发布完成)$/.test(text(e))),
   file_names:[...document.querySelectorAll('input[type="file"]')].flatMap(e=>[...e.files].map(f=>f.name))};
}"""


async def snapshot(page):
    return json.loads(await page.evaluate(SNAPSHOT))


async def unique(page, selector, *, visible=True):
    elements = await page.get_elements_by_css_selector(selector)
    if visible:
        elements = [e for e in elements if await e.evaluate(
            '() => !!this.getClientRects().length && getComputedStyle(this).visibility !== "hidden"'
        ) == "True"]
    if len(elements) != 1:
        raise BrowserError("BROWSER_CONTROL_AMBIGUOUS", "页面控件无法唯一定位，请在 Chrome 检查并人工接管")
    return elements[0]


async def fill(page, selector, value):
    element = await unique(page, selector)
    await element.fill(value)
    actual = await element.evaluate('() => this.isContentEditable ? this.innerText : this.value')
    if clean(actual.replace("\u200b", "")) != clean(value.replace("\u200b", "")):
        raise BrowserError("BROWSER_TEXT_MISMATCH", "文案回读与审核内容不一致，未执行发布")


async def upload(page, selector, path):
    element = await unique(page, selector, visible=False)
    # browser-use 0.13.10 exposes Actor elements and the underlying CDP transport.
    await element._client.send.DOM.setFileInputFiles(
        {"files": [str(path)], "backendNodeId": element._backend_node_id}, session_id=element._session_id
    )


async def file_hash(page, suffix):
    # Hash the actual File selected by Chrome, not a thumbnail or a filename guess.
    return await page.evaluate(r"""(suffix) => (async () => {
      const files = [...document.querySelectorAll('input[type="file"]')].flatMap(e=>[...e.files])
        .filter(f=>f.name.toLowerCase().endsWith(suffix));
      if (files.length !== 1) return '';
      const hash = await crypto.subtle.digest('SHA-256',await files[0].arrayBuffer());
      return [...new Uint8Array(hash)].map(b=>b.toString(16).padStart(2,'0')).join('');
    })()""", suffix)


async def declaration(page):
    return await page.evaluate(r"""(labels) => {
      const norm = s => (s||'').replace(/\s+/g,'');
      return [...document.querySelectorAll('label,[role="checkbox"],[role="switch"]')].some(e=>
        e.getClientRects().length && labels.includes(norm(e.innerText)) &&
        (e.getAttribute('aria-checked')==='true' || !!e.querySelector('input:checked')));
    }""", sorted(AI_LABELS)) == "True"


def matching_work(platform, state, title, work_id=None):
    candidates = {}
    for work in state.get("works", []):
        if not any(clean(t) == clean(title) or clean(t).startswith(clean(title) + " ")
                   for t in work.get("texts", [])):
            continue
        links = {found for url in work.get("links", []) if (found := work_link(platform, url))}
        if len(links) != 1:
            continue
        url, identity = next(iter(links))
        if work_id and identity != work_id:
            continue
        if identity in candidates:
            raise BrowserError("BROWSER_WORK_AMBIGUOUS", "同一作品出现多个数据区域，请只保留目标作品详情")
        candidates[identity] = {"work_url": url, "work_id": identity, "pairs": work.get("pairs", [])}
    if len(candidates) != 1:
        raise BrowserError("BROWSER_WORK_UNVERIFIED", "未找到唯一匹配标题和完整作品链接的作品，请打开作品管理中的目标作品")
    return next(iter(candidates.values()))


async def editor_matches(page, request, evidence):
    platform = request["platform"]
    state = await snapshot(page)
    verify_page(request, state)
    for field in ("title", "description"):
        element = await unique(page, PLATFORMS[platform][field])
        actual = await element.evaluate('() => this.isContentEditable ? this.innerText : this.value')
        if clean(actual.replace("\u200b", "")) != clean(request[field].replace("\u200b", "")):
            raise BrowserError("BROWSER_TEXT_MISMATCH", "当前编辑器文案与已审核文案不一致")
    if await file_hash(page, ".mp4") != evidence.get("video_sha256"):
        raise BrowserError("BROWSER_MEDIA_UNVERIFIED", "无法核验编辑器当前视频的完整 SHA-256，请人工核验素材")
    if await file_hash(page, ".png") != evidence.get("cover_sha256"):
        raise BrowserError("BROWSER_COVER_UNVERIFIED", "无法核验当前封面，请人工核验封面后发布")
    if not await declaration(page):
        raise BrowserError("BROWSER_AI_DECLARATION_REQUIRED", "请在 Chrome 勾选平台的 AI 合成内容声明后继续")


async def prepare(page, request):
    platform = request["platform"]
    state = await snapshot(page)
    identity = verify_page(request, state)
    video, cover = Path(request["video_path"]), Path(request["cover_path"])
    if not video.is_file() or not cover.is_file() or video.suffix.lower() != ".mp4" or cover.suffix.lower() != ".png":
        raise BrowserError("BROWSER_MEDIA_INVALID", "发布需要审核后的 MP4 视频和 PNG 封面")
    evidence = {"channel": "browser", "platform": platform, "verified_account_identifier": identity,
                "verified_at": datetime.now(UTC).isoformat(), "page": "editor",
                "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "cover_sha256": hashlib.sha256(cover.read_bytes()).hexdigest(),
                "title": request["title"], "description": request["description"],
                "final_publish_clicked": False}
    if (evidence["video_sha256"] != request.get("expected_video_sha256")
            or evidence["cover_sha256"] != request.get("expected_cover_sha256")):
        raise BrowserError("BROWSER_MEDIA_CHANGED", "本地视频或封面已偏离审核素材，未上传")
    try:
        # Never navigate away from an existing editor/draft or replace an unknown file selection.
        is_upload = urlsplit(state["url"]).path == urlsplit(PLATFORMS[platform]["upload"]).path
        path = urlsplit(state["url"]).path
        is_editor = is_upload or "/publish" in path or "/content/post/" in path
        if not is_editor:
            if state["file_names"]:
                raise BrowserError("BROWSER_DRAFT_PRESENT", "浏览器已有未处理草稿，请先处理当前页面")
            await page.navigate(PLATFORMS[platform]["upload"])
            await asyncio.sleep(2)
            verify_page(request, await snapshot(page))
        current_hash = await file_hash(page, ".mp4")
        previous = request.get("previous_evidence") or {}
        if current_hash and (current_hash != evidence["video_sha256"] or previous.get("video_sha256") != current_hash):
            raise BrowserError("BROWSER_DRAFT_PRESENT", "当前编辑器已有未绑定本任务的视频，请人工处理草稿")
        if not current_hash:
            editors = await page.get_elements_by_css_selector(PLATFORMS[platform]["title"])
            for editor in editors:
                if clean(await editor.evaluate('() => this.value || this.innerText')):
                    raise BrowserError("BROWSER_DRAFT_PRESENT", "当前编辑器已有文案，请先处理草稿")
            await upload(page, VIDEO_INPUT, video)
        deadline = asyncio.get_running_loop().time() + 150
        while True:
            state = await snapshot(page)
            verify_page(request, state)
            elements = await page.get_elements_by_css_selector(PLATFORMS[platform]["title"])
            if elements:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserError("BROWSER_UPLOAD_PENDING", "视频仍在处理，请等待 Chrome 处理完成后继续准备")
            await asyncio.sleep(1)
        await fill(page, PLATFORMS[platform]["title"], request["title"])
        await fill(page, PLATFORMS[platform]["description"], request["description"])
        if await file_hash(page, ".png") != evidence["cover_sha256"]:
            await upload(page, COVER_INPUT, cover)
        await editor_matches(page, request, evidence)
        return {"status": "WAITING_USER", "evidence": evidence}
    except BrowserError as error:
        return {"status": "NEEDS_ATTENTION", "evidence": {**evidence, "reason": error.code, "detail": error.message}}


async def submit(page, request):
    evidence = request["evidence"]
    if evidence.get("final_publish_clicked") or evidence.get("submit_started_at"):
        raise BrowserError("PUBLICATION_OUTCOME_UNKNOWN", "已有提交证据，不会再次点击发布")
    if evidence.get("video_sha256") != request.get("video_sha256"):
        raise BrowserError("BROWSER_MEDIA_UNVERIFIED", "当前素材与提交快照不一致")
    await editor_matches(page, request, evidence)
    buttons = await page.get_elements_by_css_selector('button,[role="button"]')
    matches = []
    for button in buttons:
        state = json.loads(await button.evaluate('''() => ({visible:!!this.getClientRects().length,
            text:(this.innerText||'').trim(),disabled:this.disabled||this.getAttribute('aria-disabled')==='true'})'''))
        if state["visible"] and not state["disabled"] and state["text"] in {"发布", "发布笔记", "确认发布"}:
            matches.append(button)
    if len(matches) != 1:
        raise BrowserError("BROWSER_CONTROL_AMBIGUOUS", "没有找到唯一可用的发布按钮，未点击发布")
    before = await snapshot(page)
    previous_ids = {link[1] for work in before.get("works", []) for url in work.get("links", [])
                    if (link := work_link(request["platform"], url))}
    # Actor 0.13.10 calculates the click point before its own scroll, so settle
    # offscreen buttons first to avoid clicking stale coordinates.
    await matches[0].evaluate('() => this.scrollIntoView({block: "center", behavior: "instant"})')
    await matches[0].click()
    evidence = {**evidence, "final_publish_clicked": True}
    # Any second confirmation, timeout, moderation or missing exact work link stays VERIFYING.
    for _ in range(15):
        await asyncio.sleep(1)
        state = await snapshot(page)
        try:
            verify_page(request, state)
            work = matching_work(request["platform"], state, request["title"])
            if state["success"] and work["work_id"] not in previous_ids:
                return {"status": "PUBLISHED", "work_url": work["work_url"], "work_id": work["work_id"],
                        "evidence": {**evidence, "success_visible": True}}
        except BrowserError:
            continue
    return {"status": "VERIFYING", "evidence": evidence}


async def operate(browser, request):
    platform, action = request["platform"], request["action"]
    host = PLATFORMS[platform]["host"]
    pages = await browser.get_pages()
    matches = [page for page in pages if urlsplit(await page.get_url()).hostname == host]
    if action in {"open", "inspect"}:
        matches = matches[:1]  # Inspection opens its own identity tab when needed; it does not edit a draft.
    if action in {"detect", "metrics"}:
        # A title click opens a separate Douyin detail tab. Reading that exact work
        # is safe while the list remains open; writes still require one creator tab.
        detail = []
        for candidate in matches:
            link = work_link(platform, await candidate.get_url())
            if link and (not request.get("work_id") or request["work_id"] == link[1]):
                detail.append(candidate)
        if detail:
            matches = detail
    if len(matches) > 1:
        raise BrowserError("BROWSER_TABS_AMBIGUOUS", "该独立 Chrome 中有多个平台标签页，请只保留当前操作页")
    if not matches and action == "open":
        page = await browser.new_page(PLATFORMS[platform]["home"])
        await asyncio.sleep(2)
    elif not matches:
        raise BrowserError("BROWSER_PAGE_REQUIRED", "请先打开对应创作服务平台并完成登录")
    else:
        page = matches[0]
    if action in {"open", "inspect"}:
        result = {"channel": "browser", "platform": platform, "ready": False,
                  "account_identifier": None, "warnings": []}
        try:
            result["account_identifier"] = await verify_session(browser, page, request)
            result["ready"] = True
        except BrowserError as error:
            result["warnings"].append(error.message)
        return result
    await verify_session(browser, page, request)
    if action == "prepare":
        return await prepare(page, request)
    if action == "submit":
        return await submit(page, request)
    state = await snapshot(page)
    verify_page(request, state)
    if action in {"detect", "metrics"}:
        work = matching_work(platform, state, request.get("title") or request["content_title"], request.get("work_id"))
        if action == "detect":
            return {"work_url": work["work_url"], "work_id": work["work_id"], "evidence": {
                "channel": "browser", "verified_account_identifier": request["account_identifier"],
                "title_verified": True, "work_id_verified": True}}
        values = parse_metrics(work["pairs"])
        if not any(values[key] is not None for key in METRICS):
            raise BrowserError("BROWSER_METRICS_UNAVAILABLE", "目标作品页面未显示可核验指标，请打开作品数据详情后重试")
        values["raw"] = {"channel": "browser", "work_id": work["work_id"], "observations": values["raw"]}
        return values
    raise BrowserError("BROWSER_ACTION_INVALID", "不支持的浏览器动作")


async def main(request):
    from browser_use import Browser

    platform = request["platform"]
    if platform not in PLATFORMS:
        raise BrowserError("BROWSER_PLATFORM_INVALID", "不支持的平台")
    endpoint = urlsplit(request["endpoint"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
        raise BrowserError("BROWSER_ENDPOINT_INVALID", "仅允许本机 Chrome 调试连接")
    browser = Browser(cdp_url=request["endpoint"], use_cloud=False, keep_alive=True,
                      enable_default_extensions=False, auto_download_pdfs=False, permissions=[],
                      allowed_domains=[PLATFORMS[platform]["host"]])
    try:
        await browser.start()
        command = await browser.cdp_client.send.Browser.getBrowserCommandLine()
        if f"--user-data-dir={request['profile']}" not in command.get("arguments", []):
            raise BrowserError("BROWSER_PROFILE_MISMATCH", "调试端口不属于本项目的独立 Chrome，停止操作")
        return await operate(browser, request)
    finally:
        await browser.stop()


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.load(sys.stdin)))
    except BrowserError as error:
        result = {"error": {"code": error.code, "message": error.message}}
    except Exception:  # noqa: BLE001 - keep page contents and runtime internals out of API/log responses
        result = {"error": {"code": "BROWSER_RUNTIME_FAILED", "message": "浏览器运行异常，请检查 Chrome 现场后重试"}}
    print("BROWSER_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
