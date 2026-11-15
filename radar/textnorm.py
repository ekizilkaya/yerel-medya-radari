"""Türkçe'ye duyarlı metin normalleştirme, URL temizleme ve kısa özet (hash) yardımcıları."""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TR_UPPER = str.maketrans({"I": "ı", "İ": "i"})
_CIRCUMFLEX = str.maketrans({"â": "a", "î": "i", "û": "u", "Â": "a", "Î": "i", "Û": "u"})
_APOS = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'", "ʼ": "'"})
_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-zçğıöşü0-9]+")


def tr_lower(s: str) -> str:
    """Türkçe kurallarıyla küçük harf: I→ı, İ→i; şapkalı harfler sadeleşir, kesme işaretleri birleşir."""
    s = s.translate(_TR_UPPER).lower().translate(_CIRCUMFLEX).translate(_APOS)
    return s.replace("̇", "")  # 'İ'.lower() artığı birleşen nokta


def fold_ascii(s: str) -> str:
    """Küçük harfli Türkçe metni ASCII'ye indirger (ç→c, ğ→g, ı→i, ö→o, ş→s, ü→u)."""
    return s.translate(_FOLD)


def clean_text(s: str, limit: int = 0) -> str:
    """HTML etiketlerini/varlıklarını ayıklar, boşlukları toplar; limit verilirse keser."""
    if not s:
        return ""
    s = html.unescape(_TAG_RE.sub(" ", s))
    s = _WS_RE.sub(" ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def tokens(s: str) -> list[str]:
    return _WORD_RE.findall(tr_lower(s))


_STOP = set(
    "bir ve ile de da bu şu o için olan ne her çok daha gibi ise ya ama ki en son mi mı mu mü "
    "veya hem ancak kadar sonra önce göre karşı üzere diye ilk yeni var yok oldu olacak olarak "
    "etti edildi yapıldı yaptı dedi açıklandı belli nasıl neden kim hangi the".split()
)


def content_tokens(s: str) -> list[str]:
    return [t for t in tokens(s) if len(t) > 2 and t not in _STOP]


def title_key(title: str) -> str:
    """Aynı ajans haberinin farklı sitelerdeki kopyalarını yakalamak için başlık anahtarı."""
    return " ".join(tokens(title))


def trigrams(title: str) -> set[str]:
    t = title_key(title)
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else {t}


_TRACKING = re.compile(r"^(utm_|fbclid|gclid|yclid|mc_|ref$|source$|share$)", re.I)


def normalize_url(url: str) -> str:
    """İzleme parametrelerini ve parçayı (#) atar; şema/alan adını küçültür."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.netloc:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, urlencode(q), ""))


def bare_domain(url_or_domain: str) -> str:
    d = (url_or_domain or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0]
    return re.sub(r"^www\.", "", d).strip(".")


def short_hash(s: str, n: int = 12) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:n]


def article_id(url: str, source: str, title: str) -> str:
    """Makale kimliği: URL varsa URL'den (alan adı öneki olmadan), yoksa kaynak+başlıktan."""
    u = normalize_url(url)
    if u:
        p = urlsplit(u)
        return short_hash(re.sub(r"^www\.", "", p.netloc) + p.path + ("?" + p.query if p.query else ""))
    return short_hash(source + "|" + title_key(title))
