"""Konu tanımları (data/topics/*.json) ve Türkçe çekim eklerine duyarlı eşleştirici.

Bir konu; terimler (çekirdek / zayıf), bağlam sözcükleri ve dışlama kalıplarından oluşur.
Anlamsal genişletme (radar.expand) bu dosyaları yerel dil modeliyle üretir; günlük tarama
(GitHub Actions) ise yalnızca buradaki kalıpları kullanır — bulutta LLM gerekmez.

Terim sözdizimi
  fon             → çekim ekleriyle: fon, fonu, fonun, fona, fondan, fonları… (telefon/fonksiyon DEĞİL)
  hibe desteği    → çok sözcüklü; son sözcük çekimlenir
  fonla*          → önek: fonlama, fonlanan, fonlayacak…
  "tam ifade"     → ek almadan, birebir
  KOSGEB          → BÜYÜK harfli kısaltma; harf duyarlı aranır (KOSGEB'den, KOSGEB'in…)
  re:\\bipard\\b    → ham düzenli ifade (küçük harfe çevrilmiş metinde aranır)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .paths import TOPICS_DIR, read_json, today_tr
from .textnorm import fold_ascii, tr_lower

VOWELS = "aeıioöuü"
_FOLD_CLASS = {"ç": "[çc]", "ğ": "[ğg]", "ı": "[ıi]", "ö": "[öo]", "ş": "[şs]", "ü": "[üu]"}
_SOFTEN = {"p": "[pb]", "ç": "[çc]", "t": "[td]", "k": "[kğg]"}

# İsim çekimi: çoğul + iyelik + hâl + (-ki). Ünlü uyumu denetlenmez (gereksiz; yalnızca
# gerçekte var olmayan biçimleri de kabul etmiş oluruz).
SUFFIX = (
    r"(?:l[ae]r)?"
    r"(?:[ıiuü]m[ıiuü]z|[ıiuü]n[ıiuü]z|m[ıiuü]z|n[ıiuü]z|l[ae]r[ıi]|s[ıiuü]|[ıiuü]m|[ıiuü]n|[ıiuü]|m|n)?"
    r"(?:n?[ıiuü]n|[yn]?[ae]|[yn]?[ıiuü]|n?[dt][ae]n?|y?l[ae]|[cç][ae])?"
    r"(?:ki)?"
)
_WB, _WE = r"(?<![\w'])", r"(?![\w])"


def _lit(word: str) -> str:
    """Sözcüğü regex'e çevirir; Türkçe'ye özgü harfler ASCII yazımını da kabul eder (teşvik→tesvik)."""
    return "".join(_FOLD_CLASS.get(ch, re.escape(ch)) for ch in word)


def _stem(word: str) -> str:
    """Sözcük gövdesi: son ünsüz yumuşaması (k→ğ, p→b, ç→c, t→d) ve ünlü düşmesi (vakıf→vakfı)."""
    w = word
    if len(w) >= 5 and w[-1] not in VOWELS and w[-2] in "ıiuü" and w[-3] not in VOWELS:
        return _lit(w[:-2]) + "(?:" + _lit(w[-2]) + ")?" + _SOFTEN.get(w[-1], _lit(w[-1]))
    if len(w) >= 3 and w[-1] in _SOFTEN and w[-2] in VOWELS + "nrl":
        return _lit(w[:-1]) + _SOFTEN[w[-1]]
    return _lit(w)


def term_regex(term: str) -> tuple[str, bool]:
    """Terimi düzenli ifadeye çevirir. Dönüş: (kalıp, harf_duyarlı_mı)."""
    t = term.strip()
    if t.startswith("re:"):
        return t[3:], False
    if len(t) >= 2 and t[0] == t[-1] == '"':
        words = tr_lower(t[1:-1]).split()
        return _WB + r"[\s\-]+".join(_lit(w) for w in words) + _WE, False
    letters = [ch for ch in t if ch.isalpha()]
    if len(letters) >= 2 and all(ch.isupper() for ch in letters) and " " not in t:
        # Kısaltma: özgün metinde, harf duyarlı; kesme işaretiyle ek alabilir
        return _WB + re.escape(t) + r"(?:['’][a-zçğıöşü]+)?" + _WE, True
    prefix = t.endswith("*")
    words = tr_lower(t.rstrip("*")).split()
    if not words:
        return r"(?!)", False
    head = [_lit(w) for w in words[:-1]]
    last = words[-1]
    tail = _lit(last) + r"\w*" if prefix else _stem(last) + r"'?" + SUFFIX
    return _WB + r"[\s\-]+".join(head + [tail]) + _WE, False


def slugify(text: str) -> str:
    s = fold_ascii(tr_lower(text))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:48] or "konu"


@dataclass
class Term:
    t: str                      # terim (sözdizimi için modül açıklamasına bakın)
    kind: str = "core"          # core: tek başına yeter | weak: aynı metinde bir bağlam sözcüğü de gerekir
    on: bool = True
    src: str = ""               # seed | llm | korpus | birliktelik | elle
    sim: float | None = None    # tohum ile anlamsal benzerlik (gömme modeli)
    prec: float | None = None   # LLM denetimine göre kesinlik tahmini (0-1)
    hits: int | None = None     # yerel arşivde eşleşen başlık sayısı

    def to_dict(self) -> dict:
        d = {"t": self.t, "kind": self.kind, "on": self.on}
        for k in ("src", "sim", "prec", "hits"):
            v = getattr(self, k)
            if v not in ("", None):
                d[k] = round(v, 3) if isinstance(v, float) else v
        return d


@dataclass
class Topic:
    slug: str
    title: str
    keyword: str = ""
    description: str = ""
    terms: list[Term] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)   # tipik başlıklar (anlamsal arama için)
    fields: list[str] = field(default_factory=lambda: ["title", "summary"])
    semantic: bool = True       # regex'e takılmayan anlamsal adayları da topla
    active: bool = True         # günlük taramaya dahil
    color: str = ""
    created: str = ""
    updated: str = ""
    model: dict = field(default_factory=dict)           # genişletmede kullanılan modeller

    # -- serileştirme -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "title": self.title, "keyword": self.keyword,
            "description": self.description,
            "terms": [t.to_dict() for t in self.terms],
            "context": self.context, "exclude": self.exclude, "examples": self.examples,
            "fields": self.fields, "semantic": self.semantic, "active": self.active,
            "color": self.color, "created": self.created, "updated": self.updated, "model": self.model,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Topic":
        terms = []
        for t in d.get("terms", []):
            if isinstance(t, str):
                t = {"t": t}
            terms.append(Term(**{k: v for k, v in t.items() if k in Term.__dataclass_fields__}))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "terms"}
        known.setdefault("slug", slugify(d.get("title") or d.get("keyword") or "konu"))
        known.setdefault("title", d.get("keyword") or known["slug"])
        return cls(terms=terms, **known)

    def signature(self) -> str:
        """Eşleştirmeyi etkileyen alanların özeti; değişirse geçmiş yeniden eşleştirilir."""
        payload = json.dumps({
            "terms": sorted((t.t, t.kind) for t in self.terms if t.on),
            "context": sorted(self.context), "exclude": sorted(self.exclude), "fields": self.fields,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

    def matcher(self) -> "TopicMatcher":
        return TopicMatcher(self)


def _compile(pattern: str, case_sensitive: bool) -> re.Pattern | None:
    try:
        return re.compile(pattern) if case_sensitive else re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


class TopicMatcher:
    """Bir konunun derlenmiş kalıpları."""

    def __init__(self, topic: Topic):
        self.topic = topic
        self.terms: list[tuple[str, str, re.Pattern, bool]] = []   # (terim, tür, kalıp, harf_duyarlı)
        low_parts, raw_parts = [], []
        for t in topic.terms:
            if not t.on or not t.t.strip():
                continue
            pat, cs = term_regex(t.t)
            rx = _compile(pat, cs)
            if rx is None:
                continue
            self.terms.append((t.t, t.kind, rx, cs))
            (raw_parts if cs else low_parts).append(pat)
        self._pre_low = _compile("|".join(f"(?:{p})" for p in low_parts), False) if low_parts else None
        self._pre_raw = _compile("|".join(f"(?:{p})" for p in raw_parts), True) if raw_parts else None
        self._exclude = [rx for rx in (_compile(term_regex(e)[0], False) for e in topic.exclude if e.strip()) if rx]
        # Bir terim kendi kendisinin bağlamı olamaz ("destek" zayıf terimse bağlamdaki "destek" sayılmaz)
        own = {tr_lower(t[0]) for t in self.terms}
        ctx = [term_regex(c)[0] for c in topic.context if c.strip() and tr_lower(c) not in own]
        self._context = _compile("|".join(f"(?:{p})" for p in ctx), False) if ctx else None

    def match_text(self, raw: str) -> list[str]:
        """Metinde konuya işaret eden terimleri döndürür (yoksa boş liste)."""
        if not raw:
            return []
        low = tr_lower(raw)
        for rx in self._exclude:
            low = rx.sub(lambda m: " " * len(m.group()), low)
        if not ((self._pre_low and self._pre_low.search(low)) or (self._pre_raw and self._pre_raw.search(raw))):
            return []
        core, weak = [], []
        for term, kind, rx, cs in self.terms:
            if rx.search(raw if cs else low):
                (core if kind == "core" else weak).append(term)
        if core:
            return core + weak
        if weak and self._context:
            # Bağlam, zayıf terimin kendi sözcüklerinden gelemez ("sermaye piyasası" terimi için
            # bağlamdaki "sermaye" sayılmaz): zayıf terimlerin kapladığı yerler maskelenip öyle aranır.
            masked = low
            for _, kind, rx, cs in self.terms:
                if kind != "core" and not cs:
                    masked = rx.sub(lambda m: " " * len(m.group()), masked)
            if self._context.search(masked):
                return weak
        return []

    def match(self, title: str, summary: str = "") -> tuple[list[str], str] | None:
        """(terimler, 't'|'s') — eşleşme başlıkta mı (t) yalnızca özette mi (s)."""
        in_title = self.match_text(title)
        if "summary" in self.topic.fields and summary:
            both = self.match_text(title + " ¦ " + summary)
            if both:
                return both, ("t" if in_title else "s")
            return None
        return (in_title, "t") if in_title else None


# ---------------------------------------------------------------------------
# Dosya işlemleri
# ---------------------------------------------------------------------------

def load_topics(*, active_only: bool = False) -> list[Topic]:
    topics = []
    if TOPICS_DIR.exists():
        for p in sorted(TOPICS_DIR.glob("*.json")):
            d = read_json(p)
            if not d:
                continue
            d.setdefault("slug", p.stem)
            t = Topic.from_dict(d)
            if active_only and not t.active:
                continue
            topics.append(t)
    return topics


def load_topic(slug: str) -> Topic | None:
    d = read_json(TOPICS_DIR / f"{slug}.json")
    if not d:
        return None
    d.setdefault("slug", slug)
    return Topic.from_dict(d)


def save_topic(topic: Topic) -> None:
    today = today_tr().isoformat()
    topic.created = topic.created or today
    topic.updated = today
    # Terimler satır başına bir tane yazılır: dosya elle düzenlenebilir, git farkları okunaklı kalır
    d = topic.to_dict()
    lines = []
    for key, value in d.items():
        if key == "terms":
            rows = ",\n".join("  " + json.dumps(t, ensure_ascii=False) for t in value)
            lines.append(f' "terms": [\n{rows}\n ]' if value else ' "terms": []')
        else:
            lines.append(f" {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
    path = TOPICS_DIR / f"{topic.slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8", newline="\n")


def delete_topic(slug: str) -> bool:
    p = TOPICS_DIR / f"{slug}.json"
    if p.exists():
        p.unlink()
        return True
    return False
