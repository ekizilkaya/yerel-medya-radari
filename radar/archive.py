"""Dosya tabanlı veri deposu (GitHub Actions'ın yazdığı, yerel uygulamanın da okuduğu biçim).

  data/archive/YYYY/MM/YYYY-MM-DD.jsonl.gz   o gün ilk kez görülen tüm başlıklar
                                             (depoya girmez; aylık GitHub Release'lerine yüklenir)
  data/matches/<konu>/YYYY-MM.jsonl          konu eşleşmeleri (depoda)
  data/matches/<konu>/meta.json              konunun eşleştirme imzası
  data/stats/YYYY.json                       gün gün taranan başlık sayıları (payda)
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

import httpx

from .paths import ARCHIVE_DIR, MATCHES_DIR, ROOT, STATS_DIR, read_json, write_json


# ---------------------------------------------------------------------------
# Günlük başlık arşivi
# ---------------------------------------------------------------------------

def day_path(d: date) -> Path:
    return ARCHIVE_DIR / f"{d:%Y}" / f"{d:%m}" / f"{d.isoformat()}.jsonl.gz"


def read_day(d: date) -> list[dict]:
    p = day_path(d)
    if not p.exists():
        return []
    out = []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def write_day(d: date, records: list[dict]) -> Path:
    p = day_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, p)
    return p


def available_days() -> list[date]:
    days = []
    if ARCHIVE_DIR.exists():
        for p in ARCHIVE_DIR.glob("*/*/*.jsonl.gz"):
            try:
                days.append(date.fromisoformat(p.name[:10]))
            except ValueError:
                continue
    return sorted(days)


def seen_state(until: date, window: int) -> tuple[set[str], set[str], bool]:
    """Son `window` günde görülen makale kimlikleri ve kaynaklar. Üçüncü değer: hiç arşiv var mı."""
    ids: set[str] = set()
    sources: set[str] = set()
    any_day = False
    for k in range(1, window + 1):
        recs = read_day(until - timedelta(days=k))
        if recs:
            any_day = True
        for r in recs:
            ids.add(r["i"])
            sources.add(r.get("s", ""))
    return ids, sources, any_day


# ---------------------------------------------------------------------------
# Arşivin GitHub Releases ile eşitlenmesi
# ---------------------------------------------------------------------------

def release_tag(d: date) -> str:
    return f"arsiv-{d:%Y-%m}"


def repo_slug() -> str:
    """'kullanici/depo' — Actions'ta ortam değişkeninden, yerelde git remote'tan."""
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if slug:
        return slug
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    url = url.removesuffix(".git")
    if "github.com" not in url:
        return ""
    return url.split("github.com")[-1].lstrip(":/")


def pull_days(days: list[date], *, quiet: bool = False) -> int:
    """Eksik arşiv günlerini Release'lerden indirir (herkese açık depo için kimlik gerekmez)."""
    slug = repo_slug()
    if not slug:
        return 0
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    got = 0
    with httpx.Client(follow_redirects=True, timeout=60, headers=headers) as client:
        for d in days:
            p = day_path(d)
            if p.exists():
                continue
            url = f"https://github.com/{slug}/releases/download/{release_tag(d)}/{p.name}"
            try:
                r = client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and r.content[:2] == b"\x1f\x8b":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(r.content)
                got += 1
    if not quiet:
        print(f"Arşiv: {got} gün indirildi ({slug})")
    return got


def push_day(d: date) -> bool:
    """Günün arşiv dosyasını aylık Release'e yükler (gh CLI + GH_TOKEN gerekir)."""
    p = day_path(d)
    if not p.exists():
        return False
    tag = release_tag(d)

    def gh(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True, timeout=300)

    try:
        if gh("release", "view", tag).returncode != 0:
            made = gh("release", "create", tag, "--title", f"Başlık arşivi {d:%Y-%m}", "--latest=false",
                      "--notes", "Günlük taramada ilk kez görülen yerel haber başlıkları (JSONL, gzip). "
                                 "Yeni eklenen konuların geçmişini doldurmak için kullanılır.")
            if made.returncode != 0:
                print("  ! Release oluşturulamadı:", made.stderr.strip()[:300])
                return False
        up = gh("release", "upload", tag, str(p), "--clobber")
        if up.returncode != 0:
            print("  ! Arşiv yüklenemedi:", up.stderr.strip()[:300])
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print("  ! gh çalıştırılamadı:", exc)
        return False


# ---------------------------------------------------------------------------
# Konu eşleşmeleri
# ---------------------------------------------------------------------------

def load_matches(slug: str, kind: str | None = None) -> list[dict]:
    """Konunun eşleşmeleri. kind: 're' (kalıp) | 'sem' (anlamsal aday) | None (ikisi birden).
    Anlamsal adaylar ayrı dosyalarda (sem-YYYY-MM.jsonl) tutulur."""
    out: list[dict] = []
    folder = MATCHES_DIR / slug
    if folder.exists():
        for p in sorted(folder.glob("*.jsonl")):
            is_sem = p.name.startswith("sem-")
            if kind and is_sem != (kind == "sem"):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    return out


def _write_month(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        if path.exists():
            path.unlink()
        return
    items = sorted(items, key=lambda m: (m["d"], m.get("s", ""), m["i"]))
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for m in items:
            f.write(json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def replace_matches(slug: str, days: set[str], items: list[dict], kind: str = "re") -> int:
    """Verilen günlere ait eşleşmeleri `items` ile değiştirir (aynı gün yeniden çalıştırma güvenli)."""
    prefix = "sem-" if kind == "sem" else ""
    existing = load_matches(slug, kind)
    kept = [m for m in existing if m["d"] not in days]
    keep_ids = {m["i"] for m in kept}
    fresh = []
    for m in items:
        if m["i"] not in keep_ids:       # eski bir günde zaten eşleşmişse tekrar sayma
            keep_ids.add(m["i"])
            fresh.append(m)
    merged = kept + fresh
    months = {m["d"][:7] for m in existing} | {m["d"][:7] for m in merged}
    for month in months:
        _write_month(MATCHES_DIR / slug / f"{prefix}{month}.jsonl", [m for m in merged if m["d"][:7] == month])
    return len(fresh)


def match_meta(slug: str) -> dict:
    return read_json(MATCHES_DIR / slug / "meta.json", {}) or {}


def save_match_meta(slug: str, meta: dict) -> None:
    write_json(MATCHES_DIR / slug / "meta.json", meta)


# ---------------------------------------------------------------------------
# Günlük tarama istatistikleri (payda)
# ---------------------------------------------------------------------------

def load_stats() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if STATS_DIR.exists():
        for p in sorted(STATS_DIR.glob("*.json")):
            out.update(read_json(p, {}) or {})
    return out


def save_stats_day(day: str, entry: dict) -> None:
    p = STATS_DIR / f"{day[:4]}.json"
    data = read_json(p, {}) or {}
    data[day] = entry
    # Gün başına tek satır: git farkları okunaklı kalsın
    lines = [f'{json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
             for k, v in sorted(data.items())]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8", newline="\n")
