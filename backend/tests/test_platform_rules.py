from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import Script, platform_description, platform_title_units, validate_publishable_script


def script_with(*, douyin_title="抖音标题", xiaohongshu_title="小红书标题", description="测试正文，AI辅助制作。", tags=None):
    return {
        "platforms": {
            "douyin": {
                "title": douyin_title,
                "description": description,
                "tags": tags or [],
            },
            "xiaohongshu": {
                "title": xiaohongshu_title,
                "description": description,
                "tags": tags or [],
            },
        }
    }


def test_title_limits_use_platform_weighted_counting():
    assert platform_title_units("中A") == 3
    Script.model_validate(script_with(douyin_title="中" * 30, xiaohongshu_title="A" * 40))
    with pytest.raises(ValidationError, match="抖音标题最多30个中文字符"):
        Script.model_validate(script_with(douyin_title="中" * 31))
    with pytest.raises(ValidationError, match="小红书标题最多20个中文字符"):
        Script.model_validate(script_with(xiaohongshu_title="中" * 21))


def test_description_limit_includes_rendered_hashtags():
    description = "AI辅助制作" + "字" * 989
    valid = Script.model_validate(script_with(description=description, tags=["测试"]))
    assert len(platform_description(valid.platforms.douyin)) == 999
    with pytest.raises(ValidationError, match="发布正文.*最多1000个字符"):
        Script.model_validate(script_with(description=description, tags=["测试", "长"]))


def test_publishable_copy_requires_both_platforms_and_ai_disclosure():
    validate_publishable_script(script_with())
    with pytest.raises(ValueError, match="抖音标题不能为空"):
        validate_publishable_script(script_with(douyin_title=""))
    with pytest.raises(ValueError, match="抖音描述需明确标注"):
        validate_publishable_script(script_with(description="未披露的正文"))
