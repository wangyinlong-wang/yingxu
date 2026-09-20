"""Explicitly authorized final posting; a durable intent callback fences every click."""

from __future__ import annotations

import re
import time

from . import device as ui
from .errors import CanceledError, IntegrationError

_SUCCESS_LABELS = {"发布成功", "笔记发布成功", "已成功发布", "发布完成"}


def open_account_profile(*, serial, platform, account_identifier, canceled, app_config):
    """Navigate ordinary pages; leave an existing editor untouched for resume validation."""
    ui._validate_serial(serial)
    ui._require_connected(serial)
    phone = ui._connect_device(serial)
    if not ui.ensure_unlocked(serial, phone, canceled, app_config):
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "手机尚未解锁，请检查自动解锁设置")
    package, _ = ui._current_app(phone)
    package = phone.info.get("currentPackageName") or package
    nodes = ui._hierarchy(phone)
    current_platform = next(
        (name for name, item in ui._PLATFORMS.items() if item["package"] == package), None
    )
    current_page = ui._classify_page(current_platform, package, nodes) if current_platform else "unknown"
    if current_page in {"editor", "picker", "camera"}:
        if current_platform == platform:
            return
        raise IntegrationError("DEVICE_DRAFT_PRESENT", "另一平台有未完成的编辑流程，请先处理手机现场")
    config = ui._platform(platform)
    if package != config["package"]:
        if canceled():
            raise CanceledError()
        phone.app_start(config["package"], stop=False)
        time.sleep(ui._WAIT_AFTER_NAVIGATION)
    for _ in range(4):
        if canceled():
            raise CanceledError()
        page, package, nodes = ui._guarded_page(phone, platform)
        if page in {"editor", "picker", "camera"}:
            return
        if package != config["package"] or page in {"login", "permission", "captcha", "locked"}:
            raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "目标平台需要登录、授权或其他本人操作")
        if page == "profile":
            if not ui._account_visible(ui._tokens(nodes), config, account_identifier):
                raise IntegrationError("ACCOUNT_MISMATCH", "目标平台当前账号与已绑定账号不一致")
            return
        if platform == "douyin" and any(
            node.get("resource_id") == "com.ss.android.ugc.aweme:id/z0a"
            and node.get("text") == "链接已复制成功，去粘贴分享："
            for node in nodes
        ):
            phone.press("back")
            time.sleep(ui._WAIT_AFTER_NAVIGATION)
            continue
        if ui._click_unique(phone, nodes, config["profile_labels"]):
            time.sleep(ui._WAIT_AFTER_NAVIGATION)
            continue
        if "更多，按钮" in ui._tokens(nodes) and ui._click_unique(phone, nodes, ("返回",)):
            time.sleep(ui._WAIT_AFTER_NAVIGATION)
            continue
        break
    raise IntegrationError("ACCOUNT_VERIFICATION_REQUIRED", "未能进入可核验的本人主页，请检查手机现场")


def _copy_published_link(phone, platform, nodes):
    from ..services import normalized_work

    if platform == "douyin":
        work_id, _ = ui._douyin_work_link(phone, nodes, "")
        return f"https://www.douyin.com/video/{work_id}", work_id
    if not ui._click_unique(phone, nodes, ("分享", "更多", "分享，按钮", "更多，按钮")):
        raise IntegrationError("WORK_LINK_UNAVAILABLE", "已提交发布，但未找到作品链接入口")
    time.sleep(ui._WAIT_AFTER_NAVIGATION)
    _, package, nodes = ui._guarded_page(phone, platform)
    if package != ui._platform(platform)["package"]:
        raise IntegrationError("DEVICE_PAGE_CHANGED", "手机已切换应用，停止读取发布链接")
    marker = "LocalPublisher:published-link"
    try:
        phone.clipboard = marker
        if not ui._click_unique(phone, nodes, ("复制链接", "分享链接")):
            raise IntegrationError("WORK_LINK_UNAVAILABLE", "没有找到唯一的复制链接入口")
        time.sleep(ui._WAIT_AFTER_NAVIGATION)
        links = re.findall(
            r"https://(?:www\.xiaohongshu\.com|(?:www\.)?xhslink\.(?:com|cn))/[^\s]+",
            phone.clipboard or "",
        )
        if len(links) != 1:
            raise IntegrationError("WORK_LINK_UNVERIFIED", "没有读取到新的作品链接")
        return normalized_work(platform, links[0], None)
    finally:
        if phone.clipboard == marker:
            phone.clipboard = ""


def _published_title_visible(tokens, title):
    clean_title = " ".join(str(title or "").split())
    return bool(clean_title) and any(
        " ".join(token.split()) == clean_title or " ".join(token.split()).startswith(clean_title + " ")
        for token in tokens
    )


def detect_published_work(
    *,
    serial,
    platform,
    account_identifier,
    title,
    canceled,
    app_config,
):
    """Read the link for the reviewed work that is already open on the bound phone."""
    ui._validate_serial(serial)
    ui._require_connected(serial)
    phone = ui._connect_device(serial)
    if not ui.ensure_unlocked(serial, phone, canceled, app_config):
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "手机尚未解锁，请检查自动解锁设置")
    if canceled():
        raise CanceledError()
    config = ui._platform(platform)
    page, package, nodes = ui._guarded_page(phone, platform)
    if package != config["package"] or page in {"login", "permission", "captcha", "locked"}:
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "请在绑定手机上打开刚发布的目标作品详情页")
    tokens = ui._tokens(nodes)
    if not _published_title_visible(tokens, title):
        raise IntegrationError("WORK_TITLE_MISMATCH", "当前手机页面未显示已审核作品标题，未读取链接")
    work_url, work_id = _copy_published_link(phone, platform, nodes)
    return {
        "work_url": work_url,
        "work_id": work_id,
        "evidence": {
            "confirmation_source": "device_detection",
            "work_title_verified": True,
            "verified_account_identifier": account_identifier,
            "app_version": ui._package_version(serial, config["package"]),
        },
    }


def submit_publication(
    *,
    serial,
    platform,
    account_identifier,
    video_sha256,
    caption,
    title,
    description,
    evidence,
    record_submit,
    canceled,
):
    """Only call after prepare_upload returned WAITING_USER in the same device lock."""
    phone = ui._connect_device(serial)
    config = ui._platform(platform)
    version = ui._package_version(serial, config["package"])
    clean_caption, clean_title, clean_description = (
        " ".join(value.split()) for value in (caption, title, description)
    )
    if (
        not ui._valid_previous_evidence(
            evidence,
            serial=serial,
            platform=platform,
            app_version=version,
            account_identifier=account_identifier,
            video_sha256=video_sha256,
            caption=clean_caption,
        )
        or evidence.get("page") != "editor"
        or evidence.get("final_publish_clicked") is not False
    ):
        raise IntegrationError("PUBLISH_EVIDENCE_REQUIRED", "最终发布缺少本次账号和素材的连续核验证据")
    if canceled():
        raise CanceledError()
    page, package, nodes = ui._guarded_page(phone, platform)
    if (
        page != "editor"
        or package != config["package"]
        or not ui._caption_visible(
            ui._tokens(nodes), clean_caption, title=clean_title, description=clean_description
        )
    ):
        raise IntegrationError("PUBLISH_EDITOR_CHANGED", "最终编辑页或审核文案发生变化，未点击发布")
    selected = None
    labels = ("发作品", "发布") if platform == "douyin" else ("发笔记", "发布笔记", "发布")
    for label in labels:
        node = ui._unique_node(nodes, (label,))
        if node:
            selected = ui._selector(phone, node, nodes, require_clickable=True)
            if selected is not None:
                break
    if selected is None:
        raise IntegrationError("PUBLISH_CONTROL_UNAVAILABLE", "未识别到唯一的最终发布按钮")
    record_submit("intent")
    if canceled():
        raise CanceledError()
    ui._click_selected(phone, selected)
    declaration_selected = False
    if platform == "xiaohongshu" and version == "9.46.0":
        time.sleep(ui._WAIT_AFTER_NAVIGATION)
        _page, package, nodes = ui._guarded_page(phone, platform)
        tokens = ui._tokens(nodes)
        if package != config["package"] or "声明内容来源" not in tokens:
            raise IntegrationError(
                "CONTENT_DECLARATION_REQUIRED",
                "小红书未显示已核验的内容来源声明，未继续提交",
            )
        if not ui._click_unique(phone, nodes, ("含 AI 合成内容",)):
            raise IntegrationError(
                "CONTENT_DECLARATION_UNAVAILABLE",
                "未识别到唯一的 AI 合成内容声明选项",
            )
        time.sleep(ui._WAIT_AFTER_NAVIGATION)
        _page, package, nodes = ui._guarded_page(phone, platform)
        confirm_node = ui._unique_node(nodes, ("确认并发布",))
        confirm_control = (
            ui._selector(phone, confirm_node, nodes, require_clickable=True)
            if confirm_node
            else None
        )
        if package != config["package"] or confirm_control is None:
            raise IntegrationError(
                "CONTENT_DECLARATION_UNCONFIRMED",
                "AI 内容声明未生效，未点击确认并发布",
            )
        ui._click_selected(phone, confirm_control)
        declaration_selected = True
    record_submit("clicked")
    success_seen = False
    for _ in range(8):
        if canceled():
            break
        time.sleep(ui._WAIT_AFTER_NAVIGATION)
        page, package, nodes = ui._guarded_page(phone, platform)
        if package != config["package"] or page in {"login", "permission", "captcha", "locked"}:
            break
        if page == "editor":
            continue
        tokens = ui._tokens(nodes)
        success_seen = success_seen or any(
            node.get("text") in _SUCCESS_LABELS
            and node.get("text") not in {clean_title, clean_description}
            and not node.get("resource_id", "").endswith("/desc")
            and "EditText" not in node.get("class_name", "")
            for node in nodes
        )
        if not success_seen:
            continue
        if ui._click_unique(phone, nodes, ("查看作品", "查看笔记")):
            time.sleep(ui._WAIT_AFTER_NAVIGATION)
            _, package, nodes = ui._guarded_page(phone, platform)
            tokens = ui._tokens(nodes)
        if package != config["package"]:
            break
        if not _published_title_visible(tokens, clean_title):
            continue
        try:
            work_url, work_id = _copy_published_link(phone, platform, nodes)
        except IntegrationError:  # The intent is durable; an unreadable result must never repeat the click.
            break
        return {
            "status": "PUBLISHED",
            "work_url": work_url,
            "work_id": work_id,
            "evidence": {
                "final_publish_clicked": True,
                "success_message_observed": True,
                "work_title_verified": True,
                "confirmation_source": "device",
                "content_declaration": "ai_generated" if declaration_selected else None,
            },
        }
    return {
        "status": "VERIFYING",
        "evidence": {
            "final_publish_clicked": True,
            "success_message_observed": success_seen,
            "content_declaration": "ai_generated" if declaration_selected else None,
            "reason": "已点击最终发布，尚未取得足够的成功页面与作品链接证据；不会重复提交",
        },
    }
