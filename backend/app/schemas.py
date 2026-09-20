from __future__ import annotations

import ipaddress
import re
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


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


class SourceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["rss", "hackernews", "x", "x_tikhub"] = "rss"
    url: str = Field(max_length=2000)
    enabled: bool = True
    _url = field_validator("url")(http_url)

    @model_validator(mode="after")
    def canonical_source_url(self):
        if self.kind in {"x", "x_tikhub"}:
            self.url = canonical_x_profile_url(self.url)
        return self


class SourcePatch(Input):
    name: str | None = Field(None, min_length=1, max_length=200)
    kind: Literal["x", "x_tikhub"] | None = None
    url: str | None = Field(None, max_length=2000)
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return http_url(value) if value is not None else value


class SourceCollect(Input):
    expected_kind: Literal["rss", "hackernews", "x", "x_tikhub"] | None = None


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
    stage: Literal["all", "brief", "script", "assets", "render", "local_voice", "motion", "images"] = (
        "all"
    )
    scene_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def scene_only_for_images(self):
        if self.scene_id is not None and self.stage != "images":
            raise ValueError("scene_id 仅可用于重绘配图")
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
    device_serial: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


class DeviceInspect(Input):
    platform: Literal["douyin", "xiaohongshu"]


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


class ModelConfig(Input):
    protocol: Literal["openai", "ark", "dashscope"] = "openai"
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

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value):
        if not value:
            return value
        parsed = urlsplit(http_url(value))
        if parsed.query or parsed.fragment:
            raise ValueError("模型地址不能包含查询参数或片段；凭据请使用专用环境变量")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("远程模型服务必须使用 HTTPS")
        if parsed.hostname in {"169.254.169.254", "metadata.google.internal"}:
            raise ValueError("不允许访问元数据服务")
        return value.rstrip("/")

    @model_validator(mode="after")
    def configured_if_enabled(self):
        if self.enabled and (not self.base_url or not self.model):
            raise ValueError("启用模型前需填写地址和模型名")
        return self


class VideoConfig(Input):
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
        if set(value) != {"text", "image", "speech"}:
            raise ValueError("模型能力必须为 text、image、speech")
        allowed = {"text": {"openai"}, "image": {"openai", "ark"}, "speech": {"openai", "dashscope"}}
        for kind, model in value.items():
            if model.protocol not in allowed[kind]:
                raise ValueError("模型协议与能力不匹配")
        return value


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
    kind: Literal["pipeline", "metrics"]
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str
    enabled: bool = False
    daily_limit: int = Field(default=1, ge=1, le=20)
    _zone = field_validator("timezone")(AllSettings.valid_zone.__func__)


def split_narration(text: str) -> list[str]:
    """Keep each spoken phrase intact so its measured audio duration defines one subtitle interval."""
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])
