from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.integrations import net, sources
from app.integrations.errors import IntegrationError


@pytest.fixture(autouse=True)
def isolated_network_environment(monkeypatch):
    # DNS/network tests supply their own resolvers and transports, never the operator's proxy.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_pinned_tls_verifies_original_hostname(monkeypatch):
    import ssl

    captured = {}

    def wrapped(self, *args, **kwargs):
        captured.update(kwargs)
        return "fixture-socket"

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrapped)
    context = net._PinnedTLSContext("public.test")
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.wrap_socket(None, server_hostname="8.8.8.8") == "fixture-socket"
    assert captured["server_hostname"] == "public.test"


class _Response:
    def __init__(self, url: str, *, status: int = 200, headers=None, chunks=None):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        yield from self._chunks


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, _method, url, **_kwargs):
        self.requested.append(url)
        return self.responses.pop(0)


def _public_dns(_host, port, *, type):
    assert port > 0
    assert type
    return [(2, 1, 6, "", ("8.8.8.8", port))]


def test_safe_get_rejects_private_address(monkeypatch):
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda _host, port, *, type: [(2, 1, 6, "", ("127.0.0.1", port))],
    )

    with pytest.raises(IntegrationError) as caught:
        net.safe_get("http://example.test/private")

    assert caught.value.code == "SSRF_BLOCKED"


def test_safe_get_validates_redirect_before_second_request(monkeypatch):
    def resolve(host, port, *, type):
        address = "127.0.0.1" if host == "internal.test" else "8.8.8.8"
        return [(2, 1, 6, "", (address, port))]

    client = _Client(
        [
            _Response(
                "https://public.test/start",
                status=302,
                headers={"location": "http://internal.test/admin"},
            )
        ]
    )
    monkeypatch.setattr(net.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(net.httpx, "Client", lambda **_kwargs: client)

    with pytest.raises(IntegrationError) as caught:
        net.safe_get("https://public.test/start")

    assert caught.value.code == "SSRF_BLOCKED"
    assert len(client.requested) == 1
    assert client.requested[0].startswith("https://8.8.8.8/")


def test_redirect_resolution_stops_at_verified_url_without_fetching_target(monkeypatch):
    client = _Client(
        [
            _Response(
                "https://public.test/start",
                status=302,
                headers={"location": "https://public.test/video/12345"},
            )
        ]
    )
    monkeypatch.setattr(net.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(net.httpx, "Client", lambda **kwargs: client)
    assert net.safe_get("https://public.test/start", stop_at_url=lambda url: "/video/" in url) == (
        b"",
        "",
        "https://public.test/video/12345",
    )
    assert len(client.requested) == 1


def test_redirect_stop_does_not_bypass_private_address_check(monkeypatch):
    def resolve(host, port, *, type):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "internal.test" else "8.8.8.8", port))]

    client = _Client(
        [
            _Response(
                "https://public.test/start",
                status=302,
                headers={"location": "http://internal.test/video/12345"},
            )
        ]
    )
    monkeypatch.setattr(net.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(net.httpx, "Client", lambda **kwargs: client)
    with pytest.raises(IntegrationError) as caught:
        net.safe_get("https://public.test/start", stop_at_url=lambda url: True)
    assert caught.value.code == "SSRF_BLOCKED"


def test_safe_get_enforces_streamed_byte_limit(monkeypatch):
    client = _Client([_Response("https://public.test/feed", chunks=[b"1234", b"5678"])])
    monkeypatch.setattr(net.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(net.httpx, "Client", lambda **_kwargs: client)

    with pytest.raises(IntegrationError) as caught:
        net.safe_get("https://public.test/feed", max_bytes=7)

    assert caught.value.code == "RESPONSE_TOO_LARGE"


def test_safe_get_rejects_unbounded_caller_limit():
    with pytest.raises(IntegrationError) as caught:
        net.safe_get("https://public.test/feed", max_bytes=50_000_001)
    assert caught.value.code == "INVALID_LIMIT"


def test_safe_get_returns_body_type_and_final_url(monkeypatch):
    client = _Client(
        [
            _Response(
                "https://public.test/feed",
                headers={"content-type": "application/rss+xml"},
                chunks=[b"ok"],
            )
        ]
    )
    monkeypatch.setattr(net.socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr(net.httpx, "Client", lambda **_kwargs: client)

    assert net.safe_get("https://public.test/feed") == (
        b"ok",
        "application/rss+xml",
        "https://public.test/feed",
    )


def test_collect_rss_preserves_empty_feed(monkeypatch):
    monkeypatch.setattr(
        sources, "safe_get", lambda *_args, **_kwargs: (b"", "application/rss+xml", "https://feed.test/")
    )

    assert sources.collect_source({"kind": "rss", "url": "https://feed.test/rss"})["items"] == []


def test_collect_rss_filters_invalid_entries_and_normalizes_text(monkeypatch):
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Fixture</title>
      <item><title><![CDATA[AI \xe8\xa7\x86\xe9\xa2\x91 &amp; \xe5\x86\x85\xe5\xae\xb9]]></title>
        <link>https://news.test/article#tracking</link>
        <description><![CDATA[<p>\xe4\xb8\x80\xe6\xae\xb5 <b>\xe6\x91\x98\xe8\xa6\x81</b></p>]]></description>
        <pubDate>Wed, 10 Sep 2026 10:00:00 GMT</pubDate></item>
      <item><title>Duplicate</title><link>https://news.test/article#other</link></item>
      <item><title>No link</title></item>
    </channel></rss>"""
    monkeypatch.setattr(
        sources,
        "safe_get",
        lambda *_args, **_kwargs: (feed, "application/rss+xml", "https://feed.test/rss"),
    )

    items = sources.collect_source({"kind": "rss", "url": "https://feed.test/rss"})["items"]

    assert len(items) == 1
    assert items[0]["title"] == "AI 视频 & 内容"
    assert items[0]["summary"] == "一段 摘要"
    assert items[0]["url"] == "https://news.test/article"
    assert items[0]["published_at"].startswith("2026-09-10T10:00:00")
    assert 0 <= items[0]["relevance_score"] <= 100


def test_collect_hackernews_filters_dead_deleted_and_irrelevant(monkeypatch):
    responses = {
        "topstories.json": [1, 2, 3, 4],
        "/1.json": {"id": 1, "type": "story", "dead": True, "title": "AI dead"},
        "/2.json": {"id": 2, "type": "story", "deleted": True, "title": "AI deleted"},
        "/3.json": {"id": 3, "type": "story", "title": "My garden today", "score": 4},
        "/4.json": {
            "id": 4,
            "type": "story",
            "title": "OpenAI releases a video model",
            "url": "https://example.com/post",
            "score": 42,
            "time": 1_788_944_400,
        },
    }

    def fake_get(url, **_kwargs):
        key = (
            "topstories.json"
            if url.endswith("topstories.json")
            else next(key for key in responses if url.endswith(key))
        )
        return json.dumps(responses[key]).encode(), "application/json", url

    monkeypatch.setattr(sources, "safe_get", fake_get)
    items = sources.collect_source(
        {"kind": "hackernews", "url": "https://hacker-news.firebaseio.com/v0/topstories.json"}
    )["items"]

    assert [item["title"] for item in items] == ["OpenAI releases a video model"]
    assert items[0]["score"] == 42.0
    assert items[0]["selection_reason"]


def test_collect_disabled_source_does_not_fetch(monkeypatch):
    monkeypatch.setattr(sources, "safe_get", lambda *_args, **_kwargs: pytest.fail("should not fetch"))
    assert (
        sources.collect_source({"kind": "rss", "url": "https://feed.test", "enabled": False})["items"] == []
    )


def test_hackernews_rejects_arbitrary_api_host():
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source({"kind": "hackernews", "url": "https://example.com/topstories.json"})
    assert caught.value.code == "INVALID_HN_SOURCE"


def test_normalize_source_link_rejects_private_literal():
    assert sources._normalize_url("http://127.0.0.1/private", base_url="https://feed.test") is None


def _x_timeline(entries: list[dict]) -> bytes:
    payload = {"props": {"pageProps": {"timeline": {"entries": entries}}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></html>"
    ).encode()


def _x_entry(tweet: dict, *, entry_type: str = "tweet") -> dict:
    return {"type": entry_type, "content": {"tweet": tweet}}


def _x_post(identifier: str, text: str, created_at: str, author: str = "OpenAI", **extra) -> dict:
    return {
        "id": 0,
        "id_str": identifier,
        "full_text": text,
        "created_at": created_at,
        "favorite_count": 12,
        "entities": {},
        "user": {"screen_name": author},
        **extra,
    }


def test_collect_x_uses_fixed_public_timeline_and_preserves_recent_provenance(monkeypatch):
    entries = [
        _x_entry(
            _x_post(
                "101",
                "LLM inference keeps the literal <think> token.",
                "Thu Sep 10 12:00:00 +0000 2026",
                note_tweet={"text": "LLM note keeps the literal <think> token."},
                quoted_status=_x_post(
                    "201",
                    "Claude quote context",
                    "Thu Sep 10 10:00:00 +0000 2026",
                    author="AnthropicAI",
                ),
                card={
                    "binding_values": {
                        "description": {"string_value": "Machine learning release details"}
                    }
                },
            )
        ),
        _x_entry(
            _x_post(
                "102",
                "RT truncated…",
                "Thu Sep 10 11:00:00 +0000 2026",
                retweeted_status=_x_post(
                    "301",
                    "DeepSeek publishes a complete model inference guide",
                    "Wed Sep 09 11:00:00 +0000 2026",
                    author="deepseek_ai",
                ),
            )
        ),
        _x_entry(
            _x_post("103", "AI pinned but too old", "Wed Jul 01 11:00:00 +0000 2026")
        ),
        _x_entry(_x_post("0", "AI placeholder", "Thu Sep 10 12:30:00 +0000 2026")),
        _x_entry(_x_post("104", "A pleasant garden", "Thu Sep 10 13:00:00 +0000 2026")),
        _x_entry(_x_post("301", "DeepSeek duplicate", "Wed Sep 09 11:00:00 +0000 2026")),
        _x_entry(_x_post("105", "Gemini future", "Sat Sep 12 11:00:00 +0000 2026")),
        _x_entry(
            _x_post(
                "106",
                "RT wrapper",
                "Thu Sep 10 14:00:00 +0000 2026",
                retweeted_status={
                    **_x_post(
                        "401",
                        "Qwen source without a trustworthy author",
                        "Thu Sep 10 14:00:00 +0000 2026",
                    ),
                    "user": {},
                },
            )
        ),
        _x_entry(
            _x_post(
                "107",
                "OpenAI main post with an invalid quote author",
                "Thu Sep 10 15:00:00 +0000 2026",
                quoted_status={
                    **_x_post(
                        "402",
                        "Claude quote must not get a fabricated account path",
                        "Thu Sep 10 15:00:00 +0000 2026",
                    ),
                    "user": {},
                },
            )
        ),
        _x_entry({}, entry_type="cursor"),
    ]
    requested = []

    def fake_get(url, **kwargs):
        requested.append((url, kwargs))
        assert kwargs["max_bytes"] == 2_000_000
        assert kwargs["stop_at_url"]("https://example.org/redirect") is True
        return _x_timeline(entries), "text/html", url

    monkeypatch.setattr(sources, "safe_get", fake_get)
    result = sources._collect_x(
        "https://twitter.com/OpenAI/",
        10,
        instant=datetime(2026, 9, 11, tzinfo=UTC),
    )

    assert requested[0][0] == (
        "https://syndication.twitter.com/srv/timeline-profile/screen-name/OpenAI"
    )
    assert result["complete"] is False
    assert result["scanned"] == result["candidates"] == len(entries)
    assert "不代表完整 X 搜索或完整 30 天历史" in result["warning"]
    assert [item["url"] for item in result["items"]] == [
        "https://x.com/OpenAI/status/107",
        "https://x.com/OpenAI/status/101",
        "https://x.com/deepseek_ai/status/301",
    ]
    assert "fabricated account path" not in result["items"][0]["summary"]
    assert "<think>" in result["items"][1]["summary"]
    assert "https://x.com/AnthropicAI/status/201" in result["items"][1]["summary"]
    assert "Claude quote context" in result["items"][1]["summary"]
    assert "Machine learning release details" in result["items"][1]["summary"]
    assert "RT truncated" not in result["items"][2]["summary"]
    assert all("/status/401" not in item["url"] for item in result["items"])


@pytest.mark.parametrize(
    "body,code",
    [
        (b"<html>login</html>", "X_TIMELINE_INVALID"),
        (_x_timeline([]), "X_TIMELINE_UNAVAILABLE"),
    ],
)
def test_collect_x_rejects_missing_or_empty_timeline(monkeypatch, body, code):
    monkeypatch.setattr(
        sources,
        "safe_get",
        lambda url, **_kwargs: (body, "text/html", url),
    )
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source({"kind": "x", "url": "https://x.com/OpenAI"})
    assert caught.value.code == code


def test_collect_x_propagates_public_endpoint_rate_limit_without_fallback(monkeypatch):
    monkeypatch.setattr(
        sources,
        "safe_get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IntegrationError("UPSTREAM_HTTP_429", "公开时间线当前限流")
        ),
    )
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source({"kind": "x", "url": "https://x.com/OpenAI"})
    assert caught.value.code == "UPSTREAM_HTTP_429"
