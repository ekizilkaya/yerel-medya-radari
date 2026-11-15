"""Kaynak tarama: RSS/Atom keşfi + okuma, RSS yoksa ana sayfadan başlık çıkarma.

Önceki sürümden farkı: her makale sayfasını ayrıca indirmez (kaynak başına 1-2 istek),
yayın tarihini alır, keşfedilen besleme adresini sources.json'da saklar.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .sources import Source
from .textnorm import article_id, bare_domain, clean_text, normalize_url

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
}

FEED_PATHS = [
    "/rss", "/feed", "/rss.xml", "/feed/", "/rss/", "/?feed=rss2", "/atom.xml", "/index.xml",
    "/rss/gundem", "/rss/anasayfa", "/rss/news", "/rss/haberler", "/sondakika.xml", "/export/rss",
]


@dataclass(slots=True)
class Article:
    title: str
    url: str
    source: str            # alan adı
    city: str
    summary: str = ""
    published: str = ""    # ISO-8601 (UTC) ya da ""
    id: str = ""

    def record(self) -> dict:
        """Arşiv satırı (kısa anahtarlarla)."""
        r = {"i": self.id, "t": self.title, "u": self.url, "s": self.source, "c": self.city}
        if self.published:
            r["p"] = self.published
        if self.summary:
            r["x"] = self.summary
        return r

    @classmethod
    def from_record(cls, r: dict) -> "Article":
        return cls(title=r["t"], url=r.get("u", ""), source=r.get("s", ""), city=r.get("c", ""),
                   summary=r.get("x", ""), published=r.get("p", ""), id=r.get("i", ""))


def make_client(timeout: float = 20, max_connections: int = 60) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=20),
    )


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        r = await client.get(url)
        if r.status_code == 200 and r.content:
            return r
    except (httpx.HTTPError, ValueError, UnicodeError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Ayrıştırıcılar
# ---------------------------------------------------------------------------

def _looks_like_feed(content: bytes) -> bool:
    head = content[:1500].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head or b"<rdf" in head


def _entry_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OverflowError, OSError):
                pass
    return ""


def parse_feed(content: bytes, base_url: str, max_items: int = 40) -> list[dict]:
    if not _looks_like_feed(content):
        return []
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries[: max_items * 2]:
        title = clean_text(e.get("title") or "")
        if not (12 <= len(title) <= 300):
            continue
        link = (e.get("link") or "").strip()
        if link and not link.startswith("http"):
            link = urljoin(base_url, link)
        items.append({
            "title": title,
            "url": normalize_url(link),
            "summary": clean_text(e.get("summary") or e.get("description") or "", 200),
            "published": _entry_date(e),
        })
        if len(items) >= max_items:
            break
    return items


def extract_feed_links(html: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for link in soup.find_all("link", href=True):
        typ = (link.get("type") or "").lower()
        if "rss" in typ or "atom" in typ:
            href = urljoin(base_url, link["href"].strip())
            if "comment" not in href and "yorum" not in href and href not in out:
                out.append(href)
    return out[:4]


_BAD_LINK = re.compile(
    r"(/etiket|/tag/|/kategori|/category|/yazar|/author|/iletisim|/kunye|/hakkimizda|/reklam|"
    r"/gizlilik|/cerez|/uye|/login|/galeri/?$|/video/?$|javascript:|mailto:|whatsapp:|#)", re.I)


def extract_headlines(html: bytes, base_url: str, max_items: int = 40) -> list[dict]:
    """RSS'i olmayan siteler için: aynı alan adına giden, haber başlığına benzeyen bağlantılar."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "form", "noscript"]):
        tag.decompose()
    host = bare_domain(base_url)
    seen_t: set[str] = set()
    seen_u: set[str] = set()
    items: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or _BAD_LINK.search(href):
            continue
        url = normalize_url(urljoin(base_url, href))
        if bare_domain(url) != host:
            continue
        path = urlsplit(url).path
        if len(path) < 15:      # /spor, /gundem gibi bölüm bağlantıları
            continue
        title = clean_text(a.get("title") or "") or ""
        text = clean_text(a.get_text(" ", strip=True))
        if len(text) > len(title):
            title = text
        if not (25 <= len(title) <= 220) or title.count("|") > 1 or "{" in title:
            continue
        if len(title.split()) < 4:
            continue
        key = title.lower()
        if key in seen_t or url in seen_u:
            continue
        seen_t.add(key)
        seen_u.add(url)
        items.append({"title": title, "url": url, "summary": "", "published": ""})
        if len(items) >= max_items:
            break
    return items


def _to_articles(items: list[dict], src: Source) -> list[Article]:
    out = []
    for it in items:
        out.append(Article(
            title=it["title"], url=it["url"], source=src.domain, city=src.city,
            summary=it.get("summary", ""), published=it.get("published", ""),
            id=article_id(it["url"], src.domain, it["title"]),
        ))
    return out


# ---------------------------------------------------------------------------
# Keşif (yeni kaynak doğrulama)
# ---------------------------------------------------------------------------

async def discover(client: httpx.AsyncClient, src: Source, max_items: int = 40) -> tuple[Source, int]:
    """Kaynağın çalışan kök adresini ve beslemesini bulur; src alanlarını günceller.
    Dönen sayı: bulunan başlık adedi."""
    d = src.domain
    home = None
    for base in (f"https://{d}", f"https://www.{d}", f"http://{d}"):
        home = await _get(client, base)
        if home is not None:
            break
    if home is None:
        src.status, src.method, src.feed = "erisilemedi", "", ""
        return src, 0

    final = urlsplit(str(home.url))
    base = f"{final.scheme}://{final.netloc}"
    src.base = base
    moved = bare_domain(base) != d      # alan adı başka siteye yönlenmiş (satılmış/taşınmış olabilir)

    candidates = extract_feed_links(home.content, base)
    candidates += [base + p for p in FEED_PATHS if base + p not in candidates]
    best: tuple[int, str] = (0, "")
    for i, url in enumerate(candidates):
        r = await _get(client, url)
        if r is None:
            continue
        n = len(parse_feed(r.content, base, max_items))
        if n > best[0]:
            best = (n, url)
        if n >= 10 or (n >= 3 and i >= 5):
            break
    if best[0] >= 3:
        src.feed, src.method, src.status = best[1], "rss", "ok"
        return src, best[0]

    heads = extract_headlines(home.content, base, max_items)
    if len(heads) >= 8 and not moved:
        src.feed, src.method, src.status = "", "html", "ok"
        return src, len(heads)
    src.feed, src.method, src.status = "", "", "bos"
    return src, 0


# ---------------------------------------------------------------------------
# Günlük okuma
# ---------------------------------------------------------------------------

async def fetch_source(client: httpx.AsyncClient, src: Source, max_items: int = 40) -> tuple[list[Article], str]:
    """Bir kaynağın güncel başlıklarını getirir. Dönüş: (makaleler, durum[ok|bos|hata])."""
    base = src.base or f"https://{src.domain}"
    if src.method == "rss" and src.feed:
        r = await _get(client, src.feed)
        if r is not None:
            items = parse_feed(r.content, base, max_items)
            if items:
                return _to_articles(items, src), "ok"
    # RSS yok ya da bozulmuş → ana sayfadan başlıklar
    r = await _get(client, base)
    if r is None:
        return [], "hata"
    items = extract_headlines(r.content, base, max_items)
    return (_to_articles(items, src), "ok") if items else ([], "bos")


async def scan_sources(
    sources: list[Source],
    *,
    concurrency: int = 24,
    timeout: float = 20,
    max_items: int = 40,
    on_progress=None,           # callable(done, total, source, n_articles, status)
) -> tuple[list[Article], dict[str, dict]]:
    """Tüm kaynakları eşzamanlı tarar. Dönüş: (makaleler, {alan_adı: {status, n}})."""
    sem = asyncio.Semaphore(concurrency)
    articles: list[Article] = []
    health: dict[str, dict] = {}
    done = 0

    async with make_client(timeout, concurrency * 2) as client:
        async def one(src: Source):
            nonlocal done
            async with sem:
                try:
                    arts, status = await asyncio.wait_for(fetch_source(client, src, max_items), timeout * 3)
                except Exception as exc:  # noqa: BLE001 — tek kaynak hatası taramayı durdurmamalı
                    logger.debug("kaynak hatası %s: %s", src.domain, exc)
                    arts, status = [], "hata"
            articles.extend(arts)
            health[src.domain] = {"status": status, "n": len(arts)}
            done += 1
            if on_progress:
                res = on_progress(done, len(sources), src, len(arts), status)
                if asyncio.iscoroutine(res):
                    await res

        await asyncio.gather(*(one(s) for s in sources))

    # Aynı makale aynı kaynaktan iki kez gelmesin
    uniq: dict[str, Article] = {}
    for a in articles:
        uniq.setdefault(a.id, a)
    return list(uniq.values()), health


async def discover_sources(sources: list[Source], *, concurrency: int = 30, timeout: float = 15,
                           on_progress=None) -> dict[str, int]:
    """Verilen kaynakların beslemelerini keşfeder (yerinde günceller)."""
    sem = asyncio.Semaphore(concurrency)
    counts: dict[str, int] = {}
    done = 0
    async with make_client(timeout, concurrency * 2) as client:
        async def one(src: Source):
            nonlocal done
            async with sem:
                try:
                    _, n = await asyncio.wait_for(discover(client, src), timeout * 8)
                except Exception:  # noqa: BLE001
                    src.status, src.method, src.feed, n = "erisilemedi", "", "", 0
            counts[src.domain] = n
            done += 1
            if on_progress:
                on_progress(done, len(sources), src, n)
        await asyncio.gather(*(one(s) for s in sources))
    return counts


# ---------------------------------------------------------------------------
# Derin arama: sitelerin kendi arama sayfalarını kullanarak geçmişe dönük tarama
# ---------------------------------------------------------------------------

SEARCH_TEMPLATES = [
    ("wp-feed", "{base}/?s={q}&feed=rss2"),
    ("wp", "{base}/?s={q}"),
    ("arama-q", "{base}/arama?q={q}"),
    ("ara-q", "{base}/ara?q={q}"),
    ("search-q", "{base}/search?q={q}"),
    ("arama-key", "{base}/arama?key={q}"),
    ("ara-key", "{base}/ara?key={q}"),
]


async def site_search(client: httpx.AsyncClient, src: Source, query: str, template: str = "",
                      max_items: int = 30, accept=None) -> tuple[list[Article], str]:
    """Sitenin kendi aramasında `query` arar; (sonuçlar, çalışan kalıbın adı) döndürür.

    Arama desteklemeyen siteler çoğu kalıba ana sayfayı ya da olağan beslemeyi döndürür; bu yüzden bir
    kalıp ancak `accept(makale)` koşulunu sağlayan en az bir sonuç verirse "çalışıyor" sayılır ve
    yalnızca koşulu sağlayan sonuçlar döner."""
    base = src.base or f"https://{src.domain}"
    templates = [t for t in SEARCH_TEMPLATES if t[0] == template] or SEARCH_TEMPLATES
    q = quote_plus(query)
    for name, tpl in templates:
        r = await _get(client, tpl.format(base=base, q=q))
        if r is None:
            continue
        items = parse_feed(r.content, base, max_items) if name == "wp-feed" else extract_headlines(r.content, base, max_items)
        arts = _to_articles(items, src)
        if accept:
            arts = [a for a in arts if accept(a)]
        if arts:
            return arts, name
    return [], ""
