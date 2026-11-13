"""Парсер сайтов компаний.

Обходит страницы сайта (sitemap + ссылки), соблюдая robots.txt, лимиты и
таймауты, защищаясь от SSRF и недопустимых redirect. Извлекает чистый текст,
таблицы характеристик, контакты и тип страницы (товар, услуга, проект,
новость...), удаляет дубликаты и поддерживает повторную синхронизацию.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .base import (
    IndexCallback,
    JsonlDocumentStore,
    ParsedDocument,
    SourceType,
    SyncResult,
    UpsertStatus,
    clean_text,
    utcnow,
)
from .net import DEFAULT_USER_AGENT, FetchError, RobotsRules, UnsafeURLError, assert_public_url, safe_get

log = logging.getLogger(__name__)
logging.getLogger("trafilatura").setLevel(logging.ERROR)

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
    ".mp4", ".mov", ".avi", ".webm", ".mp3", ".wav",
    ".zip", ".rar", ".7z", ".gz", ".tar",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dmg", ".apk",
}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf"}
LANG_CODES = {"en", "ru", "tr", "de", "fr", "es", "it", "zh", "cn", "kz", "uz", "ar", "pl", "ua", "uk", "by", "fa", "ja", "ko"}
PAGINATION_PARAM = re.compile(r"^(PAGEN_\d+|page|p|PAGE)$", re.I)

PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s\- ]*\(?\d{3,4}\)?[\s\- ]*\d{1,3}[\s\- ]*\d{2}[\s\- ]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Тип страницы по первому сегменту пути. Расширяется под конкретный сайт.
PAGE_TYPE_BY_SEGMENT = {
    "products": "product", "product": "product", "catalog": "product", "katalog": "product", "produkciya": "product",
    "services": "service", "service": "service", "uslugi": "service", "service-support": "service",
    "projects": "project", "proekty": "project", "portfolio": "project",
    "news": "news", "novosti": "news", "press": "news", "blog": "article", "articles": "article", "stati": "article",
    "about": "about", "o-kompanii": "about", "company": "about", "o-nas": "about",
    "contacts": "contacts", "kontakty": "contacts",
    "career": "career", "vacancies": "career", "vakansii": "career",
    "shareholders-investors": "investors", "investors": "investors",
    "policy": "legal", "privacy": "legal",
}


@dataclass
class WebsiteParserConfig:
    max_pages: int = 300
    max_depth: int = 4
    concurrency: int = 4
    request_delay: float = 0.3  # секунд между запросами одного воркера (минимум, robots может увеличить)
    timeout: float = 20.0
    max_bytes: int = 5 * 1024 * 1024
    allowed_languages: tuple[str, ...] = ("ru",)  # пусто = все языки
    use_sitemap: bool = True
    respect_robots: bool = True
    min_text_chars: int = 80
    user_agent: str = DEFAULT_USER_AGENT
    check_ip: bool = True  # отключается только в тестах


@dataclass
class _Ctx:
    root_host: str
    allowed_hosts: set[str]
    robots: RobotsRules
    delay: float
    seen: set[str] = field(default_factory=set)
    visited: set[str] = field(default_factory=set)
    document_links: set[str] = field(default_factory=set)
    truncated: bool = False


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path.endswith(("/index.php", "/index.html")):
        path = path.rsplit("/", 1)[0] + "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if PAGINATION_PARAM.match(k)))
    return urlunsplit((scheme, netloc, path, query, ""))


def classify_page(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments and segments[0].lower() in LANG_CODES:
        segments = segments[1:]
    if not segments:
        return "home"
    kind = PAGE_TYPE_BY_SEGMENT.get(segments[0].lower(), "page")
    if kind in ("product", "service", "project", "news", "article"):
        if len(segments) == 1:
            return f"{kind}_index"
        if kind == "product" and len(segments) == 2:
            return "product_category"
    return kind


def _url_language(url: str) -> str | None:
    seg = next((s for s in urlsplit(url).path.split("/") if s), "").lower()
    return seg if seg in LANG_CODES else None


def _extension(url: str) -> str:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot > path.rfind("/") else ""


def _parse_tables(soup: BeautifulSoup) -> list[list[list[str]]]:
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [clean_text(td.get_text(" ")) for td in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if len(rows) >= 2:
            tables.append(rows)
    return tables


def extract_page(html: str, url: str) -> dict[str, Any]:
    """Извлекает содержимое страницы: заголовок, текст, таблицы, контакты, ссылки."""
    soup = BeautifulSoup(html, "lxml")
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").split("-")[0].lower() if html_tag else ""

    def meta(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return clean_text(tag["content"])
        return ""

    title = clean_text(soup.title.get_text()) if soup.title else ""
    h1 = clean_text(soup.find("h1").get_text(" ")) if soup.find("h1") else ""
    canonical_tag = soup.find("link", rel="canonical")
    canonical = urljoin(url, canonical_tag["href"]) if canonical_tag and canonical_tag.get("href") else ""

    links: list[str] = []
    phones: set[str] = set()
    emails: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("tel:"):
            phones.add(re.sub(r"[^\d+]", "", href[4:]))
        elif low.startswith("mailto:"):
            emails.add(href[7:].split("?")[0].strip().lower())
        elif not low.startswith(("javascript:", "#", "data:")):
            links.append(urljoin(url, href))

    breadcrumbs: list[str] = []
    bc = soup.select_one('[class*="breadcrumb"], nav[aria-label*="bread" i]')
    if bc:
        breadcrumbs = [clean_text(x.get_text(" ")) for x in bc.find_all(["a", "span", "li"]) if clean_text(x.get_text(" "))]
        breadcrumbs = list(dict.fromkeys(breadcrumbs))

    tables = _parse_tables(soup)

    text = trafilatura.extract(
        html, url=url, include_tables=True, include_links=False, include_comments=False,
        favor_recall=True, deduplicate=False,  # dedup trafilatura имеет глобальный кэш → недетерминизм
    ) or ""
    if len(text) < 80:  # запасной вариант: видимый текст основного блока
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form", "svg"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        text = main.get_text("\n")
    text = clean_text(text)
    for m in PHONE_RE.findall(text):
        phones.add(re.sub(r"[^\d+]", "", m))
    for m in EMAIL_RE.findall(text):
        if not m.lower().endswith((".png", ".jpg", ".webp", ".svg")):
            emails.add(m.lower())

    return {
        "lang": lang,
        "title": h1 or meta("og:title") or title,
        "html_title": title,
        "description": meta("description", "og:description"),
        "site_name": meta("og:site_name"),
        "canonical": canonical,
        "text": text,
        "tables": tables,
        "breadcrumbs": breadcrumbs,
        "phones": sorted(p for p in phones if len(re.sub(r"\D", "", p)) >= 10),
        "emails": sorted(emails),
        "links": links,
    }


class WebsiteParser:
    def __init__(self, config: WebsiteParserConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or WebsiteParserConfig()
        self._transport = transport  # для тестов (respx / MockTransport)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.config.timeout),
            headers={"User-Agent": self.config.user_agent, "Accept-Language": "ru,en;q=0.5"},
            transport=self._transport,
        )

    # ------------------------------------------------------------------ helpers

    def _in_scope(self, url: str, ctx: _Ctx) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in ctx.allowed_hosts:
            return False
        ext = _extension(url)
        if ext in DOCUMENT_EXTENSIONS:
            ctx.document_links.add(url)
            return False
        if ext in SKIP_EXTENSIONS:
            return False
        lang = _url_language(url)
        if self.config.allowed_languages and lang and lang not in self.config.allowed_languages:
            return False
        if self.config.respect_robots and not ctx.robots.allowed(url):
            return False
        return True

    async def _load_robots(self, client: httpx.AsyncClient, base: str) -> RobotsRules:
        if not self.config.respect_robots:
            return RobotsRules()
        try:
            res = await safe_get(client, urljoin(base, "/robots.txt"), max_bytes=512 * 1024,
                                 check_ip=self.config.check_ip)
        except (FetchError, httpx.HTTPError) as e:
            log.info("robots.txt недоступен (%s) — используем правила по умолчанию", e)
            return RobotsRules()
        if res.status_code >= 400:
            return RobotsRules()
        return RobotsRules.parse(res.text, self.config.user_agent)

    async def _sitemap_urls(self, client: httpx.AsyncClient, ctx: _Ctx, base: str) -> list[str]:
        queue = list(ctx.robots.sitemaps) or [urljoin(base, "/sitemap.xml")]
        found: list[str] = []
        checked: set[str] = set()
        while queue and len(checked) < 20:
            sm = queue.pop(0)
            if sm in checked:
                continue
            checked.add(sm)
            if (urlsplit(sm).hostname or "").lower() not in ctx.allowed_hosts:
                continue
            try:
                res = await safe_get(client, sm, max_bytes=20 * 1024 * 1024, allowed_hosts=ctx.allowed_hosts,
                                     check_ip=self.config.check_ip)
            except (FetchError, UnsafeURLError, httpx.HTTPError) as e:
                log.info("sitemap %s недоступен: %s", sm, e)
                continue
            if res.status_code >= 400 or "<" not in res.text[:200]:
                continue
            soup = BeautifulSoup(res.text, "xml")
            if soup.find("sitemapindex"):
                queue.extend(loc.get_text(strip=True) for loc in soup.select("sitemap > loc"))
            else:
                found.extend(loc.get_text(strip=True) for loc in soup.select("url > loc"))
        return found

    # ------------------------------------------------------------------ main

    async def sync(
        self,
        start_url: str,
        store: JsonlDocumentStore,
        index_callback: IndexCallback | None = None,
    ) -> SyncResult:
        start_url = normalize_url(start_url)
        root = urlsplit(start_url)
        host = (root.hostname or "").lower()
        bare = host[4:] if host.startswith("www.") else host
        result = SyncResult(source_type=SourceType.WEBSITE, source_id=bare)
        changed: list[ParsedDocument] = []

        if self.config.check_ip:
            await assert_public_url(start_url)
        async with self._client() as client:
            robots = await self._load_robots(client, start_url)
            delay = max(self.config.request_delay, robots.crawl_delay or 0)
            ctx = _Ctx(root_host=bare, allowed_hosts={bare, f"www.{bare}"}, robots=robots, delay=delay)

            queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()

            def enqueue(url: str, depth: int) -> None:
                url = normalize_url(url)
                if url in ctx.seen or not self._in_scope(url, ctx):
                    return
                if len(ctx.seen) >= self.config.max_pages:
                    ctx.truncated = True
                    return
                ctx.seen.add(url)
                queue.put_nowait((url, depth))

            enqueue(start_url, 0)
            if self.config.use_sitemap:
                for u in await self._sitemap_urls(client, ctx, start_url):
                    enqueue(u, 1)
                result.notes.append(f"sitemap: в очереди {len(ctx.seen)} URL")

            async def worker() -> None:
                while True:
                    url, depth = await queue.get()
                    try:
                        doc, links = await self._fetch_page(client, url, ctx, result)
                        if doc is not None:
                            self._store(doc, store, result, changed)
                        if depth < self.config.max_depth:
                            for link in links:
                                enqueue(link, depth + 1)
                    except Exception as e:  # noqa: BLE001 — ошибка одной страницы не останавливает обход
                        result.errors.append(f"{url}: {type(e).__name__}: {e}")
                    finally:
                        queue.task_done()
                        await asyncio.sleep(ctx.delay)

            workers = [asyncio.create_task(worker()) for _ in range(max(1, self.config.concurrency))]
            await queue.join()
            for w in workers:
                w.cancel()

            removed = await self._drop_missing(client, ctx, store, bare, result)
            if removed:
                result.notes.append(f"удалено исчезнувших страниц: {removed}")

        if ctx.truncated:
            result.notes.append(f"достигнут лимит max_pages={self.config.max_pages}")
        if ctx.document_links:
            result.notes.append(f"найдено ссылок на документы (PDF/DOC и т.п.): {len(ctx.document_links)}")
        store.set_state(f"website:{bare}", {
            "start_url": start_url,
            "last_sync": utcnow().isoformat(),
            "pages_seen": len(ctx.seen),
            "document_links": sorted(ctx.document_links),
        })
        store.save()
        if index_callback and changed:
            await index_callback(changed)
        result.finished_at = utcnow()
        return result

    async def _fetch_page(self, client: httpx.AsyncClient, url: str, ctx: _Ctx, result: SyncResult):
        ctx.visited.add(url)
        res = await safe_get(client, url, max_bytes=self.config.max_bytes, allowed_hosts=ctx.allowed_hosts,
                             check_ip=self.config.check_ip)
        if res.status_code >= 400:
            result.skipped += 1
            return None, []
        if res.content_type and "html" not in res.content_type:
            result.skipped += 1
            return None, []
        page = extract_page(res.text, res.url)
        if self.config.allowed_languages and page["lang"] and page["lang"] not in self.config.allowed_languages:
            result.skipped += 1
            return None, page["links"]
        final_url = normalize_url(res.url)
        canonical = normalize_url(page["canonical"]) if page["canonical"] else ""
        # canonical учитываем, кроме страниц пагинации (они часто ссылаются на первую страницу)
        if (canonical and not urlsplit(final_url).query
                and (urlsplit(canonical).hostname or "") in ctx.allowed_hosts):
            final_url = canonical
        if len(page["text"]) < self.config.min_text_chars:
            result.skipped += 1
            return None, page["links"]
        doc = ParsedDocument(
            source_type=SourceType.WEBSITE,
            source_id=ctx.root_host,
            external_id=final_url,
            url=final_url,
            title=page["title"],
            text=page["text"],
            metadata={
                "page_type": classify_page(final_url),
                "description": page["description"],
                "html_title": page["html_title"],
                "site_name": page["site_name"],
                "lang": page["lang"],
                "breadcrumbs": page["breadcrumbs"],
                "tables": page["tables"],
                "phones": page["phones"],
                "emails": page["emails"],
                "requested_url": url,
            },
        )
        return doc, page["links"]

    @staticmethod
    def _store(doc: ParsedDocument, store: JsonlDocumentStore, result: SyncResult, changed: list) -> None:
        if store.get(doc.key) is None:
            dup = store.find_by_hash(doc.source_type, doc.source_id, doc.content_hash)
            if dup is not None and dup.key != doc.key:
                result.duplicates += 1
                return
        status = store.upsert(doc)
        result.count(status)
        if status in (UpsertStatus.NEW, UpsertStatus.UPDATED):
            changed.append(doc)

    async def _drop_missing(self, client, ctx: _Ctx, store: JsonlDocumentStore, source_id: str,
                            result: SyncResult) -> int:
        """Повторная синхронизация: страницы из прошлых обходов, которые сейчас отдают 404/410, удаляются."""
        if ctx.truncated:
            return 0
        stale = [d for d in store.all(SourceType.WEBSITE, source_id)
                 if d.metadata.get("requested_url", d.url) not in ctx.visited and d.url not in ctx.visited]
        gone: list[str] = []
        for d in stale[:100]:
            try:
                res = await safe_get(client, d.url, max_bytes=self.config.max_bytes,
                                     allowed_hosts=ctx.allowed_hosts, check_ip=self.config.check_ip)
                if res.status_code in (404, 410):
                    gone.append(d.key)
            except (FetchError, UnsafeURLError, httpx.HTTPError) as e:
                result.errors.append(f"recheck {d.url}: {e}")
        return store.remove(gone)


def build_site_profile(docs: list[ParsedDocument]) -> dict[str, Any]:
    """Сводка о бизнесе по страницам сайта: товары, услуги, проекты, контакты."""
    by_type: dict[str, list[ParsedDocument]] = {}
    for d in docs:
        by_type.setdefault(d.metadata.get("page_type", "page"), []).append(d)
    home = (by_type.get("home") or [None])[0]
    phones: Counter[str] = Counter()
    emails: Counter[str] = Counter()
    for d in docs:
        phones.update(d.metadata.get("phones", []))
        emails.update(d.metadata.get("emails", []))
    contacts_text = "\n\n".join(d.text for d in by_type.get("contacts", []))[:4000]

    def items(kind: str) -> list[dict[str, Any]]:
        out = []
        for d in sorted(by_type.get(kind, []), key=lambda x: x.url):
            entry: dict[str, Any] = {"title": d.title, "url": d.url}
            if d.metadata.get("description"):
                entry["description"] = d.metadata["description"]
            if d.metadata.get("tables"):
                entry["specs"] = d.metadata["tables"]
            out.append(entry)
        return out

    return {
        "site": docs[0].source_id if docs else None,
        "site_name": next((d.metadata.get("site_name") for d in docs if d.metadata.get("site_name")), None),
        "home_title": home.title if home else None,
        "home_description": home.metadata.get("description") if home else None,
        "pages_by_type": {k: len(v) for k, v in sorted(by_type.items())},
        "product_categories": items("product_category"),
        "products": items("product"),
        "services": items("service") + items("service_index"),
        "projects": [{"title": d.title, "url": d.url} for d in sorted(by_type.get("project", []), key=lambda x: x.url)],
        "news": [{"title": d.title, "url": d.url} for d in sorted(by_type.get("news", []), key=lambda x: x.url)],
        "about_pages": [{"title": d.title, "url": d.url} for d in by_type.get("about", [])],
        "phones": [p for p, _ in phones.most_common(15)],
        "emails": [e for e, _ in emails.most_common(15)],
        "contacts_text": contacts_text,
        "generated_at": utcnow().isoformat(),
        "_note": "Сводка собрана правилами по структуре сайта, без участия LLM.",
    }
