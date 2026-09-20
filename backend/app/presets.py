from __future__ import annotations

import copy

DEFAULT_PRESET_ID = "tech_brief_video_v1"

_PRESETS = (
    {
        "id": DEFAULT_PRESET_ID,
        "version": 1,
        "name": "科技快讯",
        "description": "用克制的信息密度快速介绍新闻、发布和产品更新。",
        "content_type": "video",
        "supported_scene_types": ["image", "text_card", "stat_card", "quote_card"],
        "style": {
            "palette": ["#315bff", "#111827", "#f7f8fb", "#f2c94c"],
            "information_density": "low",
            "motion_density": "medium",
            "transition": "fade",
        },
        "quality_rules": {
            "min_visual_types": 2,
            "max_consecutive_same_type": 2,
            "require_hero_scene": True,
        },
    },
    {
        "id": "data_explainer_video_v1",
        "version": 1,
        "name": "数据解释",
        "description": "以数据为主线解释榜单、指标与实验结论。",
        "content_type": "video",
        "supported_scene_types": ["image", "text_card", "stat_card", "quote_card"],
        "style": {
            "palette": ["#315bff", "#111827", "#f7f8fb", "#f2c94c"],
            "information_density": "medium",
            "motion_density": "medium",
            "transition": "fade",
        },
        "quality_rules": {
            "min_visual_types": 2,
            "max_consecutive_same_type": 2,
            "require_hero_scene": True,
        },
    },
    {
        "id": "avatar_commentary_video_v1",
        "version": 1,
        "name": "数字人口播",
        "description": "以人物表达为主的观点、评论和教程视频。",
        "content_type": "video",
        "supported_scene_types": ["image", "text_card", "stat_card", "quote_card"],
        "style": {
            "palette": ["#315bff", "#111827", "#f7f8fb", "#f2c94c"],
            "information_density": "low",
            "motion_density": "low",
            "transition": "fade",
        },
        "quality_rules": {
            "min_visual_types": 2,
            "max_consecutive_same_type": 2,
            "require_hero_scene": True,
        },
    },
)


def list_presets() -> list[dict]:
    return copy.deepcopy(list(_PRESETS))


def preset_by_id(identifier: str) -> dict | None:
    return next((copy.deepcopy(item) for item in _PRESETS if item["id"] == identifier), None)
