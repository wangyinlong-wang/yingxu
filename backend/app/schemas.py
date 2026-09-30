from __future__ import annotations

import ipaddress
import re
from typing import Annotated, ClassVar, Literal
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def http_url(value: str) -> str:
    parsed = urlsplit(value)
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请输入不含账户凭据的 HTTP(S) 地址")
    if any(ord(char) < 32 for char in value):
        raise ValueError("地址包含控制字符")
    return value


def public_http_url(value: str) -> str:
    value = http_url(value)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal", "169.254.169.254"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("只允许公网 HTTP(S) 地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("只允许公网 HTTP(S) 地址") from None
    else:
        if not address.is_global:
            raise ValueError("只允许公网 HTTP(S) 地址")
    return value


SourceKind = Literal["rss", "hackernews", "x", "x_tikhub", "x_tikhub_search", "html_list"]
X_PROFILE_KINDS = {"x", "x_tikhub"}
PAID_SOURCE_KINDS = {"x_tikhub", "x_tikhub_search"}


class SourceOptions(Input):
    path_prefix: str | None = Field(None, max_length=200, pattern=r"^/[^\s?#]*$")


def clean_keywords(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    result: list[str] = []
    for item in value:
        keyword = re.sub(r"\s+", " ", item).strip()
        if not keyword:
            continue
        if len(keyword) > 50 or any(ord(char) < 32 for char in keyword):
            raise ValueError("关注词每个不超过 50 个字符")
        if keyword.casefold() not in {existing.casefold() for existing in result}:
            result.append(keyword)
    if len(result) > 30:
        raise ValueError("关注词最多 30 个")
    return result


def canonical_source_url_for(kind: str, url: str) -> str:
    if kind in X_PROFILE_KINDS:
        return canonical_x_profile_url(url)
    if kind == "x_tikhub_search":
        return canonical_x_search_url(url)
    return url


class SourceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: SourceKind = "rss"
    url: str = Field(max_length=2000)
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list, max_length=60)
    options: SourceOptions = Field(default_factory=SourceOptions)
    _url = field_validator("url")(http_url)
    _keywords = field_validator("keywords")(clean_keywords)

    @model_validator(mode="after")
    def canonical_source_url(self):
        self.url = canonical_source_url_for(self.kind, self.url)
        if self.options.path_prefix and self.kind != "html_list":
            raise ValueError("只有网页列表来源可以设置文章路径前缀")
        return self


class SourcePatch(Input):
    name: str | None = Field(None, min_length=1, max_length=200)
    kind: Literal["x", "x_tikhub"] | None = None
    url: str | None = Field(None, max_length=2000)
    enabled: bool | None = None
    keywords: list[str] | None = Field(None, max_length=60)
    options: SourceOptions | None = None
    _keywords = field_validator("keywords")(clean_keywords)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return http_url(value) if value is not None else value


class SourceCollect(Input):
    expected_kind: SourceKind | None = None


class SourcePresetApply(Input):
    ids: list[str] = Field(min_length=1, max_length=50)


def canonical_x_search_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        params = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        params = {}
    queries, modes = params.get("q", []), params.get("f", ["live"])
    query = re.sub(r"\s+", " ", queries[0]).strip() if len(queries) == 1 else ""
    if (
        host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or parsed.port is not None
        or parsed.path.rstrip("/") != "/search"
        or parsed.fragment
        or set(params) - {"q", "f", "src"}
        or not query
        or len(query) > 500
        or len(modes) != 1
        or modes[0] not in {"live", "top"}
    ):
        raise ValueError("X 搜索来源必须是 https://x.com/search?q=关键词 形式，f 可选 live（最新）或 top（热门）")
    return f"https://x.com/search?q={quote(query, safe='')}&f={modes[0]}"


def canonical_x_profile_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("X 来源必须是公开账号主页地址")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 1
        or parts[0].lower() in {"status", "search"}
        or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0])
    ):
        raise ValueError("X 来源地址必须只包含一个有效账号名")
    return f"https://x.com/{parts[0]}"


class ContentCreate(Input):
    template_id: str | None = None
    preset_id: str = Field(default="tech_brief_video_v1", min_length=1, max_length=100)


class TopicImport(Input):
    source_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=12000)

    @field_validator("url")
    @classmethod
    def validate_article_url(cls, value):
        value = public_http_url(value)
        parsed = urlsplit(value)
        if parsed.hostname.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,15}|i)/status/([0-9]+)/?", parsed.path)
            if not match or parsed.port is not None:
                raise ValueError("请填写 X 原帖链接，而不是账号主页")
            return f"https://x.com/{match[1].lower()}/status/{match[2]}"
        return value


class EstimatedCalls(Input):
    text: int = Field(ge=0, le=100)
    image: int = Field(ge=0, le=100)
    speech: int = Field(ge=0, le=100)


class BriefConcept(Input):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    hook: str = Field(min_length=1, max_length=100)
    narrative_structure: Literal[
        "problem_solution",
        "comparison",
        "myth_busting",
        "timeline",
        "tutorial",
        "data_narrative",
        "story",
    ]
    visual_approach: str = Field(min_length=1, max_length=3000)
    core_message: str = Field(min_length=1, max_length=3000)
    why_this_works: str = Field(min_length=1, max_length=3000)
    estimated_calls: EstimatedCalls
    estimated_cost_usd: None = None


class ProductionBrief(Input):
    version: Literal["1.0"] = "1.0"
    target_audience: str = Field(min_length=1, max_length=1000)
    content_goal: str = Field(min_length=1, max_length=2000)
    target_platform: Literal["douyin", "xiaohongshu", "both"] = "both"
    quality_floor: Literal["presentable"] = "presentable"
    concepts: list[BriefConcept] = Field(min_length=3, max_length=3)
    selected_concept_id: str | None = Field(None, max_length=100)
    selected_at: str | None = Field(None, max_length=40)

    @model_validator(mode="after")
    def valid_selection(self):
        identifiers = [concept.id for concept in self.concepts]
        if len(set(identifiers)) != 3:
            raise ValueError("创意方案标识必须唯一")
        if self.selected_concept_id is not None and self.selected_concept_id not in identifiers:
            raise ValueError("已选方案不存在")
        if bool(self.selected_concept_id) != bool(self.selected_at):
            raise ValueError("方案选择标识与时间必须同时存在")
        return self


class BriefSelect(Input):
    version: int = Field(ge=1)
    concept_id: str = Field(min_length=1, max_length=100)


class Fact(Input):
    claim: str = Field(min_length=1, max_length=2000)
    source_url: str = Field(default="", max_length=2000)
    verified: bool = False
    notes: str = Field(default="", max_length=4000)

    @field_validator("source_url")
    @classmethod
    def valid_source(cls, value):
        return http_url(value) if value else value


PLATFORM_TEXT_LIMITS = {
    "douyin": {"title_units": 60, "title_han_chars": 30, "description_chars": 1000},
    "xiaohongshu": {"title_units": 40, "title_han_chars": 20, "description_chars": 1000},
}
AI_DISCLOSURE_PATTERN = re.compile(
    r"(?:AI\s*(?:辅助制作|生成|创作)|人工智能(?:辅助)?(?:生成|制作|创作)|AIGC)", re.IGNORECASE
)


def platform_title_units(value: str) -> int:
    """平台标题计数：中文和其他全角字符计 2，ASCII 字符计 1。"""
    return sum(1 if ord(character) < 128 else 2 for character in value)


def platform_description(text: PlatformText | dict) -> str:
    value = text.model_dump() if isinstance(text, PlatformText) else text
    tags = " ".join("#" + str(tag).lstrip("#") for tag in value.get("tags", []))
    return "\n".join(part for part in (str(value.get("description", "")), tags) if part)


class PlatformText(Input):
    platform: ClassVar[str] = ""
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def platform_lengths(self):
        limits = PLATFORM_TEXT_LIMITS[self.platform]
        if platform_title_units(self.title) > limits["title_units"]:
            raise ValueError(
                f"{self.platform_label}标题最多{limits['title_han_chars']}个中文字符"
                f"（或{limits['title_units']}个半角字符）"
            )
        if len(platform_description(self)) > limits["description_chars"]:
            raise ValueError(
                f"{self.platform_label}发布正文（描述与话题合计）最多"
                f"{limits['description_chars']}个字符"
            )
        return self

    @property
    def platform_label(self) -> str:
        return {"douyin": "抖音", "xiaohongshu": "小红书"}[self.platform]


class DouyinPlatformText(PlatformText):
    platform: ClassVar[str] = "douyin"


class XiaohongshuPlatformText(PlatformText):
    platform: ClassVar[str] = "xiaohongshu"


class Platforms(Input):
    douyin: DouyinPlatformText = Field(default_factory=DouyinPlatformText)
    xiaohongshu: XiaohongshuPlatformText = Field(default_factory=XiaohongshuPlatformText)


class Script(Input):
    angle: str = Field(default="", max_length=3000)
    selection_reason: str = Field(default="", max_length=3000)
    facts: list[Fact] = Field(default_factory=list, max_length=40)
    platforms: Platforms = Field(default_factory=Platforms)


def validate_publishable_script(value: Script | dict) -> Script:
    script = value if isinstance(value, Script) else Script.model_validate(value)
    for platform in ("douyin", "xiaohongshu"):
        text = getattr(script.platforms, platform)
        if not text.title:
            raise ValueError(f"{text.platform_label}标题不能为空")
        if not text.description:
            raise ValueError(f"{text.platform_label}描述不能为空")
        if not AI_DISCLOSURE_PATTERN.search(text.description):
            raise ValueError(f"{text.platform_label}描述需明确标注“AI辅助制作”")
    return script


class Scene(Input):
    id: str = Field(min_length=1, max_length=100)
    order: int = Field(ge=0, le=30)
    narration: str = Field(default="", max_length=500)
    caption: str = Field(default="", max_length=100)
    highlight_terms: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=8
    )
    image_prompt: str = Field(default="", max_length=3000)
    image_asset_id: str | None = None
    video_asset_id: str | None = None
    audio_asset_ids: list[str] = Field(default_factory=list, max_length=30)
    motion: Literal["auto", "none", "push_in", "pull_out", "pan_left", "pan_right"] = "auto"
    transition: Literal["fade", "cut"] = "fade"
    scene_type: Literal["image", "text_card", "stat_card", "quote_card"] = "image"
    visual_data: dict[str, str] = Field(default_factory=dict)
    hero_moment: bool = False
    shot_intent: str = Field(default="", max_length=1000)

    @field_validator("visual_data")
    @classmethod
    def safe_visual_data(cls, value):
        allowed = {"headline", "body", "value", "source_url", "emphasis"}
        if set(value) - allowed:
            raise ValueError("卡片视觉数据包含不支持的字段")
        cleaned = {key: str(item).strip() for key, item in value.items()}
        for text in cleaned.values():
            if len(text) > 2000 or any(ord(character) < 32 and character not in "\n\t" for character in text):
                raise ValueError("卡片文字超出安全长度或包含控制字符")
            if (
                re.search(r"<\s*/?\s*[a-z][^>]*>", text, re.IGNORECASE)
                or re.search(r"\{\\[^}]+\}", text)
                or "$(" in text
                or "`" in text
                or re.search(r"(?:^|[\s'\"])(?:\.\./|/\w|[A-Za-z]:\\|file://)", text)
            ):
                raise ValueError("卡片文字只能是纯文本，不接受标记、命令、路径或表达式")
        source_url = cleaned.get("source_url", "")
        if source_url:
            cleaned["source_url"] = public_http_url(source_url)
        return cleaned

    @model_validator(mode="after")
    def literal_highlights(self, info: ValidationInfo):
        self.highlight_terms = list(dict.fromkeys(self.highlight_terms))
        invalid = [term for term in self.highlight_terms if term not in self.narration and term not in self.caption]
        if invalid:
            warnings = info.context.get("generated_highlight_warnings") if info.context else None
            if warnings is None:
                raise ValueError("黄色关键词必须出现在本镜头的旁白或画面标题中")
            warnings.append({"scene_id": self.id, "terms": invalid})
            self.highlight_terms = [term for term in self.highlight_terms if term not in invalid]
        return self

    @model_validator(mode="after")
    def required_card_text(self):
        if self.scene_type == "text_card" and not any(
            self.visual_data.get(key) for key in ("headline", "body")
        ):
            raise ValueError("文字卡需要 headline 或 body")
        if self.scene_type == "stat_card" and not self.visual_data.get("value"):
            raise ValueError("数据卡需要 value")
        if self.scene_type == "quote_card" and not self.visual_data.get("body"):
            raise ValueError("引语卡需要 body")
        return self


class ContentPatch(Input):
    version: int = Field(ge=1)
    title: str | None = Field(None, min_length=1, max_length=1000)
    script: Script | None = None
    scenes: list[Scene] | None = Field(None, max_length=20)
    template_id: str | None = None
    preset_id: str | None = Field(None, min_length=1, max_length=100)


class Generation(Input):
    stage: Literal[
        "all",
        "brief",
        "script",
        "assets",
        "render",
        "local_voice",
        "local_avatar_all",
        "motion",
        "images",
        "video",
    ] = "all"
    scene_id: str | None = Field(default=None, min_length=1, max_length=100)
    clip_duration: int | None = Field(default=None, ge=4, le=15)
    clip_resolution: Literal["768P", "2K"] | None = None
    reference_image_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    reference_audio_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    avatar_layout: Literal["corner", "fullscreen"] | None = None

    @model_validator(mode="after")
    def scene_only_for_images(self):
        if (self.reference_image_asset_id or self.reference_audio_asset_id) and self.stage != "video":
            raise ValueError("参考素材仅可用于明确请求的视频生成")
        if self.reference_audio_asset_id and not self.reference_image_asset_id:
            raise ValueError("声音参考须与图片参考同时提供")
        if (self.clip_duration is None) != (self.clip_resolution is None):
            raise ValueError("视频时长与分辨率必须同时提供")
        fullscreen_avatar = self.stage == "local_avatar_all" and self.avatar_layout == "fullscreen"
        if self.avatar_layout is not None and self.stage != "local_avatar_all":
            raise ValueError("数字人布局仅可用于本人声音与数字人全流程")
        if fullscreen_avatar and self.clip_duration is None:
            raise ValueError("全屏数字人必须确认视频时长与分辨率")
        if self.clip_duration is not None and self.stage != "video" and not fullscreen_avatar:
            raise ValueError("视频时长与分辨率仅可用于生成视频")
        if self.stage == "video" and self.scene_id is None:
            raise ValueError("生成视频必须指定一个分镜")
        if self.scene_id is not None and self.stage not in {"images", "video"}:
            raise ValueError("scene_id 仅可用于重绘配图或生成视频")
        return self


class MotionDirection(Input):
    id: str = Field(min_length=1, max_length=100)
    motion: Literal["none", "push_in", "pull_out", "pan_left", "pan_right"]
    transition: Literal["fade", "cut"]


class MotionPlan(Input):
    scenes: list[MotionDirection] = Field(min_length=1, max_length=20)


class MotionInputScene(Input):
    id: str = Field(min_length=1, max_length=100)
    caption: str = Field(default="", max_length=100)
    narration: str = Field(min_length=1, max_length=500)
    image_prompt: str = Field(default="", max_length=3000)


class MotionInput(Input):
    title: str = Field(default="", max_length=1000)
    scenes: list[MotionInputScene] = Field(min_length=1, max_length=20)


class ComplianceChecks(Input):
    source_rights_confirmed: bool
    no_unauthorized_material: bool
    claims_not_exaggerated: bool
    no_offplatform_diversion: bool
    no_unverifiable_guarantees: bool
    ai_disclosure_confirmed: bool
    platform_rules_checked: bool
    visuals_manually_reviewed: bool


class Review(Input):
    version: int = Field(ge=1)
    notes: str = Field(default="", max_length=5000)
    auto_publish_expected: bool | None = None
    compliance_confirmed: bool = False
    compliance_checks: ComplianceChecks | None = None


class DemandScores(Input):
    demand: int = Field(ge=0, le=5)
    production: int = Field(ge=0, le=5)
    delivery: int = Field(ge=0, le=5)
    presentation: int = Field(ge=0, le=5)
    compliance: int = Field(ge=0, le=5)
    iteration: int = Field(ge=0, le=5)


class DemandSignalCreate(Input):
    title: str = Field(min_length=1, max_length=1000)
    source_kind: Literal["search", "comment", "forum", "customer", "manual"]
    source_url: str | None = Field(None, max_length=2000)
    evidence: str = Field(default="", max_length=12000)
    scores: DemandScores
    status: Literal["CANDIDATE", "VALIDATED", "REJECTED"] = "CANDIDATE"

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value):
        return public_http_url(value) if value else value


class DemandSignalPatch(Input):
    title: str | None = Field(None, min_length=1, max_length=1000)
    source_kind: Literal["search", "comment", "forum", "customer", "manual"] | None = None
    source_url: str | None = Field(None, max_length=2000)
    evidence: str | None = Field(None, max_length=12000)
    scores: DemandScores | None = None
    status: Literal["CANDIDATE", "VALIDATED", "REJECTED"] | None = None

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value):
        return public_http_url(value) if value else value


class BenchmarkAnalysis(Input):
    audience: str = Field(default="", max_length=4000)
    pain_point: str = Field(default="", max_length=4000)
    hook: str = Field(default="", max_length=4000)
    cover: str = Field(default="", max_length=4000)
    structure: str = Field(default="", max_length=4000)
    cta: str = Field(default="", max_length=4000)
    metrics: str = Field(default="", max_length=4000)
    learnings: str = Field(default="", max_length=4000)
    prohibited_copy: str = Field(default="", max_length=4000)


class BenchmarkCreate(Input):
    platform: Literal["douyin", "xiaohongshu", "other"]
    account_name: str = Field(min_length=1, max_length=200)
    content_url: str | None = Field(None, max_length=2000)
    title: str = Field(min_length=1, max_length=1000)
    content_type: Literal["video", "image_note", "other"]
    analysis: BenchmarkAnalysis

    @field_validator("content_url")
    @classmethod
    def valid_content_url(cls, value):
        return public_http_url(value) if value else value


class BenchmarkPatch(Input):
    platform: Literal["douyin", "xiaohongshu", "other"] | None = None
    account_name: str | None = Field(None, min_length=1, max_length=200)
    content_url: str | None = Field(None, max_length=2000)
    title: str | None = Field(None, min_length=1, max_length=1000)
    content_type: Literal["video", "image_note", "other"] | None = None
    analysis: BenchmarkAnalysis | None = None

    @field_validator("content_url")
    @classmethod
    def valid_content_url(cls, value):
        return public_http_url(value) if value else value


def _unique_ids(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    if len(set(values)) != len(values):
        raise ValueError("关联标识不能重复")
    return values


def _observation_window(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or not re.fullmatch(r"[\w\u4e00-\u9fff .:+-]+", value):
        raise ValueError("观察窗口标签无效")
    return value


class ExperimentCreate(Input):
    name: str = Field(min_length=1, max_length=500)
    hypothesis: str = Field(min_length=1, max_length=12000)
    variable: Literal["angle", "title", "cover", "format", "publish_time", "other"]
    status: Literal["DRAFT", "RUNNING", "COMPLETED", "CANCELED"] = "DRAFT"
    content_ids: list[str] = Field(default_factory=list, max_length=100)
    publication_ids: list[str] = Field(default_factory=list, max_length=100)
    observation_windows: list[str] = Field(default_factory=list, max_length=30)
    conclusion: str = Field(default="", max_length=12000)
    next_action: Literal[
        "continue", "revise_title", "revise_cover", "change_angle", "stop", "undecided"
    ] = "undecided"
    _content_ids = field_validator("content_ids")(_unique_ids)
    _publication_ids = field_validator("publication_ids")(_unique_ids)
    _windows = field_validator("observation_windows", mode="before")(
        lambda values: [_observation_window(value) for value in values]
    )


class ExperimentPatch(Input):
    name: str | None = Field(None, min_length=1, max_length=500)
    hypothesis: str | None = Field(None, min_length=1, max_length=12000)
    variable: Literal["angle", "title", "cover", "format", "publish_time", "other"] | None = None
    status: Literal["DRAFT", "RUNNING", "COMPLETED", "CANCELED"] | None = None
    content_ids: list[str] | None = Field(None, max_length=100)
    publication_ids: list[str] | None = Field(None, max_length=100)
    observation_windows: list[str] | None = Field(None, max_length=30)
    conclusion: str | None = Field(None, max_length=12000)
    next_action: Literal[
        "continue", "revise_title", "revise_cover", "change_angle", "stop", "undecided"
    ] | None = None
    _content_ids = field_validator("content_ids")(_unique_ids)
    _publication_ids = field_validator("publication_ids")(_unique_ids)
    _windows = field_validator("observation_windows", mode="before")(
        lambda values: None if values is None else [_observation_window(value) for value in values]
    )


class AccountInput(Input):
    platform: Literal["douyin", "xiaohongshu"]
    display_name: str = Field(min_length=1, max_length=200)
    account_identifier: str = Field(min_length=1, max_length=200)
    channel: Literal["android", "browser"] = "android"
    device_serial: str = Field(default="", max_length=200, pattern=r"^[A-Za-z0-9._:-]*$")

    @model_validator(mode="after")
    def channel_binding(self):
        if self.channel == "browser":
            self.device_serial = f"browser:{self.platform}"
        elif not self.device_serial or self.device_serial.startswith("browser:"):
            raise ValueError("Android 通道需要有效的设备序列号")
        return self


class DeviceInspect(Input):
    platform: Literal["douyin", "xiaohongshu"]


class WirelessAddress(Input):
    ip: str = Field(min_length=7, max_length=15)

    @field_validator("ip")
    @classmethod
    def local_device_ip(cls, value: str) -> str:
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            raise ValueError("请输入手机无线调试页面显示的有效 IP 地址") from None
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
            raise ValueError("仅支持同一局域网内的私有 IP 地址")
        return str(address)


class WirelessPairInput(WirelessAddress):
    pairing_port: int = Field(ge=1, le=65535)
    connection_port: int | None = Field(default=None, ge=1, le=65535)
    pairing_code: SecretStr

    @field_validator("pairing_code")
    @classmethod
    def six_digit_pairing_code(cls, value: SecretStr) -> SecretStr:
        code = value.get_secret_value()
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("请输入手机当前显示的六位数字配对码")
        return value


class WirelessConnectInput(WirelessAddress):
    connection_port: int = Field(ge=1, le=65535)


class PublicationCreate(Input):
    content_id: str
    content_version: int = Field(ge=1)
    account_id: str


class PublicationConfirm(Input):
    work_url: str = Field(min_length=1, max_length=2000)
    work_id: str | None = Field(None, max_length=200)
    confirmed: Literal[True]
    notes: str = Field(default="", max_length=5000)
    _url = field_validator("work_url")(http_url)


class PublicationMediaSelectionConfirm(Input):
    confirmed: Literal[True]
    notes: str = Field(min_length=1, max_length=5000)


class Note(Input):
    notes: str = Field(default="", max_length=5000)


class PublicationCancel(Note):
    confirmed_not_published: bool = False


class ResolveJob(Input):
    resolution: Literal["not_completed", "cancel"]
    confirmed: Literal[True]
    notes: str = Field(min_length=1, max_length=5000)


class MetricInput(Input):
    publication_id: str
    views: int | None = Field(None, ge=0, le=10**15)
    likes: int | None = Field(None, ge=0, le=10**15)
    comments: int | None = Field(None, ge=0, le=10**15)
    favorites: int | None = Field(None, ge=0, le=10**15)
    shares: int | None = Field(None, ge=0, le=10**15)
    observed_at: str | None = None
    notes: str = Field(default="", max_length=5000)


class TemplateInput(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=3000)
    prompt: str = Field(min_length=1, max_length=12000)
    style: Literal["brief", "explain"] = "brief"


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def model_base_url(value: str, *, local: bool) -> str:
    """Shared Base URL policy: no credentials/query/fragment/metadata; local profiles are loopback-only."""

    if not value:
        return value
    parsed = urlsplit(http_url(value))
    if parsed.query or parsed.fragment:
        raise ValueError("模型地址不能包含查询参数或片段")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise ValueError("不允许访问元数据服务")
    if local:
        if host not in LOOPBACK_HOSTS:
            raise ValueError("本地模型服务地址只能使用 localhost、127.0.0.1 或 ::1")
    elif parsed.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ValueError("远程模型服务必须使用 HTTPS")
    return value.rstrip("/")


class EndpointConfig(Input):
    """Endpoint fields shared by the third-party and loopback model profiles."""

    protocol: str = "openai"
    language_type: str = Field(default="Chinese", min_length=1, max_length=80)
    max_completion_tokens: int | None = Field(default=None, ge=256, le=32768)
    enable_thinking: bool | None = None
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    voice: str = Field(default="alloy", min_length=1, max_length=100)
    size: str = Field(default="1024x1536", max_length=30)
    json_mode: bool = False
    enabled: bool = False
    watermark: bool = False
    instruction: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def configured_if_enabled(self):
        if self.enabled and (not self.base_url or not self.model):
            raise ValueError("启用模型前需填写地址和模型名")
        return self


class LocalModelConfig(EndpointConfig):
    """Loopback OpenAI-compatible profile; keys are optional and never fall back to environment."""

    protocol: Literal["openai"] = "openai"

    @field_validator("base_url")
    @classmethod
    def validate_local_base(cls, value):
        return model_base_url(value, local=True)


class ModelConfig(EndpointConfig):
    protocol: Literal["openai", "ark", "dashscope", "minimax_h3"] = "openai"
    source: Literal["third_party", "local", "builtin"] = "third_party"
    local: LocalModelConfig = Field(default_factory=LocalModelConfig)

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value):
        return model_base_url(value, local=False)


class VideoConfig(Input):
    renderer: Literal["ffmpeg", "hypit"] = "ffmpeg"
    clip_duration: int = Field(default=6, ge=4, le=15)
    clip_resolution: Literal["768P", "2K"] = "768P"
    width: int = Field(default=1080, ge=320, le=2160, multiple_of=2)
    height: int = Field(default=1920, ge=320, le=3840, multiple_of=2)
    fps: int = Field(default=30, ge=24, le=60)
    target_seconds: int = Field(default=60, ge=15, le=180)
    timing_mode: Literal["natural", "fit_target"] = "natural"
    font_path: str = Field(default="", max_length=2000)
    bgm_asset_id: str | None = None
    logo_asset_id: str | None = None


class Limits(Input):
    daily_calls: int = Field(default=40, ge=1, le=500)
    daily_videos: int = Field(default=1, ge=1, le=20)


class PublishingConfig(Input):
    auto_publish_after_approval: bool = False


class LocalAIConfig(Input):
    voice_enabled: bool = False
    avatar_enabled: bool = False
    voice_model: Literal["qwen3", "chatterbox"] = "qwen3"
    emotion_strength: float = Field(default=0.65, ge=0, le=1, allow_inf_nan=False)
    reference_audio_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    reference_text: str = Field(default="", max_length=2000)
    portrait_asset_id: str | None = Field(default=None, min_length=1, max_length=36)


class AllSettings(Input):
    models: dict[str, ModelConfig]
    video: VideoConfig
    limits: Limits
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    local_ai: LocalAIConfig = Field(default_factory=LocalAIConfig)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("请选择有效的 IANA 时区") from None
        return value

    @field_validator("models")
    @classmethod
    def capabilities(cls, value):
        if set(value) != {"text", "image", "speech", "video"}:
            raise ValueError("模型能力必须为 text、image、speech、video")
        allowed = {"text": {"openai"}, "image": {"openai", "ark"}, "speech": {"openai", "dashscope"}, "video": {"minimax_h3"}}
        for kind, model in value.items():
            if kind == "video" and model.source != "third_party":
                raise ValueError("视频模型目前仅支持第三方 MiniMax H3")
            if model.protocol not in allowed[kind]:
                raise ValueError("模型协议与能力不匹配")
            if model.source == "builtin" and kind != "speech":
                raise ValueError("内置本地声音引擎仅适用于语音能力")
        return value


class TikhubKeyAction(Input):
    """Dedicated TikHub credential body; the key is stored in the Keychain and never echoed."""

    action: Literal["set", "clear"]
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "set" and (self.api_key is None or not self.api_key.get_secret_value()):
            raise ValueError("保存密钥时必须提供 API Key")
        return self


class ModelKeyAction(Input):
    """Dedicated credential endpoint body; API keys never travel inside PATCH /settings."""

    source: Literal["third_party", "local"]
    base_url: str = Field(min_length=1, max_length=2000)
    action: Literal["set", "clear"]
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def valid_action(self):
        self.base_url = model_base_url(self.base_url, local=self.source == "local")
        if self.action == "set" and (self.api_key is None or not self.api_key.get_secret_value()):
            raise ValueError("保存密钥时必须提供 API Key")
        return self


class LocalAITranscribe(Input):
    asset_id: str = Field(min_length=1, max_length=36)


class LocalAIPreview(Input):
    kind: Literal["voice", "avatar"]
    text: str = Field(default="你好，这是我的本地声音预览。", min_length=1, max_length=500)
    audio_asset_id: str | None = Field(default=None, min_length=1, max_length=36)
    voice_model: Literal["qwen3", "chatterbox"] | None = None
    emotion_strength: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def voice_options_only_for_voice(self):
        if self.kind != "voice" and (self.voice_model is not None or self.emotion_strength is not None):
            raise ValueError("声音模型与情感强度仅可用于声音预览")
        return self


class ScheduleInput(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["pipeline", "metrics", "collect"]
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str
    enabled: bool = False
    daily_limit: int = Field(default=1, ge=1, le=20)
    interval_minutes: int | None = Field(default=None, ge=30, le=1440)
    include_paid: bool = False
    _zone = field_validator("timezone")(AllSettings.valid_zone.__func__)

    @model_validator(mode="after")
    def collect_interval(self):
        if self.kind == "collect":
            self.interval_minutes = self.interval_minutes or 180
        else:
            self.interval_minutes, self.include_paid = None, False
        return self


def split_narration(text: str) -> list[str]:
    """Keep each spoken phrase intact so its measured audio duration defines one subtitle interval."""
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])
