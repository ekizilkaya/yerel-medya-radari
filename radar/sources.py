"""Kaynak listesi (data/sources.json) — okuma, yazma, özetleme."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .paths import SOURCES_FILE, read_json, today_tr, write_json


@dataclass
class Source:
    domain: str                 # www. öneki olmadan alan adı (kimlik olarak kullanılır)
    city: str                   # standart il adı
    scope: str = "yerel"        # yerel | ulusal (ulusal olanlar varsayılan taramaya girmez)
    origin: list[str] = field(default_factory=list)   # seed | bik-ilan | bik-bekleme | bik-reklam | manuel
    bik: str = ""               # BİK kategorisi / yayın içeriği
    base: str = ""              # çalışan kök adres (https://www.ornek.com)
    feed: str = ""              # doğrulanmış RSS/Atom adresi
    method: str = ""            # rss | html | "" (henüz keşfedilmedi)
    status: str = ""            # ok | bos | erisilemedi | ""
    checked: str = ""           # son doğrulama tarihi
    active: bool = True

    @property
    def usable(self) -> bool:
        return self.active and self.method in ("rss", "html")


def load_sources(*, usable_only: bool = False, scope: str | None = "yerel") -> list[Source]:
    raw = read_json(SOURCES_FILE, {}) or {}
    out = []
    for d in raw.get("sources", []):
        s = Source(**{k: v for k, v in d.items() if k in Source.__dataclass_fields__})
        if scope and s.scope != scope:
            continue
        if usable_only and not s.usable:
            continue
        out.append(s)
    return out


def save_sources(sources: list[Source], note: str = "") -> None:
    sources = sorted(sources, key=lambda s: (s.city, s.domain))
    rows = []
    for s in sources:
        d = asdict(s)
        rows.append({k: v for k, v in d.items() if v not in ("", [], None) or k in ("domain", "city")})
    write_json(
        SOURCES_FILE,
        {
            "updated": today_tr().isoformat(),
            "note": note or "scripts/update_sources.py tarafından üretilir. Elle kaynak eklemek için data/extra_sources.csv kullanın.",
            "count": len(rows),
            "sources": rows,
        },
    )


def coverage(sources: list[Source]) -> dict[str, dict]:
    """İl başına toplam ve kullanılabilir kaynak sayısı."""
    out: dict[str, dict] = {}
    for s in sources:
        c = out.setdefault(s.city, {"total": 0, "usable": 0, "rss": 0})
        c["total"] += 1
        if s.usable:
            c["usable"] += 1
            if s.method == "rss":
                c["rss"] += 1
    return out
