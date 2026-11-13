"""Безопасная сетевая загрузка: защита от SSRF, ручная проверка redirect,
ограничение размера ответа, robots.txt с поддержкой шаблонов (* и $)."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_USER_AGENT = "GlassAgentBot/0.1 (+marketing knowledge parser)"


class UnsafeURLError(ValueError):
    """URL указывает на запрещённую схему/адрес (SSRF) или недопустимый redirect."""


class FetchError(RuntimeError):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def assert_public_url(url: str) -> None:
    """Пропускает только http(s) на публичные адреса. Проверяет все A/AAAA записи."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Запрещённая схема: {parts.scheme!r}")
    if not parts.hostname:
        raise UnsafeURLError("В URL нет хоста")
    if parts.username or parts.password:
        raise UnsafeURLError("URL с учётными данными запрещён")
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise FetchError(f"DNS: не удалось разрешить {host}: {e}") from e
        ips = sorted({info[4][0] for info in infos})
    bad = [ip for ip in ips if not _is_public_ip(ip)]
    if bad or not ips:
        raise UnsafeURLError(f"Хост {host} указывает на непубличный адрес: {bad or ips}")


@dataclass
class FetchResult:
    url: str  # итоговый URL после redirect
    status_code: int
    content_type: str
    text: str
    redirects: list[str] = field(default_factory=list)


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_redirects: int = 5,
    max_bytes: int = 5 * 1024 * 1024,
    allowed_hosts: set[str] | None = None,
    check_ip: bool = True,
) -> FetchResult:
    """GET с проверкой каждого шага redirect (SSRF + разрешённые хосты) и лимитом размера.

    Клиент должен быть создан с follow_redirects=False.
    """
    chain: list[str] = []
    current = url
    for _ in range(max_redirects + 1):
        if check_ip:
            await assert_public_url(current)
        host = (urlsplit(current).hostname or "").lower()
        if allowed_hosts is not None and host not in allowed_hosts:
            raise UnsafeURLError(f"Redirect на сторонний хост запрещён: {host}")
        async with client.stream("GET", current) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise FetchError(f"Redirect без Location: {current}")
                chain.append(current)
                current = urljoin(current, location)
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchError(f"Ответ слишком большой ({declared} байт): {current}")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise FetchError(f"Ответ больше {max_bytes} байт: {current}")
            encoding = resp.charset_encoding or "utf-8"
            try:
                text = body.decode(encoding, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return FetchResult(current, resp.status_code, ctype, text, chain)
    raise FetchError(f"Слишком много redirect: {url}")


# --------------------------------------------------------------------------- robots.txt


def _rule_to_regex(path: str) -> re.Pattern[str]:
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    pattern = "".join(".*" if ch == "*" else re.escape(ch) for ch in path)
    return re.compile("^" + pattern + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    """Правила robots.txt для нашего User-agent (Google-совместимая семантика:
    поддержка * и $, побеждает самое длинное совпадение, при равенстве — Allow)."""

    rules: list[tuple[bool, str, re.Pattern[str]]] = field(default_factory=list)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, user_agent: str = DEFAULT_USER_AGENT) -> "RobotsRules":
        ua_token = user_agent.split("/")[0].lower()
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        sitemaps: list[str] = []
        agents: list[str] = []
        lines: list[tuple[str, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "sitemap":
                sitemaps.append(value)
                continue
            if key == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, lines))
                    agents, lines = [], []
                agents.append(value.lower())
                last_was_agent = True
            else:
                last_was_agent = False
                lines.append((key, value))
        if agents:
            groups.append((agents, lines))

        chosen = [g for g in groups if any(a != "*" and a in ua_token for a in g[0])]
        if not chosen:
            chosen = [g for g in groups if "*" in g[0]]
        rules: list[tuple[bool, str, re.Pattern[str]]] = []
        delay: float | None = None
        for _, glines in chosen:
            for key, value in glines:
                if key in ("allow", "disallow"):
                    if not value:
                        continue  # пустой Disallow = разрешено всё
                    rules.append((key == "allow", value, _rule_to_regex(value)))
                elif key == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError:
                        pass
        return cls(rules=rules, crawl_delay=delay, sitemaps=sitemaps)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        best: tuple[int, bool] | None = None
        for allow, raw, rx in self.rules:
            if rx.match(path):
                cand = (len(raw), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]
