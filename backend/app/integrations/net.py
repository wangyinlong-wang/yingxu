"""Bounded HTTP fetching for untrusted public source URLs."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

import certifi
import httpx

from .errors import IntegrationError

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_MAX_ALLOWED_BYTES = 50_000_000
_MAX_URL_LENGTH = 8_192
_MAX_TIMEOUT = 120.0


_DNS_CACHE: dict[str, tuple[float, set[str]]] = {}
_DNS_LOCK = threading.Lock()


def _proxy_for(scheme: str, hostname: str) -> str | None:
    proxies = getproxies_environment()
    if proxy_bypass_environment(hostname, proxies):
        return None
    return proxies.get(scheme) or proxies.get("all")


# Fixed DNS-over-HTTPS resolvers tried in order; one flaky provider must not fail every source.
_DOH_RESOLVERS = (
    ("dns.google", "https://dns.google/resolve", {}),
    ("cloudflare-dns.com", "https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"}),
)


def _doh_query(resolver: tuple[str, str, dict], hostname: str, proxy: str) -> tuple[set[str], int]:
    host, endpoint, headers = resolver
    with (
        httpx.Client(
            proxy=proxy,
            trust_env=False,
            verify=_PinnedTLSContext(host),
            timeout=8,
            follow_redirects=False,
        ) as client,
        client.stream("GET", endpoint, params={"name": hostname, "type": "A"}, headers=headers) as response,
    ):
        if response.status_code != 200:
            raise IntegrationError("DNS_FAILED", "代理环境下的公网 DNS 查询失败")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 128_000:
                raise IntegrationError("DNS_FAILED", "公网 DNS 响应超出上限")
    try:
        payload = json.loads(body)
        if payload.get("Status") != 0:
            raise ValueError()
        answers = payload.get("Answer", [])
        addresses = {answer["data"] for answer in answers if answer.get("type") in {1, 28}}
        ttl = min([60, *[max(1, int(answer.get("TTL", 60))) for answer in answers]])
        if not addresses:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise IntegrationError("DNS_FAILED", "公网 DNS 未返回有效地址") from None
    return addresses, ttl


def _resolve_addresses(hostname: str, port: int, proxy: str | None) -> set[str]:
    if proxy is None:
        return {item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    # When a configured outbound proxy is needed, local DNS can point at a wrong
    # public address. Query fixed HTTPS DNS services through that same proxy,
    # validate the answers below, then still connect to a pinned public IP.
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(hostname)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    last_error: Exception | None = None
    for resolver in _DOH_RESOLVERS:
        try:
            addresses, ttl = _doh_query(resolver, hostname, proxy)
            break
        except (IntegrationError, httpx.HTTPError) as exc:
            last_error = exc
    else:
        if isinstance(last_error, IntegrationError):
            raise last_error
        raise IntegrationError("DNS_FAILED", "代理环境下的公网 DNS 查询失败") from last_error
    with _DNS_LOCK:
        if len(_DNS_CACHE) >= 512:
            _DNS_CACHE.clear()
        _DNS_CACHE[hostname] = (time.monotonic() + ttl, addresses)
    return addresses


def _public_target(url: str) -> tuple[SplitResult, list[str]]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise IntegrationError("INVALID_URL", "来源地址无效") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise IntegrationError("INVALID_URL", "仅允许访问公网 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise IntegrationError("INVALID_URL", "来源地址不能包含账号凭据")
    if port is not None and not 1 <= port <= 65535:
        raise IntegrationError("INVALID_URL", "来源地址端口无效")

    hostname = parsed.hostname.rstrip(".")
    if hostname.lower() == "localhost" or hostname.lower().endswith((".localhost", ".local", ".internal")):
        raise IntegrationError("SSRF_BLOCKED", "来源地址指向本地或内部主机")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        addresses = {str(literal)}
    except ValueError:
        try:
            addresses = _resolve_addresses(
                hostname,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                _proxy_for(parsed.scheme.lower(), hostname),
            )
        except (OSError, UnicodeError, httpx.HTTPError) as exc:
            raise IntegrationError("DNS_FAILED", "无法解析来源主机") from exc

    if not addresses:
        raise IntegrationError("DNS_FAILED", "来源主机没有可用地址")
    public_addresses: list[str] = []
    for value in sorted(addresses, key=lambda value: (":" in value, value)):
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
        except ValueError as exc:
            raise IntegrationError("DNS_FAILED", "来源主机返回了无效地址") from exc
        if not address.is_global:
            raise IntegrationError("SSRF_BLOCKED", "来源地址指向非公网网络，已阻止访问")
        public_addresses.append(str(address))
    return parsed, public_addresses[:4]


def _pinned_url(parsed: SplitResult, address: str) -> tuple[str, str]:
    ip = ipaddress.ip_address(address)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    pinned = urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))
    original_host = (parsed.hostname or "").encode("idna").decode("ascii")
    if parsed.port is not None:
        original_host = f"{original_host}:{parsed.port}"
    return pinned, original_host


class _PinnedTLSContext(ssl.SSLContext):
    """Verify the requested hostname even when an HTTP/SOCKS proxy tunnels to a vetted IP.

    httpcore's proxy transports do not all forward the sni_hostname extension.
    Overriding this standard context method keeps certificate verification and SNI
    bound to the original hostname; it does not disable TLS verification.
    """

    def __new__(cls, hostname: str):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, hostname: str):
        self.requested_hostname = hostname
        self.load_verify_locations(
            cafile=os.environ.get("SSL_CERT_FILE") or certifi.where(), capath=os.environ.get("SSL_CERT_DIR")
        )
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True

    def wrap_socket(self, *args, server_hostname=None, **kwargs):
        return super().wrap_socket(*args, server_hostname=self.requested_hostname, **kwargs)


_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_DELAY = 10.0


def _retry_delay(error: IntegrationError, attempt: int) -> float:
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        return min(_MAX_RETRY_DELAY, max(0.0, retry_after))
    return min(_MAX_RETRY_DELAY, 1.5 * (2**attempt))


def safe_get(
    url: str,
    *,
    max_bytes: int = 5_000_000,
    timeout: float = 20,
    stop_at_url: Callable[[str], bool] | None = None,
    retries: int = 0,
) -> tuple[bytes, str, str]:
    """Fetch a public URL; transient 429/5xx/network failures are retried with bounded backoff."""

    if not isinstance(retries, int) or not 0 <= retries <= 3:
        raise IntegrationError("INVALID_LIMIT", "重试次数必须在 0 到 3 之间")
    attempt = 0
    while True:
        try:
            return _safe_get_once(url, max_bytes=max_bytes, timeout=timeout, stop_at_url=stop_at_url)
        except IntegrationError as error:
            transient = error.code in {"HTTP_TIMEOUT", "HTTP_FAILED", "DNS_FAILED"} or (
                getattr(error, "status", None) in _RETRYABLE_STATUSES
            )
            if not transient or attempt >= retries:
                raise
            time.sleep(_retry_delay(error, attempt))
            attempt += 1


def _safe_get_once(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    stop_at_url: Callable[[str], bool] | None,
) -> tuple[bytes, str, str]:
    """Fetch a public URL with redirect validation and a decoded-byte limit."""

    if not isinstance(url, str) or not url.strip():
        raise IntegrationError("INVALID_URL", "请填写来源地址")
    if len(url) > _MAX_URL_LENGTH or any(ord(character) < 32 for character in url):
        raise IntegrationError("INVALID_URL", "来源地址过长或包含无效字符")
    if not isinstance(max_bytes, int) or not 0 < max_bytes <= _MAX_ALLOWED_BYTES:
        raise IntegrationError("INVALID_LIMIT", "响应大小上限必须在 1 字节到 50 MB 之间")
    if (
        not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= _MAX_TIMEOUT
    ):
        raise IntegrationError("INVALID_TIMEOUT", "请求超时时间必须在 0 到 120 秒之间")

    current_url = url.strip()
    deadline = time.monotonic() + float(timeout)
    headers = {
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": "LocalPublisher/1.0",
    }
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            parsed, addresses = _public_target(current_url)
            if redirect_count > 0 and stop_at_url is not None and stop_at_url(current_url):
                return b"", "", current_url
            server_name = (parsed.hostname or "").encode("idna").decode("ascii")
            # Use a fresh pool per origin. A CDN may reuse one IP for different hosts;
            # those hosts must never share a TLS connection verified for another name.
            tls_context = _PinnedTLSContext(server_name)
            with httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(min(5.0, float(timeout))),
                proxy=_proxy_for(parsed.scheme.lower(), server_name),
                trust_env=False,
                verify=tls_context,
                headers=headers,
            ) as client:
                redirected = False
                last_network_error: httpx.HTTPError | None = None
                for address in addresses:
                    if time.monotonic() >= deadline:
                        raise IntegrationError("HTTP_TIMEOUT", "请求来源超时")
                    pinned_url, host_header = _pinned_url(parsed, address)
                    try:
                        response_context = client.stream(
                            "GET",
                            pinned_url,
                            headers={"Host": host_header},
                            extensions={"sni_hostname": server_name},
                        )
                        with response_context as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                location = response.headers.get("location")
                                if not location:
                                    raise IntegrationError(
                                        "INVALID_REDIRECT", "来源返回了缺少目标地址的重定向"
                                    )
                                if redirect_count == _MAX_REDIRECTS:
                                    raise IntegrationError("TOO_MANY_REDIRECTS", "来源重定向次数过多")
                                current_url = urljoin(current_url, location)
                                _public_target(current_url)
                                redirected = True
                                break
                            if response.status_code < 200 or response.status_code >= 300:
                                error = IntegrationError("HTTP_ERROR", f"来源返回 HTTP {response.status_code}")
                                error.status = response.status_code
                                try:
                                    error.retry_after = float(response.headers.get("retry-after", ""))
                                except ValueError:
                                    error.retry_after = None
                                raise error
                            content_length = response.headers.get("content-length")
                            if content_length:
                                try:
                                    if int(content_length) > max_bytes:
                                        raise IntegrationError("RESPONSE_TOO_LARGE", "来源响应超过大小上限")
                                except ValueError:
                                    pass
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                if time.monotonic() >= deadline:
                                    raise IntegrationError("HTTP_TIMEOUT", "请求来源超时")
                                if len(chunk) > max_bytes - len(body):
                                    raise IntegrationError("RESPONSE_TOO_LARGE", "来源响应超过大小上限")
                                body.extend(chunk)
                            return bytes(body), response.headers.get("content-type", ""), current_url
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        last_network_error = exc
                        continue
                if redirected:
                    continue
                if last_network_error is not None:
                    if time.monotonic() >= deadline:
                        raise IntegrationError("HTTP_TIMEOUT", "请求来源超时") from last_network_error
                    raise IntegrationError("HTTP_FAILED", "请求来源失败") from last_network_error
                if not addresses:
                    raise IntegrationError("DNS_FAILED", "来源主机没有可用地址")
    except IntegrationError:
        raise
    except httpx.TimeoutException as exc:
        raise IntegrationError("HTTP_TIMEOUT", "请求来源超时") from exc
    except httpx.HTTPError as exc:
        raise IntegrationError("HTTP_FAILED", "请求来源失败") from exc

    raise IntegrationError("HTTP_FAILED", "来源请求未完成")
