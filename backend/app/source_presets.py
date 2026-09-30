"""Recommended official AI news sources users can add with one click; nothing is added automatically."""

from __future__ import annotations

from urllib.parse import quote

VENDOR_ACCOUNTS = ("OpenAI", "AnthropicAI", "claudeai", "xai", "grok", "GoogleDeepMind", "GeminiApp")
HOT_TERMS = ("GPT", "ChatGPT", "Claude", "Grok", "Gemini", "Codex", "Sora")


def _search_url(query: str, mode: str = "live") -> str:
    return f"https://x.com/search?q={quote(query, safe='')}&f={mode}"


SOURCE_PRESETS: tuple[dict, ...] = (
    {
        "id": "openai-news",
        "name": "OpenAI News",
        "kind": "rss",
        "url": "https://openai.com/news/rss.xml",
        "description": "OpenAI 官方新闻 RSS",
        "paid": False,
    },
    {
        "id": "anthropic-news",
        "name": "Anthropic News",
        "kind": "html_list",
        "url": "https://www.anthropic.com/news",
        "options": {"path_prefix": "/news/"},
        "description": "Anthropic 官方新闻页（网页列表解析）",
        "paid": False,
    },
    {
        "id": "deepmind-blog",
        "name": "Google DeepMind Blog",
        "kind": "rss",
        "url": "https://deepmind.google/blog/rss.xml",
        "description": "Google DeepMind 官方博客 RSS",
        "paid": False,
    },
    {
        "id": "mistral-news",
        "name": "Mistral AI News",
        "kind": "html_list",
        "url": "https://mistral.ai/news",
        "options": {"path_prefix": "/news/"},
        "description": "Mistral 官方新闻页（网页列表解析）",
        "paid": False,
    },
    {
        "id": "meta-ai-blog",
        "name": "Meta AI Blog",
        "kind": "html_list",
        "url": "https://ai.meta.com/blog/",
        "options": {"path_prefix": "/blog/"},
        "description": "Meta AI 官方博客（网页列表解析）",
        "paid": False,
    },
    {
        "id": "claude-code-releases",
        "name": "Claude Code Releases",
        "kind": "rss",
        "url": "https://github.com/anthropics/claude-code/releases.atom",
        "description": "Claude Code 版本发布（GitHub Atom）",
        "paid": False,
    },
    {
        "id": "codex-releases",
        "name": "OpenAI Codex Releases",
        "kind": "rss",
        "url": "https://github.com/openai/codex/releases.atom",
        "description": "OpenAI Codex CLI 版本发布（GitHub Atom）",
        "paid": False,
    },
    {
        "id": "x-vendor-accounts",
        "name": "X · AI 厂商官方账号",
        "kind": "x_tikhub_search",
        "url": _search_url(" OR ".join(f"from:{name}" for name in VENDOR_ACCOUNTS) + " -filter:replies"),
        "description": "一次 TikHub 搜索覆盖 OpenAI/Anthropic/xAI/Grok/DeepMind 官方账号最新帖子（按次计费）",
        "paid": True,
    },
    {
        "id": "x-hot-ai",
        "name": "X · AI 热门讨论",
        "kind": "x_tikhub_search",
        "url": _search_url("(" + " OR ".join(HOT_TERMS) + ") min_faves:1000 -filter:replies"),
        "description": "近期千赞以上的 GPT/Claude/Grok/Gemini 讨论（TikHub 搜索，按次计费）",
        "paid": True,
    },
)


def preset_by_id(identifier: str) -> dict | None:
    return next((item for item in SOURCE_PRESETS if item["id"] == identifier), None)
