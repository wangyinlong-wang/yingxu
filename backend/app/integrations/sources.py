"""RSS, Atom, Hacker News, news listing pages and bounded X collection."""

from __future__ import annotations

import calendar
import concurrent.futures
import html
import ipaddress
import json
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .errors import IntegrationError
from .net import safe_get

try:  # Main application dependencies include feedparser; keep import errors safe.
    import feedparser
except ImportError:  # pragma: no cover - exercised only in incomplete environments
    feedparser = None

_MAX_FEED_BYTES = 2_000_000
_MAX_HN_CANDIDATES = 100
_HN_COLLECTION_TIMEOUT = 30.0
_SUMMARY_LIMIT = 1_000
_HN_API_HOST = "hacker-news.firebaseio.com"
_X_TIMELINE_HOST = "syndication.twitter.com"
_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_X_SEARCH_MAX = 500
_KEYWORD_WEIGHT = 30
_MAX_LISTING_BYTES = 5_000_000
_LISTING_DATE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (\d{4})\b"
    r"|\b(\d{4})-(\d{2})-(\d{2})\b"
)
_MONTHS = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TIKHUB_DATA_MISSING = object()
_RELEVANCE_PATTERNS = (
    (
        re.compile(
            r"\b(ai|agi|llms?|gpt[\w.-]*|chatgpt|openai|anthropic|claude|gemini|qwen|deepseek|grok|xai"
            r"|llama|mistral|codex|sora|copilot|perplexity|midjourney|kimi|moonshot|minimax|glm|zhipu)\b",
            re.IGNORECASE,
        ),
        28,
        "AI",
    ),
    (
        re.compile(r"artificial intelligence|machine learning|generative|neural", re.IGNORECASE),
        24,
        "机器学习",
    ),
    (re.compile(r"\b(agent|agents|prompt|model|inference)\b", re.IGNORECASE), 18, "模型与智能体"),
    (re.compile(r"video|creator|content|social media|automation|ffmpeg", re.IGNORECASE), 16, "内容与自动化"),
    (
        re.compile(
            r"\b(python|javascript|typescript|rust|database|postgres|linux|kubernetes|security|compiler|software|programming)\b",
            re.IGNORECASE,
        ),
        18,
        "软件与工程",
    ),
    (
        re.compile(r"speech synthesis|text.to.speech|\btts\b|computer vision", re.IGNORECASE),
        16,
        "音视频技术",
    ),
    (
        re.compile(r"人工智能|大模型|语言模型|机器学习|深度学习|推理|智能体|模型训练|开源模型|编程|开发者|数据库|算力"
            r"|豆包|通义|文心|智谱|月之暗面|可灵|混元"),
        24,
        "中文技术内容",
    ),
)


def _plain_text(value: Any, *, limit: int = _SUMMARY_LIMIT) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _normalize_url(value: Any, *, base_url: str) -> str | None:
    if not value:
        return None
    try:
        joined = urljoin(base_url, str(value).strip())
        parsed = urlsplit(joined)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None
    try:
        if not ipaddress.ip_address(hostname).is_global:
            return None
    except ValueError:
        pass
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _published_iso(entry: Any) -> str | None:
    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_time:
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed_time), UTC).isoformat()
        except (OverflowError, TypeError, ValueError):
            pass
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            parsed = datetime.fromisoformat(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def source_keywords(value: Any) -> list[str]:
    """Normalize user focus keywords: trimmed, de-duplicated case-insensitively, bounded."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        keyword = re.sub(r"\s+", " ", item).strip()[:50]
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            result.append(keyword)
        if len(result) >= 30:
            break
    return result


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    hits = []
    for keyword in keywords:
        # ASCII words need boundaries ("ai" must not match "said"); CJK keywords match as substrings.
        if re.fullmatch(r"[\w .+-]+", keyword, re.ASCII):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
            matched = re.search(pattern, text, re.IGNORECASE)
        else:
            matched = keyword.casefold() in text.casefold()
        if matched:
            hits.append(keyword)
    return hits


def _relevance(title: str, summary: str, keywords: list[str] | None = None) -> tuple[float, str]:
    combined = f"{title} {summary}"
    score = 0
    reasons: list[str] = []
    hits = _keyword_hits(combined, keywords or [])
    if hits:
        score += _KEYWORD_WEIGHT
        reasons.append("关注词 " + "/".join(hits[:3]))
    for pattern, weight, label in _RELEVANCE_PATTERNS:
        if pattern.search(combined):
            score += weight
            reasons.append(label)
    if not reasons:
        return 0.0, "未匹配当前内容方向"
    return float(min(100, 30 + score)), "匹配" + "、".join(reasons[:3])


def _numeric_score(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


# Case-sensitive on purpose: "rust-v0.160.0" is a bare tag, "GPT-6.1" is a product name.
_VERSION_TITLE = re.compile(r"(?:[Rr]elease\s+|[Vv]ersion\s+|[a-z]{2,12}-)?[vV]?\d+(?:\.\d+)+(?:[-+.][0-9A-Za-z]+)*")
_RELEASE_SUFFIX = re.compile(r"\s*(?:releases?|release notes|changelog|更新日志|版本发布)\s*$", re.IGNORECASE)


def release_label(source_name: str | None, link: str) -> str:
    """Product name for version-only release titles: the source name, else the GitHub owner/repo."""
    label = _RELEASE_SUFFIX.sub("", source_name or "").strip()
    if label:
        return label[:100]
    parsed = urlsplit(link)
    parts = [part for part in parsed.path.split("/") if part]
    if (parsed.hostname or "").lower() == "github.com" and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def contextual_title(title: str, label: str) -> str:
    """A bare "0.159.1" says nothing in a topic list; name the product it belongs to."""
    if label and _VERSION_TITLE.fullmatch(title) and not title.casefold().startswith(label.casefold()):
        return f"{label} {title}"[:300]
    return title


def _collect_rss(
    url: str, limit: int, keywords: list[str] | None = None, source_name: str | None = None
) -> list[dict]:
    if feedparser is None:
        raise IntegrationError("DEPENDENCY_MISSING", "未安装 RSS 解析组件")
    body, _content_type, final_url = safe_get(url, max_bytes=_MAX_FEED_BYTES, retries=2)
    if not body.strip():
        return []
    parsed = feedparser.parse(body)
    entries = getattr(parsed, "entries", None) or []
    if not getattr(parsed, "version", "") and not entries:
        raise IntegrationError("INVALID_FEED", "来源返回的内容不是有效 RSS/Atom 订阅")
    results: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        title = _plain_text(entry.get("title"), limit=300)
        link = _normalize_url(entry.get("link") or entry.get("id"), base_url=final_url)
        if not title or not link or link in seen:
            continue
        seen.add(link)
        title = contextual_title(title, release_label(source_name, link))
        summary = _plain_text(entry.get("summary") or entry.get("description") or entry.get("content"))
        relevance_score, reason = _relevance(title, summary, keywords)
        results.append(
            {
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": _published_iso(entry),
                "score": _numeric_score(entry.get("score")),
                "relevance_score": relevance_score,
                "selection_reason": reason if relevance_score else "来自已配置的 RSS/Atom 订阅",
            }
        )
        if len(results) >= limit:
            break
    return results


def _load_json(url: str, *, max_bytes: int, timeout: float = 20, retries: int = 0) -> tuple[Any, str]:
    body, _content_type, final_url = safe_get(url, max_bytes=max_bytes, timeout=timeout, retries=retries)
    try:
        return json.loads(body.decode("utf-8")), final_url
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("INVALID_SOURCE_RESPONSE", "来源返回的 JSON 无效") from exc


def _hn_index_url(source_url: str) -> str:
    try:
        parsed = urlsplit(source_url)
    except ValueError as exc:
        raise IntegrationError("INVALID_URL", "Hacker News 来源地址无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "news.ycombinator.com":
        return f"https://{_HN_API_HOST}/v0/topstories.json"
    if host != _HN_API_HOST:
        raise IntegrationError("INVALID_HN_SOURCE", "Hacker News 采集仅支持官方公开 API")
    if parsed.path.endswith("stories.json"):
        return source_url
    return f"https://{_HN_API_HOST}/v0/topstories.json"


def _collect_hackernews(url: str, limit: int, keywords: list[str] | None = None) -> dict:
    deadline = time.monotonic() + _HN_COLLECTION_TIMEOUT
    identifiers, _ = _load_json(_hn_index_url(url), max_bytes=500_000, timeout=10, retries=2)
    if not isinstance(identifiers, list):
        raise IntegrationError("INVALID_SOURCE_RESPONSE", "Hacker News 索引响应无效")
    candidate_ids = [value for value in identifiers if isinstance(value, int) and value > 0]
    candidate_ids = candidate_ids[: min(_MAX_HN_CANDIDATES, max(limit * 3, limit))]
    results: dict[int, dict] = {}
    failures = 0
    completed = 0

    def fetch(index: int, identifier: int):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IntegrationError("SOURCE_TIMEOUT", "候选读取达到时间上限")
        item, _ = _load_json(
            f"https://{_HN_API_HOST}/v0/item/{identifier}.json",
            max_bytes=250_000,
            timeout=min(8.0, remaining),
        )
        return index, item

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    futures = {pool.submit(fetch, index, identifier): index for index, identifier in enumerate(candidate_ids)}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
            completed += 1
            try:
                index, item = future.result()
            except IntegrationError:
                failures += 1
                continue
            if (
                not isinstance(item, dict)
                or item.get("dead")
                or item.get("deleted")
                or item.get("type") not in {None, "story"}
            ):
                continue
            title = _plain_text(item.get("title"), limit=300)
            summary = _plain_text(item.get("text"))
            if not title:
                continue
            relevance_score, reason = _relevance(title, summary, keywords)
            if relevance_score <= 0:
                continue
            link = _normalize_url(item.get("url"), base_url="https://news.ycombinator.com/")
            if not link:
                link = f"https://news.ycombinator.com/item?id={candidate_ids[index]}"
            published_at = None
            try:
                if item.get("time") is not None:
                    published_at = datetime.fromtimestamp(float(item["time"]), UTC).isoformat()
            except (OverflowError, TypeError, ValueError):
                pass
            results[index] = {
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": published_at,
                "score": _numeric_score(item.get("score")),
                "relevance_score": relevance_score,
                "selection_reason": reason,
            }
    except concurrent.futures.TimeoutError:
        pass
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    complete = completed == len(candidate_ids) and failures == 0
    warning = (
        ""
        if complete
        else f"本轮已读取 {completed}/{len(candidate_ids)} 条候选，其中 {failures} 条失败；仅保存已成功读取的数据"
    )
    if not results and not complete:
        raise IntegrationError("SOURCE_INCOMPLETE", warning)
    return {
        "items": [results[index] for index in sorted(results)][:limit],
        "complete": complete,
        "scanned": completed,
        "candidates": len(candidate_ids),
        "warning": warning,
    }


def x_profile_handle(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise IntegrationError("INVALID_X_SOURCE", "X 来源地址无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or host not in _X_HOSTS
        or parsed.query
        or parsed.fragment
        or len(parts) != 1
        or parts[0].lower() in {"status", "search"}
        or not _X_HANDLE.fullmatch(parts[0])
    ):
        raise IntegrationError("INVALID_X_SOURCE", "X 来源必须是公开账号主页地址")
    return parts[0]


def x_search_query(url: str) -> tuple[str, str]:
    """Return (query, TikHub search_type) from a canonical https://x.com/search?q=…&f=live|top URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=bool(parsed.query))
    except ValueError as exc:
        raise IntegrationError("INVALID_X_SEARCH", "X 搜索来源地址无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    queries = params.get("q", [])
    modes = params.get("f", ["live"])
    query = re.sub(r"\s+", " ", queries[0]).strip() if len(queries) == 1 else ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or host not in _X_HOSTS
        or parsed.path.rstrip("/") != "/search"
        or parsed.fragment
        or set(params) - {"q", "f"}
        or not query
        or len(query) > _X_SEARCH_MAX
        or any(ord(character) < 32 for character in query)
        or len(modes) != 1
        or modes[0] not in {"live", "top"}
    ):
        raise IntegrationError("INVALID_X_SEARCH", "X 搜索来源必须是 https://x.com/search?q=关键词 形式")
    return query, "Latest" if modes[0] == "live" else "Top"


def _x_plain_text(value: Any, *, limit: int = 12_000) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value).replace("\x00", "")
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _x_post_identity(tweet: dict) -> tuple[str, str] | None:
    identifier = tweet.get("id_str")
    author = tweet.get("user", {}).get("screen_name") if isinstance(tweet.get("user"), dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"[1-9]\d{1,30}", identifier):
        return None
    if not isinstance(author, str) or not _X_HANDLE.fullmatch(author):
        return None
    return author, identifier


def _x_card_description(tweet: dict) -> str:
    card = tweet.get("card")
    bindings = card.get("binding_values") if isinstance(card, dict) else None
    description = bindings.get("description") if isinstance(bindings, dict) else None
    return _x_plain_text(description.get("string_value"), limit=2_000) if isinstance(description, dict) else ""


def _x_tweet_text(tweet: dict, *, limit: int = 12_000) -> str:
    note = tweet.get("note_tweet")
    note_text = note.get("text") if isinstance(note, dict) else None
    if not isinstance(note_text, str) and isinstance(note, dict):
        results = note.get("note_tweet_results")
        result = results.get("result") if isinstance(results, dict) else None
        note_text = result.get("text") if isinstance(result, dict) else None
    return _x_plain_text(note_text or tweet.get("full_text"), limit=limit)


def _x_supplement(tweet: dict) -> list[str]:
    parts: list[str] = []
    quoted = tweet.get("quoted_status")
    if isinstance(quoted, dict):
        quoted_text = _x_tweet_text(quoted, limit=4_000)
        identity = _x_post_identity(quoted)
        if quoted_text and identity:
            author, identifier = identity
            parts.append(f"引用帖（https://x.com/{author}/status/{identifier}）：\n{quoted_text}")
    card_description = _x_card_description(tweet)
    if card_description:
        parts.append(f"链接摘要：{card_description}")
    return parts


def _filter_x_tweets(
    tweets: list[dict],
    limit: int,
    *,
    instant: datetime | None,
    scanned: int,
    warning: str,
    keywords: list[str] | None = None,
    fallback: tuple[float, str] | None = None,
) -> dict:
    observed_at = (instant or datetime.now(UTC)).astimezone(UTC)
    cutoff = observed_at - timedelta(days=30)
    seen: set[str] = set()
    candidates: list[tuple[datetime, dict]] = []
    for outer in tweets:
        if not isinstance(outer, dict):
            continue
        retweeted = outer.get("retweeted_status")
        tweet = retweeted if isinstance(retweeted, dict) else outer
        identity = _x_post_identity(tweet)
        text = _x_tweet_text(tweet)
        if not identity or not text:
            continue
        author, identifier = identity
        if identifier in seen:
            continue
        try:
            published = parsedate_to_datetime(str(tweet.get("created_at") or ""))
            if published.tzinfo is None:
                raise ValueError("missing timezone")
            published = published.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            continue
        if not cutoff <= published <= observed_at:
            continue
        supplements = _x_supplement(tweet)
        summary = _x_plain_text("\n\n".join([text, *supplements]))
        relevance_score, reason = _relevance(text, " ".join(supplements), keywords)
        if relevance_score <= 0:
            # X search already matched the user's query beyond the visible text (quotes, cards,
            # media); a literal text filter would drop most legitimate results.
            if fallback is None:
                continue
            relevance_score, reason = fallback
        seen.add(identifier)
        candidates.append(
            (
                published,
                {
                    "title": _x_plain_text(text.splitlines()[0], limit=300),
                    "url": f"https://x.com/{author}/status/{identifier}",
                    "summary": summary,
                    "published_at": published.isoformat(),
                    "score": _numeric_score(tweet.get("favorite_count")),
                    "relevance_score": relevance_score,
                    "selection_reason": reason,
                },
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return {
        "items": [item for _published, item in candidates[:limit]],
        "complete": False,
        "scanned": scanned,
        "candidates": scanned,
        "warning": warning,
    }


def _collect_x(
    url: str, limit: int, *, instant: datetime | None = None, keywords: list[str] | None = None
) -> dict:
    handle = x_profile_handle(url)
    endpoint = f"https://{_X_TIMELINE_HOST}/srv/timeline-profile/screen-name/{handle}"
    body, _content_type, final_url = safe_get(
        endpoint,
        max_bytes=2_000_000,
        timeout=20,
        stop_at_url=lambda value: value != endpoint,
    )
    if final_url != endpoint or not body.strip():
        raise IntegrationError("X_TIMELINE_UNAVAILABLE", "X 公开时间线当前不可读取或发生了重定向")
    try:
        document = BeautifulSoup(body, "html.parser")
        script = document.find("script", id="__NEXT_DATA__")
        raw = script.string if script else None
        payload = json.loads(raw) if isinstance(raw, str) else None
        entries = payload["props"]["pageProps"]["timeline"]["entries"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise IntegrationError("X_TIMELINE_INVALID", "X 公开时间线返回结构无效") from exc
    if not isinstance(entries, list) or not entries:
        raise IntegrationError("X_TIMELINE_UNAVAILABLE", "X 公开时间线缺少可读取的公开内容")

    tweets: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "tweet":
            continue
        content = entry.get("content")
        outer = content.get("tweet") if isinstance(content, dict) else None
        if isinstance(outer, dict):
            tweets.append(outer)
    scanned = len(entries)
    warning = (
        f"X 公开时间线仅提供当前可见的有限窗口（本轮 {scanned} 条），"
        "不代表完整 X 搜索或完整 30 天历史"
    )
    return _filter_x_tweets(tweets, limit, instant=instant, scanned=scanned, warning=warning, keywords=keywords)


def _tikhub_result(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = value.get("result")
    return result if isinstance(result, dict) else None


def _normalize_flat_tikhub_tweet(node: dict, *, nested: bool) -> dict | None:
    """TikHub's current flattened shape: tweet_id/text/favorites plus author or screen_name."""
    identifier = node.get("tweet_id")
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        identifier = str(identifier)
    author = node.get("author") if isinstance(node.get("author"), dict) else node.get("user_info")
    screen_name = author.get("screen_name") if isinstance(author, dict) else None
    if screen_name is None:
        screen_name = node.get("screen_name")
    result = {
        "id_str": identifier,
        "full_text": node.get("text"),
        "created_at": node.get("created_at"),
        "favorite_count": node.get("favorites"),
        "user": {"screen_name": screen_name} if screen_name is not None else {},
    }
    if not nested:
        retweeted = node.get("retweeted_tweet") or node.get("retweeted_status")
        if isinstance(retweeted, dict):
            retweet = _normalize_tikhub_tweet(retweeted, nested=True)
            if retweet is None:
                return None
            result["retweeted_status"] = retweet
        quoted = node.get("quoted")
        if isinstance(quoted, dict):
            quote = _normalize_tikhub_tweet(quoted, nested=True)
            if quote is not None:
                result["quoted_status"] = quote
    return result if _x_post_identity(result) and _x_tweet_text(result) else None


def _normalize_tikhub_tweet(value: Any, *, nested: bool = False) -> dict | None:
    if not isinstance(value, dict):
        return None
    if "tweet_id" in value and "legacy" not in value:
        return _normalize_flat_tikhub_tweet(value, nested=nested)
    wrapped = value.get("tweet")
    node = wrapped if isinstance(wrapped, dict) else value
    legacy = node.get("legacy") if isinstance(node.get("legacy"), dict) else node
    identifier = node.get("rest_id") or legacy.get("id_str")
    core = node.get("core")
    user_results = core.get("user_results") if isinstance(core, dict) else None
    user_node = _tikhub_result(user_results)
    user_legacy = user_node.get("legacy") if isinstance(user_node, dict) else None
    legacy_user = legacy.get("user")
    screen_name = (
        user_legacy.get("screen_name")
        if isinstance(user_legacy, dict)
        else legacy_user.get("screen_name")
        if isinstance(legacy_user, dict)
        else None
    )
    result = {
        "id_str": identifier,
        "full_text": legacy.get("full_text"),
        "created_at": legacy.get("created_at"),
        "favorite_count": legacy.get("favorite_count"),
        "user": {"screen_name": screen_name} if screen_name is not None else {},
    }
    note = node.get("note_tweet") or legacy.get("note_tweet")
    if isinstance(note, dict):
        result["note_tweet"] = note
    if nested:
        return result if _x_post_identity(result) and _x_tweet_text(result) else None

    retweet_container = legacy.get("retweeted_status_result") or node.get("retweeted_status_result")
    if retweet_container is not None:
        retweet = _normalize_tikhub_tweet(_tikhub_result(retweet_container), nested=True)
        if retweet is None:
            return None
        result["retweeted_status"] = retweet
    elif isinstance(legacy.get("retweeted_status"), dict):
        retweet = _normalize_tikhub_tweet(legacy["retweeted_status"], nested=True)
        if retweet is None:
            return None
        result["retweeted_status"] = retweet

    quote_container = legacy.get("quoted_status_result") or node.get("quoted_status_result")
    quote_value = _tikhub_result(quote_container) if quote_container is not None else None
    if quote_value is None and isinstance(legacy.get("quoted_status"), dict):
        quote_value = legacy["quoted_status"]
    if quote_value is not None:
        quote = _normalize_tikhub_tweet(quote_value, nested=True)
        if quote is not None:
            result["quoted_status"] = quote
    card = node.get("card") or legacy.get("card")
    if isinstance(card, dict):
        result["card"] = card
    return result if _x_post_identity(result) and _x_tweet_text(result) else None


def _tikhub_tweet_nodes(data: Any) -> tuple[list[Any], bool]:
    if isinstance(data, list):
        return data[:200], True
    if not isinstance(data, dict):
        return [], False
    for key in ("tweets", "items"):
        if key in data:
            values = data[key]
            return (values[:200], True) if isinstance(values, list) else ([], False)
    if isinstance(data.get("timeline"), list):
        # Current flattened user-posts and search pages; a pinned post is still bounded by age below.
        pinned = [data["pinned"]] if isinstance(data.get("pinned"), dict) else []
        return (pinned + data["timeline"])[:200], True
    user = data.get("user")
    result = user.get("result") if isinstance(user, dict) else None
    timeline_v2 = result.get("timeline_v2") if isinstance(result, dict) else None
    timeline = timeline_v2.get("timeline") if isinstance(timeline_v2, dict) else None
    instructions = timeline.get("instructions") if isinstance(timeline, dict) else None
    if not isinstance(instructions, list):
        return [], False
    nodes: list[Any] = []
    invalid_non_cursor = False
    for instruction in instructions[:50]:
        entries = instruction.get("entries") if isinstance(instruction, dict) else None
        if entries is None:
            continue
        if not isinstance(entries, list):
            return [], False
        for entry in entries:
            if len(nodes) >= 200 or not isinstance(entry, dict):
                continue
            content = entry.get("content")
            item_content = content.get("itemContent") if isinstance(content, dict) else None
            tweet_results = item_content.get("tweet_results") if isinstance(item_content, dict) else None
            tweet = _tikhub_result(tweet_results)
            if tweet is not None:
                nodes.append(tweet)
                continue
            entry_id = str(entry.get("entryId") or entry.get("entry_id") or "").lower()
            entry_type = str(content.get("entryType") or "") if isinstance(content, dict) else ""
            is_cursor = "cursor" in entry_id or "cursor" in entry_type.lower()
            invalid_non_cursor = invalid_non_cursor or not is_cursor
    return nodes, not invalid_non_cursor


def _collect_x_tikhub(
    data: Any,
    limit: int,
    *,
    instant: datetime | None = None,
    keywords: list[str] | None = None,
    search_query: str | None = None,
) -> dict:
    nodes, valid_shape = _tikhub_tweet_nodes(data)
    if not valid_shape:
        raise IntegrationError("TIKHUB_RESPONSE_INVALID", "TikHub X 数据结构无效，已保留原始结果供本地重试")
    tweets = [tweet for node in nodes if (tweet := _normalize_tikhub_tweet(node)) is not None]
    if nodes and not tweets:
        raise IntegrationError("TIKHUB_RESPONSE_INVALID", "TikHub 当前页没有可识别的推文结构")
    if search_query is None:
        warning = (
            f"TikHub 仅返回当前一页（本轮识别 {len(tweets)} 条），"
            "不代表完整 X 搜索或完整 30 天历史"
        )
        fallback = None
    else:
        warning = f"TikHub 搜索仅返回当前一页（本轮识别 {len(tweets)} 条），不代表完整搜索结果"
        fallback = (45.0, "匹配 X 搜索条件")
    return _filter_x_tweets(
        tweets,
        limit,
        instant=instant,
        scanned=len(nodes),
        warning=warning,
        keywords=keywords,
        fallback=fallback,
    )


def _listing_date(value: str) -> str | None:
    match = _LISTING_DATE.search(value or "")
    if not match:
        return None
    try:
        if match.group(1):
            month = _MONTHS[match.group(1)[:3].lower()]
            stamp = datetime(int(match.group(3)), month, int(match.group(2)), tzinfo=UTC)
        else:
            stamp = datetime(int(match.group(4)), int(match.group(5)), int(match.group(6)), tzinfo=UTC)
    except (KeyError, ValueError):
        return None
    return stamp.isoformat()


def _listing_prefix(options: dict, final_url: str) -> str:
    prefix = options.get("path_prefix") if isinstance(options, dict) else None
    if prefix is None:
        return urlsplit(final_url).path.rstrip("/") + "/"
    if not isinstance(prefix, str) or not prefix.startswith("/") or len(prefix) > 200 or "//" in prefix:
        raise IntegrationError("INVALID_SOURCE", "网页列表的文章路径前缀必须以 / 开头")
    return prefix


def _collect_html_list(url: str, limit: int, options: dict, keywords: list[str] | None = None) -> dict:
    """Read one public news listing page; only same-host article links under the listing path are kept."""
    body, content_type, final_url = safe_get(url, max_bytes=_MAX_LISTING_BYTES, timeout=30, retries=2)
    if "html" not in content_type and not body.lstrip().startswith((b"<!DOCTYPE", b"<!doctype", b"<html")):
        raise IntegrationError("INVALID_LISTING", "来源返回的内容不是网页，无法解析文章列表")
    prefix = _listing_prefix(options, final_url)
    host = (urlsplit(final_url).hostname or "").lower()
    document = BeautifulSoup(body, "html.parser")
    for element in document.select("script,style,noscript,svg,template"):
        element.decompose()
    found: dict[str, dict] = {}
    anchors = document.find_all("a", href=True)
    for anchor in anchors[:2_000]:
        link = _normalize_url(anchor["href"], base_url=final_url)
        if not link:
            continue
        parsed = urlsplit(link)
        path = parsed.path.rstrip("/")
        if (parsed.hostname or "").lower() != host or not path.startswith(prefix.rstrip("/") + "/"):
            continue
        if path == prefix.rstrip("/") or parsed.query:
            continue
        time_tag = anchor.find("time")
        if time_tag is None and anchor.parent is not None:
            # A parent's date belongs to this link only when that parent holds a single date.
            sibling_times = anchor.parent.find_all("time", limit=2)
            time_tag = sibling_times[0] if len(sibling_times) == 1 else None
        published = None
        if time_tag is not None:
            published = _published_iso({"published": time_tag.get("datetime")}) if time_tag.get("datetime") else None
            published = published or _listing_date(time_tag.get_text(" ", strip=True))
            if time_tag in anchor.descendants:
                time_tag.extract()
        published = published or _listing_date(anchor.get_text(" ", strip=True))
        heading = anchor.find(re.compile(r"^h[1-6]$"))
        candidates = [heading.get_text(" ", strip=True)] if heading else list(anchor.stripped_strings)
        title = _plain_text(max(candidates, key=len, default=""), limit=300)
        key = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        current = found.get(key)
        if current is None:
            found[key] = {"url": link, "title": title, "published_at": published}
        else:
            if len(title) > len(current["title"]):
                current["title"] = title
            current["published_at"] = current["published_at"] or published
    items = []
    for entry in found.values():
        if len(entry["title"]) < 6:
            continue
        relevance_score, reason = _relevance(entry["title"], "", keywords)
        items.append(
            {
                "title": entry["title"],
                "url": entry["url"],
                "summary": "",
                "published_at": entry["published_at"],
                "score": 0.0,
                "relevance_score": relevance_score,
                "selection_reason": reason if relevance_score else "来自已配置的网页列表",
            }
        )
        if len(items) >= limit:
            break
    if not items and anchors:
        raise IntegrationError("LISTING_EMPTY", f"网页中没有找到 {prefix} 下的文章链接，请检查地址或路径前缀")
    return {"items": items, "complete": True, "scanned": len(found), "candidates": len(found), "warning": ""}


def fetch_article_excerpt(url: str) -> dict:
    """Read only the selected public article, without recursive crawling or executing page content."""
    body, content_type, final_url = safe_get(url, max_bytes=3_000_000, timeout=25)
    if "html" in content_type or body.lstrip().startswith((b"<!DOCTYPE", b"<!doctype", b"<html")):
        document = BeautifulSoup(body, "html.parser")
        for element in document.select("script,style,noscript,nav,footer,header,aside,svg,form"):
            element.decompose()
        article = document.find("article") or document.find("main") or document.body or document
        text = article.get_text(" ", strip=True)
    elif content_type.startswith("text/"):
        text = body.decode("utf-8", errors="replace")
    else:
        raise IntegrationError(
            "ARTICLE_FORMAT_UNSUPPORTED", "选题原文不是可读取的网页或文本，请补充可核验材料"
        )
    text = re.sub(r"\s+", " ", text).strip()[:12_000]
    if len(text) < 100:
        raise IntegrationError(
            "ARTICLE_EVIDENCE_INSUFFICIENT", "原文可读取内容不足，请选择其他来源或补充材料"
        )
    return {"text": text, "url": final_url, "fetched_at": datetime.now(UTC).isoformat()}


def collect_source(source: dict, *, limit: int = 30, tikhub_data: Any = _TIKHUB_DATA_MISSING) -> dict:
    """Collect a bounded list from one configured public source."""

    if not isinstance(source, dict):
        raise IntegrationError("INVALID_SOURCE", "来源配置无效")
    if source.get("enabled") is False:
        return {"items": [], "complete": True, "scanned": 0, "candidates": 0, "warning": ""}
    if not isinstance(limit, int) or limit <= 0 or limit > 100:
        raise IntegrationError("INVALID_LIMIT", "单次采集数量必须在 1 到 100 之间")
    kind = str(source.get("kind") or "").strip().lower()
    url = str(source.get("url") or "").strip()
    keywords = source_keywords(source.get("keywords"))
    if not url:
        raise IntegrationError("INVALID_SOURCE", "请填写来源地址")
    if kind == "rss":
        items = _collect_rss(url, limit, keywords, str(source.get("name") or ""))
        return {
            "items": items,
            "complete": True,
            "scanned": len(items),
            "candidates": len(items),
            "warning": "",
        }
    if kind == "hackernews":
        return _collect_hackernews(url, limit, keywords)
    if kind == "x":
        return _collect_x(url, limit, keywords=keywords)
    if kind == "html_list":
        return _collect_html_list(url, limit, source.get("options") or {}, keywords)
    if kind in {"x_tikhub", "x_tikhub_search"}:
        if tikhub_data is _TIKHUB_DATA_MISSING:
            raise IntegrationError("TIKHUB_RESPONSE_MISSING", "TikHub X 采集缺少已持久化的数据响应")
        query = x_search_query(url)[0] if kind == "x_tikhub_search" else None
        return _collect_x_tikhub(tikhub_data, limit, keywords=keywords, search_query=query)
    raise IntegrationError("UNSUPPORTED_SOURCE", "暂不支持该来源类型")
