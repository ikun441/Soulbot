"""SearXNG's documented GET /search?format=json contract; no arbitrary page fetching."""

import html
import ipaddress
import re
from urllib.parse import urlsplit

import httpx

from soulbot.providers.llm.base import ProviderError


def public_url(value):
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return False
        host = url.hostname.lower().rstrip(".")
        if (
            host == "localhost"
            or "." not in host
            or host.endswith((".local", ".localhost", ".internal"))
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (TypeError, ValueError):
        return False


def plain(value, limit):
    if not isinstance(value, str):
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", value))[:limit].strip()


class SearXNGProvider:
    capabilities = frozenset({"web_search"})

    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def search(self, query, limit):
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.settings.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    self.settings.base_url.rstrip("/") + "/search",
                    params={"q": query, "format": "json", "safesearch": 2},
                    headers={"Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        kind = (
                            "authentication"
                            if response.status_code in {401, 403}
                            else "rate_limit"
                            if response.status_code == 429
                            else "unavailable"
                        )
                        raise ProviderError(
                            kind,
                            "搜索服务拒绝请求，请检查实例是否启用 JSON 和访问权限",
                            uncertain=response.status_code >= 500,
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1048576:
                            raise ProviderError(
                                "invalid_response", "搜索响应超过 1MB", uncertain=True
                            )
                    import json

                    payload = json.loads(content)
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise ValueError
            items, seen = [], set()
            for row in rows[:100]:
                if not isinstance(row, dict):
                    continue
                url = row.get("url", "")
                if (
                    not isinstance(url, str)
                    or len(url) > 2048
                    or not public_url(url)
                    or url in seen
                ):
                    continue
                seen.add(url)
                items.append(
                    {
                        "title": plain(row.get("title"), 200),
                        "url": url,
                        "snippet": plain(row.get("content"), 600),
                    }
                )
                if len(items) >= limit:
                    break
            return items
        except ProviderError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise ProviderError(
                "invalid_response", "搜索服务没有返回有效 JSON 结果", uncertain=True
            ) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", "搜索服务超时，请先核对用量", uncertain=True) from None
        except httpx.HTTPError:
            raise ProviderError(
                "network", "无法连接搜索服务，请检查地址及网络", uncertain=True
            ) from None

    async def health_check(self):
        await self.search("SearXNG", 1)
        return True

    async def close(self):
        pass
