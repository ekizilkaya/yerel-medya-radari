"""Kaynak listesini günceller ve doğrular → data/sources.json

Adaylar üç yerden gelir:
  1. data/seed_sources.csv   — önceki sürümün il başına 3 sitelik çekirdek listesi
  2. BİK (Basın İlan Kurumu) "Aylık Süreli Yayınlar Listesi" — resmî ilan/reklam alabilen
     internet haber siteleri, il il (https://ilanbis.bik.gov.tr/Uygulamalar/AylikListe)
  3. data/extra_sources.csv  — elle eklenenler (il,alan_adi)

Her aday için çalışan kök adres ve RSS/Atom beslemesi keşfedilir; RSS yoksa ana sayfadan
başlık çıkarılabiliyorsa "html" yöntemiyle kaydedilir.

Kullanım:
  python scripts/update_sources.py                # BİK'i indir, yalnız yeni/başarısız adayları dene
  python scripts/update_sources.py --recheck      # tüm kaynakları yeniden doğrula
  python scripts/update_sources.py --no-bik       # BİK'e gitmeden yalnız CSV'ler
  python scripts/update_sources.py --bik-html dosya.html   # önceden indirilmiş BİK sayfası
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html as htmllib
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from radar.cities import CITY_NAMES, canon_city  # noqa: E402
from radar.paths import DATA, today_tr  # noqa: E402
from radar.scraper import HEADERS, discover_sources  # noqa: E402
from radar.sources import Source, load_sources, save_sources  # noqa: E402
from radar.textnorm import bare_domain  # noqa: E402

BIK_URL = "https://ilanbis.bik.gov.tr/Uygulamalar/AylikListe"

# Ülke geneline yayın yapan; "yerel medya" taramasına girmemesi gereken siteler
NATIONAL = set("""
hurriyet.com.tr milliyet.com.tr sabah.com.tr sozcu.com.tr cumhuriyet.com.tr haberturk.com ntv.com.tr
cnnturk.com trthaber.com aa.com.tr iha.com.tr dha.com.tr ensonhaber.com yenisafak.com star.com.tr
aksam.com.tr takvim.com.tr posta.com.tr karar.com birgun.net evrensel.net t24.com.tr diken.com.tr
gazeteduvar.com.tr odatv.com haber7.com mynet.com internethaber.com haberler.com sondakika.com
milligazete.com.tr yeniakit.com.tr turkiyegazetesi.com.tr dunya.com ekonomim.com bloomberght.com
fanatik.com.tr fotomac.com.tr ahaber.com.tr tgrthaber.com halktv.com.tr tele1.com.tr nefes.com.tr
korkusuz.com.tr turkgun.com dirilispostasi.com dogruhaber.com.tr yenicaggazetesi.com.tr aydinlik.com.tr
ortadogugazetesi.com gazetevatan.com onedio.com medyascope.tv bianet.org artigercek.com kisadalga.net
yenibirlik.com.tr gunes.com gazetepencere.com patronlardunyasi.com ekonomigazetesi.com benguturk.com
haberglobal.com.tr ulusal.com.tr sokgazetesi.com.tr yeniasya.com.tr analizgazetesi.com.tr tv100.com
""".split())

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)/?$")
_SKIP_CONTENT = {"Spor", "Sektör", "Ticari", "Ekonomi", "Kültür"}


def _cells(row_html: str) -> list[str]:
    return [htmllib.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.S)]


def parse_bik(page: str) -> list[dict]:
    """BİK aylık liste sayfasından internet haber sitelerini çıkarır."""
    start, end = page.find('id="tab1"'), page.find('id="tab2"')
    tab = page[start:end if end > start else None]
    parts = re.split(r"<!--\s*#region\s*(.*?)\s*-->", tab)
    out: list[dict] = []
    for i in range(1, len(parts) - 1, 2):
        region = htmllib.unescape(parts[i])
        if "nternet Haber Site" not in region or "Giri" in region:
            continue
        origin = "bik-reklam" if region.startswith("Reklam") else ("bik-bekleme" if "Bekleme" in region else "bik-ilan")
        blocks = re.split(r'<div class="text-center">(.*?)</div>', parts[i + 1], flags=re.S)
        for j in range(1, len(blocks) - 1, 2):
            heads = [htmllib.unescape(re.sub(r"<[^>]+>|\s+", " ", h)).strip()
                     for h in re.findall(r"<h4[^>]*>(.*?)</h4>", blocks[j], flags=re.S)]
            block_city = canon_city(heads[-1].replace("İnternet Haber Siteleri", "").strip()) if heads else None
            body = blocks[j + 1].split("</thead>")[-1]
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
                c = _cells(row)
                if len(c) < 3:
                    continue
                m = _DOMAIN_RE.match(c[1].lower().strip())
                if not m:
                    continue
                if origin == "bik-reklam":          # Sıra, Ad, Yayın Yeri, Yayın İçeriği, Not
                    city, label = canon_city(c[2]), (c[3] if len(c) > 3 else "")
                    if label in _SKIP_CONTENT:
                        continue
                else:                                # Sıra, Ad, Kategori, Not
                    city, label = block_city, c[2]
                if city:
                    out.append({"domain": bare_domain(m.group(1)), "city": city, "origin": origin, "bik": label})
    return out


def fetch_bik() -> str:
    print(f"BİK listesi indiriliyor: {BIK_URL}")
    r = httpx.get(BIK_URL, headers=HEADERS, timeout=120, follow_redirects=True)
    r.raise_for_status()
    return r.text


def read_csv(path: Path, origin: str) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in csv.reader(l for l in f if l.strip() and not l.startswith("#")):
            if len(line) < 2:
                continue
            city, dom = canon_city(line[0].strip()), bare_domain(line[1])
            if city and dom:
                rows.append({"domain": dom, "city": city, "origin": origin, "bik": ""})
            else:
                print(f"  ! {path.name}: tanınmayan satır {line}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recheck", action="store_true", help="tüm kaynakları yeniden doğrula")
    ap.add_argument("--no-bik", action="store_true", help="BİK listesini indirme")
    ap.add_argument("--bik-html", help="önceden indirilmiş BİK sayfası")
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="en fazla N aday dene (sınama için)")
    args = ap.parse_args()

    existing = {s.domain: s for s in load_sources(scope=None)}
    candidates = read_csv(DATA / "seed_sources.csv", "seed") + read_csv(DATA / "extra_sources.csv", "manuel")
    if not args.no_bik:
        try:
            page = Path(args.bik_html).read_text(encoding="utf-8", errors="ignore") if args.bik_html else fetch_bik()
            bik = parse_bik(page)
            print(f"BİK: {len(bik)} internet haber sitesi, {len({b['city'] for b in bik})} il")
            candidates += bik
        except Exception as exc:  # noqa: BLE001
            print(f"  ! BİK listesi alınamadı ({exc}); mevcut liste ve CSV'lerle devam ediliyor.")

    # Birleştir (aynı alan adı birden çok listede olabilir)
    for c in candidates:
        s = existing.get(c["domain"])
        if s is None:
            s = existing[c["domain"]] = Source(domain=c["domain"], city=c["city"])
        if c["origin"] not in s.origin:
            s.origin.append(c["origin"])
        if c["bik"] and not s.bik:
            s.bik = c["bik"]
        if c["origin"] in ("seed", "manuel"):
            s.city = c["city"]              # elle verilen il önceliklidir
    for s in existing.values():
        s.scope = "ulusal" if (s.domain in NATIONAL or "Genel" in s.bik) else "yerel"

    todo = [s for s in existing.values() if s.scope == "yerel" and (args.recheck or s.status != "ok")]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Toplam aday: {len(existing)} | doğrulanacak: {len(todo)}")

    if todo:
        today = today_tr().isoformat()

        def progress(done, total, src, n):
            src.checked = today
            if done % 25 == 0 or done == total:
                print(f"  [{done}/{total}] {src.domain:<34} {src.method or src.status:<12} {n}", flush=True)

        asyncio.run(discover_sources(todo, concurrency=args.concurrency, on_progress=progress))

    sources = list(existing.values())
    save_sources(sources)

    local = [s for s in sources if s.scope == "yerel"]
    usable = [s for s in local if s.usable]
    by_method = Counter(s.method or s.status or "?" for s in local)
    per_city = Counter(s.city for s in usable)
    print(f"\nYerel kaynak: {len(local)} | kullanılabilir: {len(usable)} | yöntem/durum: {dict(by_method)}")
    print("İl başına kullanılabilir kaynak (en az → en çok):")
    ordered = sorted(CITY_NAMES, key=lambda c: per_city.get(c, 0))
    print("  " + ", ".join(f"{c} {per_city.get(c, 0)}" for c in ordered[:12]) + " …")
    print("  " + ", ".join(f"{c} {per_city.get(c, 0)}" for c in ordered[-8:]))
    missing = [c for c in CITY_NAMES if per_city.get(c, 0) == 0]
    if missing:
        print("  ! Kaynağı olmayan iller:", ", ".join(missing))


if __name__ == "__main__":
    main()
