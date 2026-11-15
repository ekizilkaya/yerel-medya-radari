"""Günlük tarama hattı (dosya tabanlı — GitHub Actions'ta ve istenirse yerelde çalışır).

  kaynakları tara → yeni başlıkları ayır → günlük arşive yaz → her konu için eşleştir
  → (isteğe bağlı) anlamsal adaylar → pano verisini (site/data) üret
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import archive, report
from .paths import SITE_DATA, TR_TZ, load_config, now_tr, write_json
from .scraper import Article, scan_sources
from .sources import coverage, load_sources
from .topics import Topic, load_topics


def _age_days(published: str, day: date) -> int | None:
    try:
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (day - dt.astimezone(TR_TZ).date()).days


def classify_new(articles: list[Article], day: date, seen_ids: set[str], known_sources: set[str],
                 first_run: bool, max_age_days: int) -> list[dict]:
    """Daha önce görülmemiş makaleleri arşiv kaydına çevirir.

    `b: 1` (taban) işaretli kayıtlar arşive girer ama sayıma ve eşleştirmeye girmez: yayın tarihi
    vermeyen bir kaynak ilk kez tarandığında sayfasındaki eski haberlerin "bugün" sayılmasını önler.
    """
    out = []
    for a in articles:
        if a.id in seen_ids:
            continue
        age = _age_days(a.published, day)
        if age is not None and age > max_age_days:
            continue
        rec = a.record()
        if age is None and (first_run or a.source not in known_sources):
            rec["b"] = 1
        out.append(rec)
        seen_ids.add(a.id)
    return out


def match_records(topic: Topic, records: list[dict], day: str) -> list[dict]:
    m = topic.matcher()
    out = []
    for r in records:
        if r.get("b"):
            continue
        res = m.match(r["t"], r.get("x", ""))
        if res:
            item = {"d": day, "i": r["i"], "t": r["t"], "u": r.get("u", ""), "s": r.get("s", ""),
                    "c": r.get("c", ""), "k": res[0], "w": res[1]}
            if r.get("p"):
                item["p"] = r["p"]
            if res[1] == "s" and r.get("x"):
                item["x"] = r["x"]
            out.append(item)
    return out


def update_topic_matches(topic: Topic, day: date, log=print) -> int:
    """Konunun eşleşmelerini günceller. Terimler değiştiyse diskteki tüm arşiv günleri yeniden eşleştirilir."""
    meta = archive.match_meta(topic.slug)
    sig = topic.signature()
    days = [day]
    if meta.get("signature") != sig:
        days = sorted(set(archive.available_days()) | {day})
        log(f"  {topic.slug}: terimler değişmiş/yeni konu → {len(days)} gün yeniden eşleştiriliyor")
    items: list[dict] = []
    for d in days:
        items += match_records(topic, archive.read_day(d), d.isoformat())
    report.assign_groups(items)
    added = archive.replace_matches(topic.slug, {d.isoformat() for d in days}, items, kind="re")
    meta.update(signature=sig, updated=now_tr().isoformat(timespec="minutes"))
    meta["since"] = min(meta.get("since") or days[0].isoformat(), days[0].isoformat())
    archive.save_match_meta(topic.slug, meta)
    return added


async def semantic_pass(topics: list[Topic], day: date, cfg: dict, log=print) -> None:
    """Günün başlıklarını gömme modeliyle vektörler; her konu için kalıba takılmayan adayları kaydeder."""
    import numpy as np

    from . import semantic
    from .llm import client

    records = [r for r in archive.read_day(day) if not r.get("b")][-int(cfg.get("max_titles", 20000)):]
    topics = [t for t in topics if t.semantic]
    if len(records) < 50 or not topics:
        return
    log(f"Anlamsal tarama: {len(records)} başlık vektörleniyor…")
    last = {"n": 0}

    def prog(done: int, total: int) -> None:
        if done - last["n"] >= 2000 or done == total:
            last["n"] = done
            log(f"  {done}/{total}")

    title_vecs = semantic.reduce(await client.embed([r["t"] for r in records], on_progress=prog))
    key = day.isoformat()
    for topic in topics:
        existing = archive.load_matches(topic.slug, "re")
        regex_ids = {m["i"] for m in existing}
        # Bilinen eşleşmeler: yalnızca başlığında terim geçenler (özetten eşleşenler konuya daha uzak olabilir)
        recent_pos = [m["t"] for m in existing if m.get("w", "t") == "t"][-200:]
        probe = semantic.reduce(await client.embed(semantic.probe_texts(topic)))
        pos_vecs = semantic.reduce(await client.embed(recent_pos)) if len(recent_pos) >= 20 else None
        matched = np.array([r["i"] in regex_ids for r in records])
        scores = semantic.score_titles(title_vecs, probe, pos_vecs, matched)
        idx = semantic.pick_candidates(scores, matched, z=cfg["z"], max_n=cfg["max_per_day"])
        items = []
        for i in idx:
            r = records[i]
            items.append({"d": key, "i": r["i"], "t": r["t"], "u": r.get("u", ""), "s": r.get("s", ""),
                          "c": r.get("c", ""), "k": [], "m": "sem", "sc": round(float(scores[i]), 1)})
        archive.replace_matches(topic.slug, {key}, items, kind="sem")
        log(f"  {topic.slug}: {len(items)} anlamsal aday (eşik {cfg['z']}σ)")


async def run_daily(day: date | None = None, *, limit: int = 0, semantic_enabled: bool | None = None, log=print) -> dict:
    cfg = load_config()
    scan_cfg = cfg["scan"]
    day = day or now_tr().date()
    sources = load_sources(usable_only=True)
    if limit:
        sources = sources[::max(1, len(sources) // limit)][:limit]
    log(f"{day} · {len(sources)} kaynak taranıyor…")

    def progress(done, total, src, n, status):
        if done % 100 == 0 or done == total:
            log(f"  [{done}/{total}] kaynak tarandı")

    articles, health = await scan_sources(
        sources, concurrency=scan_cfg["concurrency"], timeout=scan_cfg["timeout"],
        max_items=scan_cfg["max_items_per_source"], on_progress=progress)
    ok = sum(1 for h in health.values() if h["status"] == "ok")
    log(f"Tarama bitti: {len(articles)} başlık · {ok}/{len(sources)} kaynak yanıt verdi")

    seen_ids, known_sources, any_day = archive.seen_state(day, scan_cfg["seen_window_days"])
    today_existing = archive.read_day(day)
    seen_ids |= {r["i"] for r in today_existing}
    known_sources |= {r.get("s", "") for r in today_existing if not r.get("b")}
    fresh = classify_new(articles, day, seen_ids, known_sources, not any_day and not today_existing,
                         scan_cfg["max_age_days"])
    records = today_existing + fresh
    archive.write_day(day, records)

    counted = [r for r in records if not r.get("b")]
    by_city: Counter = Counter()
    for r in counted:
        by_city[r.get("c", "")] += 1
    failed = sorted(d for d, h in health.items() if h["status"] != "ok")
    archive.save_stats_day(day.isoformat(), {
        "n": len(counted), "c": dict(sorted(by_city.items())), "src_ok": ok, "src_fail": len(sources) - ok,
        "scanned": len(articles), "baseline": len(records) - len(counted),
    })
    log(f"Yeni başlık: {len(fresh)} (bugün toplam {len(counted)} sayılan, {len(records) - len(counted)} taban)")

    topics = load_topics(active_only=True)
    matched = {}
    for t in topics:
        update_topic_matches(t, day, log)
        matched[t.slug] = sum(1 for m in archive.load_matches(t.slug, "re") if m["d"] == day.isoformat())
        log(f"  {t.slug}: bugün {matched[t.slug]} eşleşme")

    sem_cfg = cfg["semantic"]
    if (semantic_enabled if semantic_enabled is not None else sem_cfg["enabled"]) and topics:
        try:
            await asyncio.wait_for(semantic_pass(topics, day, sem_cfg, log), sem_cfg.get("time_budget_sec", 1200))
        except asyncio.TimeoutError:
            log("  ! Anlamsal tarama süre sınırını aştı; bu çalıştırmada atlandı (kalıp eşleşmeleri etkilenmez).")
        except Exception as exc:  # noqa: BLE001 — anlamsal katman isteğe bağlıdır; taramayı düşürmemeli
            log(f"  ! Anlamsal tarama atlandı: {type(exc).__name__}: {exc}")

    return {"day": day.isoformat(), "sources": len(sources), "sources_ok": ok, "scanned": len(articles),
            "new": len(fresh), "counted": len(counted), "matches": matched, "failed_sources": failed}


def build_site(log=print) -> dict:
    """data/ altındaki eşleşme ve istatistiklerden site/data/*.json dosyalarını üretir."""
    cfg = load_config()
    stats = archive.load_stats()
    sources = load_sources()
    cov = report.build_coverage(stats, coverage(sources))
    write_json(SITE_DATA / "coverage.json", cov, compact=True)

    last_day = cov["days"][-1] if cov["days"] else now_tr().date().isoformat()
    cards = []
    for n, topic in enumerate(load_topics()):
        matches = archive.load_matches(topic.slug, "re")
        regex_ids = {m["i"] for m in matches}
        matches += [m for m in archive.load_matches(topic.slug, "sem") if m["i"] not in regex_ids]
        since = archive.match_meta(topic.slug).get("since") or (matches[0]["d"] if matches else last_day)
        if cov["days"]:
            since = max(min(since, last_day), cov["days"][0])
        topic.color = topic.color or report.PALETTE[n % len(report.PALETTE)]
        rep = report.build_topic_report(topic, matches, start=since, end=last_day,
                                        max_items=cfg["site"]["items_per_topic"])
        rep["active"] = topic.active
        write_json(SITE_DATA / "topics" / f"{topic.slug}.json", rep, compact=True)
        (SITE_DATA / "feeds").mkdir(parents=True, exist_ok=True)
        (SITE_DATA / "feeds" / f"{topic.slug}.xml").write_text(report.build_rss(rep), encoding="utf-8", newline="\n")
        card = report.topic_card(rep)
        card["active"] = topic.active
        cards.append(card)
        log(f"  site: {topic.slug} → {rep['count']} eşleşme, {len(rep['days'])} gün")

    last = stats.get(last_day, {})
    usable = [s for s in sources if s.usable]
    index = {
        "generated": now_tr().isoformat(timespec="minutes"),
        "site": cfg["site"],
        "mode": "static",
        "topics": cards,
        "last_scan": {"day": last_day, **{k: last.get(k, 0) for k in ("n", "src_ok", "src_fail", "scanned")}},
        "sources": {"total": len(sources), "usable": len(usable), "cities": len({s.city for s in usable})},
        "first_day": cov["days"][0] if cov["days"] else last_day,
    }
    write_json(SITE_DATA / "index.json", index, compact=True)
    return index


def recent_days(day: date, n: int) -> list[date]:
    return [day - timedelta(days=k) for k in range(n + 1)]
