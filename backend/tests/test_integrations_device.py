from __future__ import annotations

import hashlib
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations import device
from app.integrations.errors import CanceledError, IntegrationError


def _hierarchy(*tokens: str, clickable: set[str] | None = None) -> str:
    clickable = clickable or set()
    nodes = []
    for index, token in enumerate(tokens):
        is_clickable = "true" if token in clickable else "false"
        nodes.append(
            f'<node index="{index}" text="{token}" resource-id="id/{index}" '
            f'class="android.widget.TextView" clickable="{is_clickable}" />'
        )
    return '<?xml version="1.0" encoding="UTF-8"?><hierarchy>' + "".join(nodes) + "</hierarchy>"


class _Selected:
    def __init__(self, owner, selector):
        self.owner = owner
        self.selector = selector

    count = 1

    @property
    def info(self):
        self.owner.selected_selector = self.selector
        package = self.owner.info.get("currentPackageName")
        return {
            "visibleBounds": {"left": 11, "top": 21, "right": 42, "bottom": 62},
            "packageName": package,
        }

    def click(self):
        self.owner.actions.append(("click", self.selector))

    def set_text(self, value):
        self.owner.actions.append(("set_text", self.selector, value))
        self.owner.field_values[tuple(sorted(self.selector.items()))] = value

    def get_text(self):
        return self.owner.field_values.get(tuple(sorted(self.selector.items())), "")


class _FakeDevice:
    def __init__(self, hierarchies, packages=None):
        self.hierarchies = list(hierarchies)
        self.packages = list(packages or ["com.ss.android.ugc.aweme"] * len(self.hierarchies))
        self.index = 0
        self.actions = []
        self.field_values = {}
        self.selected_selector = None
        self.info = {"screenOn": True, "displayWidth": 1280, "displayHeight": 2772}
        self.jsonrpc = SimpleNamespace(click=self._rpc_click)

    def _rpc_click(self, x, y):
        assert type(x) is int and type(y) is int
        self.actions.append(("click", self.selected_selector))
        return True

    def app_current(self):
        return {"package": self.packages[min(self.index, len(self.packages) - 1)], "activity": ".Main"}

    def dump_hierarchy(self, **_kwargs):
        value = self.hierarchies[min(self.index, len(self.hierarchies) - 1)]
        self.index += 1
        return value

    def __call__(self, **selector):
        return _Selected(self, selector)


@pytest.fixture
def local_media(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "video.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"deterministic video fixture")
    cover.write_bytes(b"deterministic cover fixture")
    return video, cover


def _prepare_monkeypatch(monkeypatch, fake: _FakeDevice):
    monkeypatch.setattr(device, "_require_connected", lambda _serial: {"serial": _serial, "state": "device"})
    monkeypatch.setattr(device, "_package_version", lambda _serial, _package: "31.2.0")
    monkeypatch.setattr(device, "_connect_device", lambda _serial: fake)
    monkeypatch.setattr(device, "ensure_unlocked", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(device, "_WAIT_AFTER_NAVIGATION", 0)


def _pin_hierarchy(*, proximity: bool = False) -> str:
    nodes = []
    if proximity:
        nodes.append(
            '<node text="已进入防误触模式" package="android" '
            'resource-id="android.miui:id/screen_on_proximity_sensor_text_title" />'
        )
    else:
        nodes.append(
            '<node text="" package="com.android.systemui" resource-id="com.android.systemui:id/pinEntry" />'
        )
        nodes.extend(
            f'<node text="{digit}" package="com.android.systemui" resource-id="com.android.systemui:id/key{digit}" '
            'class="android.widget.TextView" clickable="true" />'
            for digit in range(10)
        )
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


def test_list_devices_returns_empty_when_adb_absent(monkeypatch):
    monkeypatch.setattr(device, "_adb_binary", lambda: None)
    assert device.list_devices() == []


def test_keyguard_uses_system_window_nodes_when_activity_belongs_to_covered_app(monkeypatch):
    fake = _FakeDevice([_pin_hierarchy()], packages=["com.ss.android.ugc.aweme"])
    nodes = device._system_ui_nodes(fake)
    assert any(node["resource_id"] == "com.android.systemui:id/pinEntry" for node in nodes)
    impostor = _FakeDevice(
        [_pin_hierarchy().replace('package="com.android.systemui"', 'package="unrelated.app"')]
    )
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    with pytest.raises(IntegrationError) as error:
        device._system_ui_nodes(impostor)
    assert error.value.code == "DEVICE_UNLOCK_SCREEN_UNKNOWN"


def test_ensure_unlocked_is_noop_and_does_not_read_keychain_when_already_unlocked(monkeypatch):
    fake = _FakeDevice([])
    calls = []
    monkeypatch.setattr(
        device,
        "_adb",
        lambda _serial, *args, **_kwargs: calls.append(args) or "showing=false",
    )
    monkeypatch.setattr(device, "get_device_pin", lambda _serial: pytest.fail("PIN must not be read"))

    assert device.ensure_unlocked("ABC123", fake) is True
    assert calls == [("shell", "dumpsys", "window", "policy")]


def test_locked_device_without_saved_pin_keeps_manual_behavior(monkeypatch, tmp_path):
    fake = _FakeDevice([])
    commands = []

    def adb(_serial, *args, **_kwargs):
        commands.append(args)
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=true"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake"
        return ""

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda _serial: None)
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    assert device.ensure_unlocked("ABC123", fake, app_config=config) is False
    assert ("shell", "input", "keyevent", "KEYCODE_MENU") in commands
    assert not device.unlock_failure_latched("ABC123", config)


def test_wireless_transport_uses_physical_serial_for_keychain_identity(monkeypatch, tmp_path):
    fake = _FakeDevice([])
    pin_reads = []

    def adb(_serial, *args, **_kwargs):
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=true"
        if args[:4] == ("shell", "getprop", "ro.serialno"):
            return "PHYSICAL123\n"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake"
        return ""

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda serial: pin_reads.append(serial) or None)
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    assert device.ensure_unlocked("192.0.2.10:5555", fake, app_config=config) is False
    assert pin_reads == ["PHYSICAL123"]
    assert not device.unlock_failure_latched("PHYSICAL123", config)


def test_ensure_unlocked_handles_sleep_and_miui_proximity_then_confirms_success(monkeypatch, tmp_path):
    system_package = "com.android.systemui"
    fake = _FakeDevice(
        [_pin_hierarchy(proximity=True), _pin_hierarchy()],
        packages=[system_package, system_package],
    )
    fake.info["currentPackageName"] = system_package
    commands = []
    keyguard = iter([True, True, False])

    def adb(_serial, *args, **_kwargs):
        commands.append(args)
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return f"showing={str(next(keyguard)).lower()}"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Dozing"
        return ""

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda _serial: "246810")
    for name in (
        "_WAIT_AFTER_POWER",
        "_WAIT_AFTER_SYSTEM_KEY",
        "_WAIT_AFTER_PROXIMITY",
        "_WAIT_AFTER_PIN",
    ):
        monkeypatch.setattr(device, name, 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    assert device.ensure_unlocked("ABC123", fake, lambda: False, config) is True
    keyevents = [args[-1] for args in commands if args[:3] == ("shell", "input", "keyevent")]
    assert keyevents == ["KEYCODE_POWER", "KEYCODE_MENU", "KEYCODE_VOLUME_UP", "KEYCODE_MENU"]
    assert [action[1]["resourceId"] for action in fake.actions] == [
        "com.android.systemui:id/key2",
        "com.android.systemui:id/key4",
        "com.android.systemui:id/key6",
        "com.android.systemui:id/key8",
        "com.android.systemui:id/key1",
        "com.android.systemui:id/key0",
    ]
    assert not device.unlock_failure_latched("ABC123", config)


def test_failed_pin_attempt_is_latched_across_calls_without_retry_or_plaintext(monkeypatch, tmp_path):
    system_package = "com.android.systemui"
    fake = _FakeDevice([_pin_hierarchy()], packages=[system_package])
    fake.info["currentPackageName"] = system_package
    pin_reads = []

    def adb(_serial, *args, **_kwargs):
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=true"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake"
        return ""

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda serial: pin_reads.append(serial) or "246810")
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    monkeypatch.setattr(device, "_WAIT_AFTER_PIN", 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    with pytest.raises(IntegrationError) as first:
        device.ensure_unlocked("ABC123", fake, app_config=config)
    assert first.value.code == "DEVICE_UNLOCK_FAILED"
    with pytest.raises(IntegrationError) as second:
        device.ensure_unlocked("ABC123", fake, app_config=config)
    assert second.value.code == "DEVICE_UNLOCK_BLOCKED"
    assert pin_reads == ["ABC123"]
    assert len(fake.actions) == 6

    marker = device._unlock_failure_path("ABC123", config)
    assert marker.stat().st_mode & 0o777 == 0o600
    assert "ABC123" not in marker.name
    marker_text = marker.read_text(encoding="ascii")
    assert "ABC123" not in marker_text and "246810" not in marker_text


def test_unknown_package_never_receives_pin_input_or_creates_attempt(monkeypatch, tmp_path):
    fake = _FakeDevice([_pin_hierarchy()], packages=["untrusted.package"])
    fake.info["currentPackageName"] = "untrusted.package"

    def adb(_serial, *args, **_kwargs):
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=true"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake"
        return ""

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda _serial: "246810")
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    with pytest.raises(IntegrationError) as caught:
        device.ensure_unlocked("ABC123", fake, app_config=config)
    assert caught.value.code == "DEVICE_UNLOCK_SCREEN_UNKNOWN"
    assert fake.actions == []
    assert not device.unlock_failure_latched("ABC123", config)


def test_cancellation_before_each_digit_leaves_latch_and_stops_remaining_input(monkeypatch, tmp_path):
    system_package = "com.android.systemui"
    fake = _FakeDevice([_pin_hierarchy()], packages=[system_package])
    fake.info["currentPackageName"] = system_package

    def adb(_serial, *args, **_kwargs):
        if args[:4] == ("shell", "dumpsys", "window", "policy"):
            return "showing=true"
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake"
        return ""

    checks = []

    def canceled():
        checks.append(True)
        return len(checks) >= 3

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "get_device_pin", lambda _serial: "246810")
    monkeypatch.setattr(device, "_WAIT_AFTER_SYSTEM_KEY", 0)
    config = device.AppConfig(data_dir=tmp_path, _env_file=None)

    with pytest.raises(CanceledError):
        device.ensure_unlocked("ABC123", fake, canceled, config)
    assert len(fake.actions) == 1
    assert device.unlock_failure_latched("ABC123", config)


def test_adb_shell_preserves_untrusted_url_as_one_literal_argument(monkeypatch):
    captured = []
    monkeypatch.setattr(device, "_adb_binary", lambda: "/fixture/adb")
    monkeypatch.setattr(device, "_run_command", lambda argv, **kwargs: captured.append(argv) or "")
    url = "https://www.douyin.com/video/123?value=$(unexpected)&name=two words"
    device._adb("ABC123", "shell", "am", "start", "-d", url)
    assert captured[0][:4] == ["/fixture/adb", "-s", "ABC123", "shell"]
    assert shlex.split(captured[0][4]) == ["am", "start", "-d", url]


def test_accessibility_button_and_duplicate_child_label_navigate_once():
    fake = _FakeDevice(
        ['<hierarchy><node text="我" content-desc="我，按钮" clickable="false" /></hierarchy>']
    )
    nodes = device._hierarchy(fake)
    assert device._click_unique(fake, nodes, ("我",))
    assert len(fake.actions) == 1

    fake = _FakeDevice(
        [
            '<hierarchy><node content-desc="我" clickable="true"><node text="我" clickable="false" /></node></hierarchy>'
        ]
    )
    nodes = device._hierarchy(fake)
    assert device._click_unique(fake, nodes, ("我",))
    assert fake.actions == [("click", {"description": "我"})]


def test_xiaohongshu_946_profile_accepts_edit_home_marker():
    config = device._PLATFORMS["xiaohongshu"]
    fake = _FakeDevice(
        [
            (
                '<hierarchy><node text="小红书号：fixture-account" package="com.xingin.xhs" />'
                '<node text="编辑主页" package="com.xingin.xhs" />'
                '<node text="我" package="com.xingin.xhs" />'
                '<node text="首页" package="com.xingin.xhs" /></hierarchy>'
            )
        ]
    )
    nodes = device._hierarchy(fake)

    assert device._classify_page("xiaohongshu", config["package"], nodes) == "profile"


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (("从相册选择", "拍摄与直播"), "camera"),
        (("全部", "视频", "照片"), "picker"),
    ],
)
def test_xiaohongshu_946_media_pages_are_classified(tokens, expected):
    config = device._PLATFORMS["xiaohongshu"]
    fake = _FakeDevice([_hierarchy(*tokens)])

    assert device._classify_page(
        "xiaohongshu", config["package"], device._hierarchy(fake)
    ) == expected


def test_xiaohongshu_946_editor_uses_description_and_publish_note_labels():
    config = device._PLATFORMS["xiaohongshu"]
    fake = _FakeDevice([_hierarchy("添加标题", "添加作品描述", "公开可见", "发笔记")])

    assert device._classify_page(
        "xiaohongshu", config["package"], device._hierarchy(fake)
    ) == "editor"
    assert "添加作品描述" in config["caption_placeholders"]


def test_unique_clickable_media_item_is_distinct_from_same_named_root():
    fake = _FakeDevice(
        [
            '<hierarchy><node resource-id="id/root_view" clickable="false"><node resource-id="id/root_view" clickable="true" /></node></hierarchy>'
        ]
    )
    nodes = device._hierarchy(fake)
    selected = device._selector(fake, nodes[1], nodes, require_clickable=True)
    selected.click()
    assert fake.actions == [("click", {"resourceId": "id/root_view", "clickable": True})]


@pytest.mark.parametrize(
    "change,code",
    [
        ("ambiguous", "DEVICE_CONTROL_AMBIGUOUS"),
        ("hidden", "DEVICE_CONTROL_HIDDEN"),
        ("app_changed", "DEVICE_PAGE_CHANGED"),
        ("rejected", "DEVICE_ACTION_REJECTED"),
    ],
)
def test_click_stops_when_target_or_phone_state_is_not_safe(change, code):
    fake = _FakeDevice([])
    control = SimpleNamespace(
        count=1,
        info={
            "visibleBounds": {"left": 10, "top": 20, "right": 31, "bottom": 41},
            "packageName": "com.ss.android.ugc.aweme",
        },
    )
    if change == "ambiguous":
        control.count = 2
    elif change == "hidden":
        control.info["visibleBounds"]["right"] = 0
    elif change == "app_changed":
        fake.info["currentPackageName"] = "different.app"
    else:
        fake.jsonrpc.click = lambda x, y: False
    with pytest.raises(IntegrationError) as error:
        device._click_selected(fake, control)
    assert error.value.code == code
    assert fake.actions == []


def test_douyin_404_profile_and_expired_xiaohongshu_login_are_distinct():
    fake = _FakeDevice([_hierarchy("抖音号：fixture-user", "编辑主页", "作品")])
    assert device._classify_page("douyin", "com.ss.android.ugc.aweme", device._hierarchy(fake)) == "profile"
    fake = _FakeDevice([_hierarchy("重新登录")])
    assert device._classify_page("xiaohongshu", "com.xingin.xhs", device._hierarchy(fake)) == "login"


def test_douyin_final_editor_fills_title_and_description_separately():
    xml = '<hierarchy><node text="添加标题" hint="添加标题" resource-id="id/title" class="android.widget.EditText" /><node text="添加作品描述.." hint="添加作品描述.." resource-id="id/body" class="android.widget.EditText" /><node text="添加话题" /><node content-desc="发作品" clickable="true" /></hierarchy>'
    fake = _FakeDevice([xml])
    nodes = device._hierarchy(fake)
    assert device._classify_page("douyin", "com.ss.android.ugc.aweme", nodes) == "editor"
    assert device._set_caption(
        fake, nodes, ("添加作品描述",), "审核标题 正文 #科技", title="审核标题", description="正文 #科技"
    )
    assert fake.actions == [
        ("set_text", {"resourceId": "id/title"}, "审核标题"),
        ("set_text", {"resourceId": "id/body"}, "正文 #科技"),
    ]
    assert device._caption_visible(
        ["审核标题", "正文 #科技"], "审核标题 正文 #科技", title="审核标题", description="正文 #科技"
    )
    assert not device._caption_visible(
        ["审核标题", "被截断正文"], "审核标题 正文 #科技", title="审核标题", description="正文 #科技"
    )


@pytest.mark.parametrize("extra_media", [False, True])
def test_douyin_album_selects_only_one_inventory_backed_video(monkeypatch, extra_media):
    digest = "a" * 64
    folder = "lp_" + digest[:16]
    remote = f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"
    path = remote.replace("/sdcard", "/storage/emulated/0")
    inventory = f"Row: 0 _id=1535, _data={path}, _size=9, mime_type=video/mp4"
    if extra_media:
        inventory += "\nRow: 1 _id=1536, _data=/unrelated.mp4, _size=9, mime_type=video/mp4"
    monkeypatch.setattr(device, "_adb", lambda *args, **kwargs: inventory)
    monkeypatch.setattr(device, "_WAIT_AFTER_NAVIGATION", 0)
    grid = f'<hierarchy><node text="{folder}" /><node text="视频" /><node resource-id="com.ss.android.ugc.aweme:id/root_view" clickable="true"><node text="01:00" resource-id="com.ss.android.ugc.aweme:id/gc9" /></node></hierarchy>'
    fake = _FakeDevice(
        [
            _hierarchy("所有照片", "视频", clickable={"所有照片"}),
            _hierarchy(folder, clickable={folder}),
            _hierarchy(folder, "视频", clickable={"视频"}),
            grid,
        ]
    )
    nodes = device._hierarchy(fake)
    item, _ = device._douyin_album_item(fake, nodes, "ABC123", remote, 9, digest, lambda: False)
    if extra_media:
        assert item is None
        assert fake.actions == []
    else:
        assert item["resource_id"] == "com.ss.android.ugc.aweme:id/root_view"
        assert len(fake.actions) == 3


def test_transfer_rejects_changed_video_and_preserves_png_extension(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"fixture")
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        return "incorrect-hash video.mp4" if "sha256sum" in args else ""

    monkeypatch.setattr(device, "_adb", adb)
    with pytest.raises(IntegrationError) as error:
        device._transfer_media("ABC123", video, cover, "a" * 64)
    assert error.value.code == "MEDIA_TRANSFER_MISMATCH"
    assert any(call[-1].endswith(".png") for call in calls)


def test_uiautomator_missing_or_mismatch_requires_explicit_setup(monkeypatch, tmp_path):
    jar = tmp_path / "u2.jar"
    jar.write_bytes(b"expected")
    monkeypatch.setattr(device, "_local_u2_jar", lambda: jar)
    monkeypatch.setattr(
        device, "_adb", lambda *_args, **_kwargs: "different checksum  /data/local/tmp/u2.jar"
    )

    with pytest.raises(IntegrationError) as caught:
        device._require_uiautomator_setup("ABC123")

    assert caught.value.code == "DEVICE_SETUP_REQUIRED"


def test_prepare_upload_stops_on_wrong_account_before_transfer(monkeypatch, local_media):
    video, cover = local_media
    fake = _FakeDevice([_hierarchy("抖音号：wrong-user", "作品", "编辑资料")])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_transfer_media", lambda *_args: pytest.fail("must not transfer"))

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption="测试文案",
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    assert result["status"] == "NEEDS_ATTENTION"
    assert result["evidence"]["reason"] == "ACCOUNT_MISMATCH"
    assert "verified_account_identifier" not in result["evidence"]
    assert "verified_at" not in result["evidence"]
    assert fake.actions == []


def test_prepare_upload_unknown_page_does_not_mutate_device(monkeypatch, local_media):
    video, cover = local_media
    fake = _FakeDevice([_hierarchy("无法识别的页面")])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_transfer_media", lambda *_args: pytest.fail("must not transfer"))

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption="测试文案",
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    assert result["status"] == "NEEDS_ATTENTION"
    assert result["evidence"]["reason"] == "ACCOUNT_VERIFICATION_REQUIRED"
    assert fake.actions == []


@pytest.mark.parametrize(
    "tokens", [("抖音号：expected-user", "关注", "私信"), ("抖音号：expected-user", "视频描述")]
)
def test_account_text_outside_own_profile_never_authorizes_upload(monkeypatch, local_media, tokens):
    video, cover = local_media
    fake = _FakeDevice([_hierarchy(*tokens)])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_transfer_media", lambda *_args: pytest.fail("must not transfer"))

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption="文案",
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    assert result["status"] == "NEEDS_ATTENTION"
    assert "verified_account_identifier" not in result["evidence"]
    assert fake.actions == []


def test_prepare_upload_navigates_home_to_profile_before_transfer(monkeypatch, local_media):
    video, cover = local_media
    fake = _FakeDevice(
        [
            _hierarchy("首页", "我", clickable={"我"}),
            _hierarchy("抖音号：expected-user", "作品", "编辑资料"),
        ]
    )
    _prepare_monkeypatch(monkeypatch, fake)
    transfers = []
    monkeypatch.setattr(
        device,
        "_transfer_media",
        lambda *_args: transfers.append(True) or ("/remote/video.mp4", "/remote/cover.jpg"),
    )

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption="测试文案",
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    assert result["status"] == "NEEDS_ATTENTION"
    assert result["evidence"]["reason"] == "CREATE_ENTRY_REQUIRED"
    assert result["evidence"]["verified_account_identifier"] == "expected-user"
    assert transfers == [True]
    assert fake.actions == [("click", {"resourceId": "id/1"})]


def test_editor_resume_requires_evidence_and_never_clicks_publish(monkeypatch, local_media):
    video, cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    caption = "完整标题与文案"
    fake = _FakeDevice([_hierarchy("作品描述", "发布", f"素材 {digest[:16]}", caption)])
    _prepare_monkeypatch(monkeypatch, fake)
    previous = {
        "serial": "ABC123",
        "platform": "douyin",
        "app_version": "31.2.0",
        "verified_account_identifier": "expected-user",
        "video_sha256": digest,
        "caption": caption,
        "verified_at": datetime.now(UTC).isoformat(),
    }

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption=caption,
        progress=lambda *_args: None,
        canceled=lambda: False,
        previous_evidence=previous,
    )

    assert result["status"] == "WAITING_USER"
    assert result["evidence"]["final_publish_clicked"] is False
    assert fake.actions == []


def test_editor_without_prior_account_evidence_stops(monkeypatch, local_media):
    video, cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    fake = _FakeDevice([_hierarchy("作品描述", "发布", digest[:16], "文案")])
    _prepare_monkeypatch(monkeypatch, fake)

    result = device.prepare_upload(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption="文案",
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    assert result["status"] == "NEEDS_ATTENTION"
    assert result["evidence"]["reason"] == "ACCOUNT_VERIFICATION_REQUIRED"
    assert fake.actions == []


def test_verify_xhs_operator_media_selection_is_read_only_and_returns_safe_proof(
    monkeypatch, local_media
):
    video, _cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    folder = f"lp_{digest[:16]}"
    remote = f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"
    fake = _FakeDevice(
        [_hierarchy("添加标题", "添加作品描述", "公开可见", "发笔记")],
        packages=["com.xingin.xhs"],
    )
    monkeypatch.setattr(device, "_require_connected", lambda _serial: {"serial": _serial})
    monkeypatch.setattr(device, "_package_version", lambda *_args: "9.46.0")
    monkeypatch.setattr(device, "_connect_device", lambda _serial: fake)
    monkeypatch.setattr(device, "_adb", lambda *_args, **_kwargs: f"{digest}  {remote}\n")
    monkeypatch.setattr(device, "ensure_unlocked", lambda *_args, **_kwargs: True)
    app_config = device.AppConfig(data_dir=video.parent / "workspace", _env_file=None)
    evidence = {
        "serial": "ABC123",
        "platform": "xiaohongshu",
        "app_version": "9.46.0",
        "verified_account_identifier": "expected-user",
        "verified_at": datetime.now(UTC).isoformat(),
        "video_sha256": digest,
        "caption": "审核标题 审核正文",
        "remote_video_path": remote,
        "reason": "MEDIA_SELECTION_REQUIRED",
        "page": "picker",
    }

    result = device.verify_operator_media_selection(
        serial="ABC123",
        platform="xiaohongshu",
        account_identifier="expected-user",
        expected_app_version="9.46.0",
        video_sha256=digest,
        caption="审核标题 审核正文",
        remote_video_path=remote,
        previous_evidence=evidence,
        app_config=app_config,
    )

    assert result == {"video_sha256": digest, "app_version": "9.46.0", "page": "editor"}
    assert "expected-user" not in str(result)
    assert fake.actions == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("expired", "MEDIA_CONTINUITY_INVALID"),
        ("version", "APP_VERSION_CHANGED"),
        ("path", "MEDIA_PATH_MISMATCH"),
        ("hash", "MEDIA_HASH_MISMATCH"),
        ("page", "MEDIA_EDITOR_REQUIRED"),
    ],
)
def test_verify_xhs_operator_media_selection_rejects_mismatches(
    monkeypatch, local_media, change, code
):
    video, _cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    folder = f"lp_{digest[:16]}"
    remote = f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"
    tokens = ("全部", "视频", "照片") if change == "page" else (
        "添加标题",
        "添加作品描述",
        "公开可见",
        "发笔记",
    )
    fake = _FakeDevice([_hierarchy(*tokens)], packages=["com.xingin.xhs"])
    monkeypatch.setattr(device, "_require_connected", lambda _serial: {"serial": _serial})
    monkeypatch.setattr(
        device,
        "_package_version",
        lambda *_args: "9.47.0" if change == "version" else "9.46.0",
    )
    monkeypatch.setattr(device, "_connect_device", lambda _serial: fake)
    monkeypatch.setattr(device, "ensure_unlocked", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        device,
        "_adb",
        lambda *_args, **_kwargs: f"{'0' * 64 if change == 'hash' else digest}  {remote}\n",
    )
    evidence = {
        "serial": "ABC123",
        "platform": "xiaohongshu",
        "app_version": "9.46.0",
        "verified_account_identifier": "expected-user",
        "verified_at": (
            datetime.now(UTC) - timedelta(minutes=31)
            if change == "expired"
            else datetime.now(UTC)
        ).isoformat(),
        "video_sha256": digest,
        "caption": "审核标题 审核正文",
        "remote_video_path": remote,
        "reason": "MEDIA_SELECTION_REQUIRED",
        "page": "picker",
    }

    with pytest.raises(IntegrationError) as error:
        device.verify_operator_media_selection(
            serial="ABC123",
            platform="xiaohongshu",
            account_identifier="expected-user",
            expected_app_version="9.46.0",
            video_sha256=digest,
            caption="审核标题 审核正文",
            remote_video_path="/sdcard/Movies/wrong.mp4" if change == "path" else remote,
            previous_evidence=evidence,
            app_config=device.AppConfig(data_dir=video.parent / "workspace", _env_file=None),
        )
    assert error.value.code == code
    assert fake.actions == []


def test_verify_xhs_media_selection_unlocks_sleeping_device_before_editor_check(
    monkeypatch, local_media
):
    video, _cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    folder = f"lp_{digest[:16]}"
    remote = f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"
    fake = _FakeDevice([], packages=["com.xingin.xhs"])
    fake.info["screenOn"] = False
    order = []
    app_config = device.AppConfig(data_dir=video.parent / "workspace", _env_file=None)
    monkeypatch.setattr(device, "_require_connected", lambda _serial: {"serial": _serial})
    monkeypatch.setattr(device, "_package_version", lambda *_args: "9.46.0")
    monkeypatch.setattr(device, "_connect_device", lambda _serial: fake)
    monkeypatch.setattr(device, "_adb", lambda *_args, **_kwargs: f"{digest}  {remote}\n")

    def unlock(serial, phone, canceled=None, app_config=None):
        assert serial == "ABC123" and phone is fake and canceled is None
        assert app_config is not None and app_config.data_dir == video.parent / "workspace"
        assert fake.info["screenOn"] is False
        order.append("unlock")
        fake.info["screenOn"] = True
        return True

    def page(phone, platform):
        assert phone is fake and platform == "xiaohongshu"
        assert fake.info["screenOn"] is True
        order.append("page")
        return "editor", "com.xingin.xhs", []

    monkeypatch.setattr(device, "ensure_unlocked", unlock)
    monkeypatch.setattr(device, "_guarded_page", page)
    evidence = {
        "serial": "ABC123",
        "platform": "xiaohongshu",
        "app_version": "9.46.0",
        "verified_account_identifier": "expected-user",
        "verified_at": datetime.now(UTC).isoformat(),
        "video_sha256": digest,
        "caption": "审核标题 审核正文",
        "remote_video_path": remote,
        "reason": "MEDIA_SELECTION_REQUIRED",
        "page": "picker",
    }

    result = device.verify_operator_media_selection(
        serial="ABC123",
        platform="xiaohongshu",
        account_identifier="expected-user",
        expected_app_version="9.46.0",
        video_sha256=digest,
        caption="审核标题 审核正文",
        remote_video_path=remote,
        previous_evidence=evidence,
        app_config=app_config,
    )

    assert result["page"] == "editor"
    assert order == ["unlock", "page"]
    assert fake.actions == []


def test_xhs_editor_resume_uses_exact_operator_media_confirmation_to_fill_caption(
    monkeypatch, local_media
):
    video, cover = local_media
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    title, description = "审核标题", "审核正文 #测试"
    caption = f"{title} {description}"
    first = (
        '<hierarchy><node text="添加标题" hint="添加标题" resource-id="id/title" '
        'class="android.widget.EditText" /><node text="添加作品描述" hint="添加作品描述" '
        'resource-id="id/body" class="android.widget.EditText" /><node text="公开可见" />'
        '<node text="发笔记" clickable="true" /></hierarchy>'
    )
    fake = _FakeDevice(
        [first, _hierarchy(title, description, "公开可见", "发笔记")],
        packages=["com.xingin.xhs", "com.xingin.xhs"],
    )
    monkeypatch.setattr(device, "_require_connected", lambda _serial: {"serial": _serial})
    monkeypatch.setattr(device, "_package_version", lambda *_args: "9.46.0")
    monkeypatch.setattr(device, "_connect_device", lambda _serial: fake)
    monkeypatch.setattr(device, "ensure_unlocked", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(device, "_WAIT_AFTER_NAVIGATION", 0)
    remote_checks = []
    monkeypatch.setattr(
        device,
        "_verify_remote_video_hash",
        lambda serial, remote, sha256: remote_checks.append((serial, remote, sha256)),
    )
    folder = f"lp_{digest[:16]}"
    remote = f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"
    confirmed_at = datetime.now(UTC).isoformat()
    previous = {
        "serial": "ABC123",
        "platform": "xiaohongshu",
        "app_version": "9.46.0",
        "verified_account_identifier": "expected-user",
        "verified_at": confirmed_at,
        "video_sha256": digest,
        "caption": caption,
        "remote_video_path": remote,
        "reason": "MEDIA_SELECTION_REQUIRED",
        "page": "picker",
        "operator_media_confirmation": {
            "confirmed": True,
            "notes": "已人工核验哈希、时长和预览",
            "video_sha256": digest,
            "app_version": "9.46.0",
            "confirmed_at": confirmed_at,
        },
    }

    # A later device error may replace the display reason, but must not erase
    # the already-audited media confirmation used by a safe resume.
    previous["reason"] = "无法安全填写发布文案"
    previous["error_code"] = "DEVICE_ACTION_FAILED"

    result = device.prepare_upload(
        serial="ABC123",
        platform="xiaohongshu",
        account_identifier="expected-user",
        video_path=str(video),
        cover_path=str(cover),
        caption=caption,
        title=title,
        description=description,
        progress=lambda *_args: None,
        canceled=lambda: False,
        previous_evidence=previous,
    )

    assert result["status"] == "WAITING_USER"
    assert result["evidence"]["operator_media_confirmation"]["video_sha256"] == digest
    assert remote_checks == [("ABC123", remote, digest)]
    assert fake.actions == [
        ("set_text", {"resourceId": "id/title"}, title),
        ("set_text", {"resourceId": "id/body"}, description),
    ]


def test_prepare_upload_honors_cancellation_before_device_access(monkeypatch, local_media):
    video, cover = local_media
    monkeypatch.setattr(device, "_require_connected", lambda _serial: pytest.fail("must not inspect device"))
    with pytest.raises(CanceledError):
        device.prepare_upload(
            serial="ABC123",
            platform="douyin",
            account_identifier="expected-user",
            video_path=str(video),
            cover_path=str(cover),
            caption="文案",
            progress=lambda *_args: None,
            canceled=lambda: True,
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.2w", 12_000), ("2.5万", 25_000), ("3K", 3_000), ("4千+", 4_000), ("1.1亿", 110_000_000)],
)
def test_metric_number_parses_abbreviations(raw, expected):
    assert device._metric_number(raw) == expected


def test_metric_parser_keeps_missing_values_null_and_raw_evidence():
    values, raw = device._parse_metrics(["播放 1.2w", "点赞", "345", "评论不可见"])
    assert values == {"views": 12_000, "likes": 345, "comments": None, "favorites": None, "shares": None}
    assert raw == {"views": "1.2w", "likes": "345"}


def test_douyin_native_counts_ignore_caption_claims_and_word_only_labels():
    fake = _FakeDevice(
        [
            '<hierarchy><node text="播放100万 点赞5000 评论300" resource-id="com.ss.android.ugc.aweme:id/desc" /><node text="赞" resource-id="com.ss.android.ugc.aweme:id/gt=" /><node text="收藏" resource-id="com.ss.android.ugc.aweme:id/d=i" /></hierarchy>'
        ]
    )
    values, raw = device._douyin_visible_counts(device._hierarchy(fake))
    assert all(value is None for value in values.values())
    assert raw == {}
    fake = _FakeDevice(
        [
            '<hierarchy><node text="1.2万" resource-id="com.ss.android.ugc.aweme:id/gt=" /><node text="0" resource-id="com.ss.android.ugc.aweme:id/d=i" /></hierarchy>'
        ]
    )
    values, raw = device._douyin_visible_counts(device._hierarchy(fake))
    assert values["likes"] == 12000 and values["favorites"] == 0 and values["views"] is None
    assert raw == {"likes": "1.2万", "favorites": "0"}


def test_collect_metrics_verifies_account_and_work_then_parses(monkeypatch):
    fake = _FakeDevice(
        [
            _hierarchy("抖音号：expected-user", "作品", "编辑资料"),
            _hierarchy("内容标题", "作品 ID-9988", "播放 1.2万", "点赞 500", "评论 7"),
        ]
    )
    _prepare_monkeypatch(monkeypatch, fake)
    adb_calls = []
    monkeypatch.setattr(device, "_adb", lambda *args, **_kwargs: adb_calls.append(args) or "")

    result = device.collect_metrics(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        work_url="https://www.douyin.com/video/ID-9988",
        work_id="ID-9988",
        content_title="内容标题",
    )

    assert result["views"] == 12_000
    assert result["likes"] == 500
    assert result["comments"] == 7
    assert result["favorites"] is None
    assert result["shares"] is None
    assert result["raw"]["observed_values"]["views"] == "1.2万"
    assert any("android.intent.action.VIEW" in call for call in adb_calls)


def test_collect_metrics_rejects_wrong_account_before_navigation(monkeypatch):
    fake = _FakeDevice([_hierarchy("抖音号：other-user")])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_adb", lambda *_args, **_kwargs: pytest.fail("must not navigate"))

    with pytest.raises(IntegrationError) as caught:
        device.collect_metrics(
            serial="ABC123",
            platform="douyin",
            account_identifier="expected-user",
            work_url="https://www.douyin.com/video/ID-9988",
            work_id="ID-9988",
            content_title="内容标题",
        )

    assert caught.value.code == "ACCOUNT_MISMATCH"


@pytest.mark.parametrize("copied", [None, "https://www.douyin.com/video/123456"])
def test_work_link_requires_a_fresh_copy_and_restores_marker(monkeypatch, copied):
    fake = _FakeDevice([])
    fake.clipboard = "https://www.douyin.com/video/999999"
    monkeypatch.setattr(device, "_WAIT_AFTER_NAVIGATION", 0)
    monkeypatch.setattr(device, "_guarded_page", lambda *args: ("unknown", "com.ss.android.ugc.aweme", []))

    def click(_fake, _nodes, labels):
        if "分享链接" in labels and copied:
            fake.clipboard = copied
        return True

    monkeypatch.setattr(device, "_click_unique", click)
    fallback = "https://www.douyin.com/video/123456"
    if copied:
        work_id, _ = device._douyin_work_link(fake, [], fallback)
        assert work_id == "123456"
    else:
        with pytest.raises(IntegrationError) as error:
            device._douyin_work_link(fake, [], fallback)
        assert error.value.code == "WORK_LINK_UNVERIFIED"
    assert fake.clipboard == fallback


@pytest.mark.parametrize("observed_id", ["123456", "999999"])
def test_calibrated_metrics_require_exact_copied_work_id(monkeypatch, observed_id):
    fake = _FakeDevice(
        [
            _hierarchy("抖音号：expected-user", "作品", "编辑资料"),
            _hierarchy("相同标题", "播放100万"),
        ]
    )
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *args: "40.4.0")
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        return "com.ss.android.ugc.aweme/.app.DeepLinkHandlerActivity"

    monkeypatch.setattr(device, "_adb", adb)
    monkeypatch.setattr(device, "_douyin_work_link", lambda *args: (observed_id, []))
    arguments = {
        "serial": "ABC123",
        "platform": "douyin",
        "account_identifier": "expected-user",
        "work_url": "https://www.douyin.com/video/123456",
        "work_id": "123456",
        "content_title": "相同标题",
    }
    if observed_id != "123456":
        with pytest.raises(IntegrationError) as error:
            device.collect_metrics(**arguments)
        assert error.value.code == "WORK_MISMATCH"
    else:
        result = device.collect_metrics(**arguments)
        assert result["raw"]["verification"] == "device_share_link"
        assert all(result[name] is None for name in ("views", "likes", "comments", "favorites", "shares"))
    assert any("snssdk1128://aweme/detail/123456" in call for call in calls)
