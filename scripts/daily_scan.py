"""Günlük tarama — GitHub Actions bu betiği çalıştırır; yerelde de aynı şekilde çalışır.

  python scripts/daily_scan.py                 tara + eşleştir + pano verisini üret
  python scripts/daily_scan.py --site-only     yalnızca pano verisini yeniden üret
  python scripts/daily_scan.py --pull 21       taramadan önce son 21 günün arşivini Release'lerden indir
  python scripts/daily_scan.py --pull-all      tüm arşivi indir (yeni konunun geçmişini doldurmak için)
  python scripts/daily_scan.py --push          günün arşivini Release'e yükle (gh + GH_TOKEN gerekir)
  python scripts/daily_scan.py --rematch       konuları diskteki tüm arşiv günleriyle baştan eşleştir
  python scripts/daily_scan.py --no-semantic   gömme modeliyle anlamsal aday taramasını atla
  python scripts/daily_scan.py --limit 60      yalnızca 60 kaynak (sınama için)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import archive, pipeline  # noqa: E402
from radar.paths import RUNTIME_DIR, load_config, now_tr  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_semantic_runtime(cfg: dict) -> bool:
    """Bulutta (CPU) anlamsal tarama için küçük gömme modelini ve llama.cpp'yi hazırlar."""
    import httpx

    from radar.llm import models, runtime

    settings = models.load_settings()
    if models.resolve_model("embed", settings) is None:
        url = cfg["model_url"]
        dest = RUNTIME_DIR / "models" / url.rsplit("/", 1)[-1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"Gömme modeli indiriliyor: {dest.name}")
        with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(dest.with_suffix(".part"), "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        dest.with_suffix(".part").replace(dest)
    if runtime.find_llama_server(settings) is None:
        runtime.download_llama_cpp(log)
    return True


def write_summary(result: dict, index: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        f"## Yerel Medya Radarı — {result['day']}",
        "",
        f"- Kaynak: **{result['sources_ok']} / {result['sources']}** yanıt verdi",
        f"- Okunan başlık: **{result['scanned']}** · yeni: **{result['new']}**",
        "",
        "| Konu | Bugün | Son 7 gün | Toplam |",
        "|---|---:|---:|---:|",
    ]
    for card in index.get("topics", []):
        lines.append(f"| {card['title']} | {result['matches'].get(card['slug'], 0)} | {card['last7']} | {card['count']} |")
    failed = result.get("failed_sources", [])
    if failed:
        lines += ["", f"<details><summary>Yanıt vermeyen {len(failed)} kaynak</summary>", "", ", ".join(failed), "", "</details>"]
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        log("\n" + text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site-only", action="store_true")
    ap.add_argument("--pull", type=int, default=0, metavar="GÜN")
    ap.add_argument("--pull-all", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--rematch", action="store_true", help="tüm konuları diskteki bütün arşiv günleriyle baştan eşleştir")
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--date", help="taramanın yazılacağı gün (YYYY-AA-GG); varsayılan bugün (TSİ)")
    args = ap.parse_args()

    cfg = load_config()
    day = date.fromisoformat(args.date) if args.date else now_tr().date()

    if args.pull_all:
        known = sorted(archive.load_stats())
        start = date.fromisoformat(known[0]) if known else day
        archive.pull_days([start + timedelta(days=i) for i in range((day - start).days + 1)])
    elif args.pull:
        archive.pull_days([day - timedelta(days=k) for k in range(args.pull + 1)])

    if args.rematch:
        from radar.topics import load_topics
        for topic in load_topics():
            meta = archive.match_meta(topic.slug)
            meta["signature"] = ""          # imza boşalınca hat tüm günleri yeniden eşleştirir
            archive.save_match_meta(topic.slug, meta)

    if args.site_only:
        if args.rematch:                    # tarama yapmadan, eldeki arşivle yeniden eşleştir
            from radar.topics import load_topics
            for topic in load_topics(active_only=True):
                pipeline.update_topic_matches(topic, day, log)
        index = pipeline.build_site(log)
        log(f"Pano verisi üretildi: {len(index['topics'])} konu")
        return

    semantic = cfg["semantic"]["enabled"] and not args.no_semantic
    if semantic:
        try:
            ensure_semantic_runtime(cfg["semantic"])
        except Exception as exc:  # noqa: BLE001
            log(f"! Anlamsal katman hazırlanamadı ({type(exc).__name__}: {exc}); kalıp eşleştirmeyle devam.")
            semantic = False

    result = asyncio.run(pipeline.run_daily(day, limit=args.limit, semantic_enabled=semantic, log=log))

    if args.push:
        ok = archive.push_day(day)
        log("Arşiv Release'e yüklendi." if ok else "! Arşiv yüklenemedi (bir sonraki çalıştırmada yeniden denenir).")

    index = pipeline.build_site(log)
    write_summary(result, index)

    try:
        from radar.llm.runtime import runtime
        runtime.stop()
    except Exception:  # noqa: BLE001
        pass
    if result["sources_ok"] == 0:
        sys.exit("Hiçbir kaynağa ulaşılamadı.")


if __name__ == "__main__":
    main()
