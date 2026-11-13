"""Тесты парсера сайтов. Сеть замокана (httpx.MockTransport) — это НЕ проверка реального сайта."""
import httpx
import pytest

from app.parsers.base import JsonlDocumentStore, SourceType
from app.parsers.net import RobotsRules, UnsafeURLError, assert_public_url, safe_get
from app.parsers.website_parser import (
    WebsiteParser, WebsiteParserConfig, build_site_profile, classify_page, normalize_url,
)

LONG = "Компания производит листовое стекло и стеклопакеты. " * 5


def page(title: str, body: str, links: list[str] = (), lang: str = "ru", canonical: str = "") -> str:
    a = "".join(f'<a href="{l}">{l}</a>' for l in links)
    can = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (f'<html lang="{lang}"><head><title>{title}</title>{can}'
            f'<meta name="description" content="desc {title}"></head>'
            f'<body><nav>{a}</nav><main><h1>{title}</h1><p>{body}</p>'
            f'<table><tr><th>Показатель</th><th>4 мм</th></tr><tr><td>LT</td><td>91%</td></tr></table>'
            f'<p>Тел.: +7 (3476) 37-71-55, sales@example.ru</p></main></body></html>')


def make_site(pages: dict[str, tuple[int, str]], extra: dict | None = None):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        url = str(req.url)
        if extra and url in extra:
            return extra[url]
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?print=\nDisallow: /secret/\n"
                                             "Sitemap: https://example.ru/sitemap.xml\n")
        if req.url.path == "/sitemap.xml":
            locs = "".join(f"<url><loc>https://example.ru{p}</loc></url>" for p in pages)
            return httpx.Response(200, text=f'<?xml version="1.0"?><urlset>{locs}</urlset>',
                                  headers={"content-type": "application/xml"})
        key = req.url.raw_path.decode()
        if key in pages:
            code, html = pages[key]
            return httpx.Response(code, text=html, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="nf", headers={"content-type": "text/html"})

    return handler, calls


def parser(handler) -> WebsiteParser:
    cfg = WebsiteParserConfig(request_delay=0, check_ip=False, concurrency=2)
    return WebsiteParser(cfg, transport=httpx.MockTransport(handler))


def test_robots_wildcards_and_longest_match():
    r = RobotsRules.parse("User-agent: *\nDisallow: /*?print=\nDisallow: /search/\nAllow: /search/map.php\n"
                          "Disallow: /club/$\nCrawl-delay: 2\nSitemap: https://x.ru/s.xml")
    assert not r.allowed("https://x.ru/news/?print=Y")
    assert not r.allowed("https://x.ru/search/q")
    assert r.allowed("https://x.ru/search/map.php")
    assert not r.allowed("https://x.ru/club/") and r.allowed("https://x.ru/club/a")
    assert r.crawl_delay == 2 and r.sitemaps == ["https://x.ru/s.xml"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.5/x", "http://169.254.169.254/latest",
                                 "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "http://u:p@1.1.1.1/"])
async def test_ssrf_blocked(url):
    with pytest.raises(UnsafeURLError):
        await assert_public_url(url)


async def test_redirect_to_foreign_host_blocked():
    def handler(req):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        with pytest.raises(UnsafeURLError):
            await safe_get(c, "https://example.ru/", allowed_hosts={"example.ru"}, check_ip=False)


def test_normalize_and_classify():
    assert normalize_url("HTTPS://Example.ru:443//a/index.php?utm_source=x&PAGEN_1=2#top") == "https://example.ru/a/?PAGEN_1=2"
    assert classify_page("https://x.ru/") == "home"
    assert classify_page("https://x.ru/products/") == "product_index"
    assert classify_page("https://x.ru/products/okna/") == "product_category"
    assert classify_page("https://x.ru/products/okna/ultra/") == "product"
    assert classify_page("https://x.ru/news/item-1/") == "news"


async def test_crawl_dedup_lang_robots_and_resync(tmp_path):
    pages = {
        "/": (200, page("Главная", LONG, ["/products/", "/products/a/b/", "/en/", "/secret/x/",
                                          "/files/price.pdf", "https://other.com/", "/copy/", "/news/?print=Y"])),
        "/products/": (200, page("Продукция", LONG + " каталог")),
        "/products/a/b/": (200, page("Стекло Ultra", LONG + " ультра")),
        "/copy/": (200, page("Стекло Ultra", LONG + " ультра")),  # дубликат содержимого
        "/en/": (200, page("Home", "Glass " * 50, lang="en")),
        "/secret/x/": (200, page("Секрет", LONG)),
    }
    handler, calls = make_site(pages)
    store = JsonlDocumentStore(tmp_path)
    res = await parser(handler).sync("https://example.ru/", store)
    urls = {d.url for d in store.all()}
    assert urls == {"https://example.ru/", "https://example.ru/products/", "https://example.ru/products/a/b/"}
    assert res.new == 3 and res.duplicates == 1 and not res.errors
    assert not any("/en/" in c or "/secret/" in c or "other.com" in c or "print=" in c for c in calls)
    doc = store.get("website:example.ru:https://example.ru/products/a/b/")
    assert doc.metadata["page_type"] == "product"
    assert doc.metadata["tables"] == [[["Показатель", "4 мм"], ["LT", "91%"]]]
    assert "+73476377155" in doc.metadata["phones"] and "sales@example.ru" in doc.metadata["emails"]
    state = store.get_state("website:example.ru")
    assert state["document_links"] == ["https://example.ru/files/price.pdf"]

    # повторная синхронизация: без изменений; затем изменение и удаление страницы
    store2 = JsonlDocumentStore(tmp_path)
    res2 = await parser(handler).sync("https://example.ru/", store2)
    assert (res2.new, res2.updated, res2.unchanged) == (0, 0, 3)

    pages["/products/"] = (200, page("Продукция", LONG + " обновлённый каталог"))
    pages["/"] = (200, page("Главная", LONG, ["/products/"]))
    del pages["/products/a/b/"]
    pages.pop("/copy/")
    res3 = await parser(make_site(pages)[0]).sync("https://example.ru/", JsonlDocumentStore(tmp_path))
    final = JsonlDocumentStore(tmp_path)
    assert res3.updated == 1
    assert {d.url for d in final.all()} == {"https://example.ru/", "https://example.ru/products/"}

    profile = build_site_profile(final.all(SourceType.WEBSITE))
    assert profile["pages_by_type"] == {"home": 1, "product_index": 1}


async def test_pagination_not_collapsed_by_canonical(tmp_path):
    pages = {
        "/": (200, page("Главная", LONG, ["/news/?PAGEN_1=2"])),
        "/news/?PAGEN_1=2": (200, page("Новости 2", LONG + " вторая", canonical="https://example.ru/news/")),
    }
    handler, _ = make_site(pages)
    store = JsonlDocumentStore(tmp_path)
    await parser(handler).sync("https://example.ru/", store)
    assert "https://example.ru/news/?PAGEN_1=2" in {d.url for d in store.all()}
