"""Pano verisi: eşleşmelerden zaman serisi, il dağılımı, terim dökümü, sıçrama tespiti, RSS.

Aynı fonksiyonlar hem GitHub Actions'ta (dosyalardan) hem yerel uygulamada (SQLite'tan)
kullanılır; bu yüzden girdi olarak düz `dict` listeleri alırlar.

Eşleşme kaydı (match):
  d gün · i kimlik · t başlık · u adres · s kaynak · c il · p yayın zamanı
  k eşleşen terimler · w 't' başlıkta / 's' yalnız özette · m 're' kalıp / 'sem' anlamsal aday
  sc anlamsal benzerlik · g hikâye grubu (aynı haberin kopyaları aynı g değerini taşır)
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

from .cities import CITY_NAMES
from .paths import now_tr
from .textnorm import title_key, trigrams
from .topics import Topic

PALETTE = ["#2f6fde", "#d9480f", "#0c8599", "#9c36b5", "#2b8a3e", "#c2255c", "#e67700", "#5f3dc4"]


def daterange(start: date, end: date) -> list[str]:
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)] if n >= 0 else []


# ---------------------------------------------------------------------------
# Hikâye grupları: aynı ajans haberinin onlarca yerel sitedeki kopyalarını tek hikâye say
# ---------------------------------------------------------------------------

def assign_groups(items: list[dict], threshold: float = 0.6) -> None:
    """Her güne ait eşleşmelere `g` (grup temsilcisinin kimliği) atar. Yerinde değiştirir."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in items:
        by_day[m["d"]].append(m)
    for day_items in by_day.values():
        reps: list[tuple[str, set[str], str]] = []      # (anahtar, trigramlar, grup kimliği)
        exact: dict[str, str] = {}
        for m in day_items:
            key = title_key(m["t"])
            if key in exact:
                m["g"] = exact[key]
                continue
            tri = trigrams(m["t"])
            gid = None
            if len(reps) <= 600:                        # çok büyük günlerde ikinci aşamayı atla
                for _, rtri, rid in reps:
                    inter = len(tri & rtri)
                    if inter and inter / (len(tri) + len(rtri) - inter) >= threshold:
                        gid = rid
                        break
            if gid is None:
                gid = m["i"]
                reps.append((key, tri, gid))
            exact[key] = gid
            m["g"] = gid


# ---------------------------------------------------------------------------
# Sıçrama (olağandışı artış) tespiti
# ---------------------------------------------------------------------------

def detect_spikes(days: list[str], counts: list[int], window: int = 28, min_n: int = 5, z_min: float = 3.0) -> list[dict]:
    spikes = []
    for i in range(7, len(counts)):
        hist = counts[max(0, i - window):i]
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / len(hist)
        sd = max(math.sqrt(var), math.sqrt(max(mean, 1.0)))      # Poisson tabanı: küçük sayılarda aşırı duyarlılığı önler
        z = (counts[i] - mean) / sd
        if counts[i] >= min_n and z >= z_min:
            spikes.append({"d": days[i], "n": counts[i], "expected": round(mean, 1), "z": round(z, 1)})
    return spikes


# ---------------------------------------------------------------------------
# Konu raporu
# ---------------------------------------------------------------------------

def build_topic_report(topic: Topic, matches: list[dict], *, start: str | None = None,
                       end: str | None = None, max_items: int = 1200) -> dict:
    """Bir konunun pano verisini üretir. `matches` içindeki her kayıtta en az d,i,t,s,c bulunmalı."""
    matches = sorted(matches, key=lambda m: (m["d"], m.get("p", ""), m["i"]))
    ungrouped = [m for m in matches if m.get("m") != "sem" and "g" not in m]
    if ungrouped:
        assign_groups([m for m in matches if m.get("m") != "sem"])

    today = now_tr().date().isoformat()
    first = start or (matches[0]["d"] if matches else today)
    last = end or today
    days = daterange(date.fromisoformat(first), date.fromisoformat(last))
    idx = {d: i for i, d in enumerate(days)}
    n = len(days)

    total, title_n, uniq, sem = [0] * n, [0] * n, [0] * n, [0] * n
    city_days: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    term_days: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    src_all: Counter = Counter()
    src_city: dict[str, str] = {}
    seen_groups: set[tuple[str, str]] = set()
    seen_city_groups: set[tuple[str, str, str]] = set()

    for m in matches:
        i = idx.get(m["d"])
        if i is None:
            continue
        if m.get("m") == "sem":
            sem[i] += 1
            continue
        total[i] += 1
        if m.get("w", "t") == "t":
            title_n[i] += 1
        g = m.get("g", m["i"])
        if (m["d"], g) not in seen_groups:
            seen_groups.add((m["d"], g))
            uniq[i] += 1
        cities = [c.strip() for c in m.get("c", "").split("/") if c.strip()] or ["?"]
        for c in cities:
            cell = city_days[c][i]
            cell[0] += 1
            if (m["d"], g, c) not in seen_city_groups:
                seen_city_groups.add((m["d"], g, c))
                cell[1] += 1
        for k in m.get("k", []):
            term_days[k][i] += 1
        src_all[m["s"]] += 1
        src_city[m["s"]] = m.get("c", "")

    regex_items = [m for m in matches if m.get("m") != "sem"]
    sem_items = [m for m in matches if m.get("m") == "sem"]
    group_size = Counter((m["d"], m.get("g", m["i"])) for m in regex_items)

    def item(m: dict) -> dict:
        out = {"d": m["d"], "t": m["t"], "u": m.get("u", ""), "s": m["s"], "c": m.get("c", ""),
               "k": m.get("k", []), "w": m.get("w", "t")}
        if m.get("m") == "sem":
            out["m"] = "sem"
            out["sc"] = m.get("sc")
        else:
            g = m.get("g", m["i"])
            out["g"] = g
            if g == m["i"]:
                out["gn"] = group_size[(m["d"], g)]
        if m.get("x"):
            out["x"] = m["x"]
        return out

    return {
        "slug": topic.slug,
        "title": topic.title,
        "keyword": topic.keyword,
        "description": topic.description,
        "color": topic.color,
        "generated": now_tr().isoformat(timespec="minutes"),
        "terms": [t.to_dict() for t in topic.terms],
        "context": topic.context,
        "exclude": topic.exclude,
        "days": days,
        "total": total,          # kalıp eşleşmeleri (haber sayısı)
        "in_title": title_n,     # başlığında geçenler
        "uniq": uniq,            # tekil hikâye sayısı (kopyalar birleştirilmiş)
        "sem": sem,              # anlamsal adaylar (kalıba takılmayan)
        "city_days": {c: [[i, v[0], v[1]] for i, v in sorted(cd.items())] for c, cd in sorted(city_days.items())},
        "term_days": {k: [[i, v] for i, v in sorted(td.items())] for k, td in term_days.items()},
        "sources": [[s, src_city.get(s, ""), c] for s, c in src_all.most_common(60)],
        "spikes": detect_spikes(days, total),
        "count": len(regex_items),
        "items": [item(m) for m in reversed(regex_items[-max_items:])],
        "sem_items": [item(m) for m in reversed(sem_items[-150:])],
    }


def topic_card(report: dict) -> dict:
    """Genel bakış sayfası için konu özeti."""
    t = report["total"]
    return {
        "slug": report["slug"], "title": report["title"], "description": report["description"],
        "color": report["color"], "count": report["count"],
        "last7": sum(t[-7:]), "prev7": sum(t[-14:-7]), "last30": sum(t[-30:]),
        "spark": t[-60:], "spark_start": report["days"][-len(t[-60:])] if report["days"] else "",
        "n_terms": sum(1 for x in report["terms"] if x.get("on", True)),
        "spike": report["spikes"][-1] if report["spikes"] and report["spikes"][-1]["d"] >= report["days"][-3:][0] else None,
        "latest": report["items"][:3],
    }


# ---------------------------------------------------------------------------
# Kapsam (payda) verisi
# ---------------------------------------------------------------------------

def build_coverage(stats: dict[str, dict], source_cov: dict[str, dict]) -> dict:
    """Gün gün ve il il taranan yeni başlık sayıları — haritayı 'bin başlıkta' ölçeğine çevirmek için."""
    days = sorted(stats)
    if days:
        days = daterange(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
    cities = {c: [0] * len(days) for c in CITY_NAMES}
    total, ok, fail = [0] * len(days), [0] * len(days), [0] * len(days)
    for i, d in enumerate(days):
        e = stats.get(d)
        if not e:
            continue
        total[i] = e.get("n", 0)
        ok[i], fail[i] = e.get("src_ok", 0), e.get("src_fail", 0)
        for c, v in e.get("c", {}).items():
            if c in cities:
                cities[c][i] = v
    return {
        "days": days, "total": total, "src_ok": ok, "src_fail": fail, "cities": cities,
        "sources": {c: source_cov.get(c, {"total": 0, "usable": 0, "rss": 0}) for c in CITY_NAMES},
    }


# ---------------------------------------------------------------------------
# Konu başına RSS beslemesi
# ---------------------------------------------------------------------------

def build_rss(report: dict, site_url: str = "") -> str:
    now = format_datetime(now_tr())
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
        f"<title>{escape(report['title'])} — Yerel Medya Radarı</title>",
        f"<link>{escape(site_url or 'https://github.com')}</link>",
        f"<description>{escape(report.get('description') or report['title'])}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in report["items"][:80]:
        if not it.get("u"):
            continue
        parts.append(
            "<item>"
            f"<title>{escape(it['t'])}</title><link>{escape(it['u'])}</link>"
            f"<guid isPermaLink=\"true\">{escape(it['u'])}</guid>"
            f"<category>{escape(it.get('c', ''))}</category>"
            f"<description>{escape(it.get('c', '') + ' · ' + it['s'] + ' · ' + ', '.join(it.get('k', [])))}</description>"
            f"<pubDate>{format_datetime(now_tr().replace(year=int(it['d'][:4]), month=int(it['d'][5:7]), day=int(it['d'][8:10]), hour=12, minute=0, second=0, microsecond=0))}</pubDate>"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "\n".join(parts)
