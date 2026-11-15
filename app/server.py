"""Yerel uygulama sunucusu (FastAPI).

Aynı pano (site/) hem GitHub Pages'te statik olarak hem de burada çalışır. Burada ek olarak:
anlık tarama, anahtar kelime genişletme (yerel dil modeli), önizleme, derin arama, gündem özeti,
model seçimi ve GitHub'a yayınlama uçları bulunur.

  python app/server.py        →  http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from radar import __version__, archive, digest, expand, report, semantic  # noqa: E402
from radar.llm import models as llm_models  # noqa: E402
from radar.llm.runtime import RuntimeError_, download_llama_cpp, runtime  # noqa: E402
from radar.paths import LOCAL_DIR, ROOT, SITE, load_config, now_tr, read_json, today_tr, write_json  # noqa: E402
from radar.scraper import make_client, scan_sources, site_search  # noqa: E402
from radar.sources import coverage, load_sources  # noqa: E402
from radar.store import Store  # noqa: E402
from radar.topics import Topic, delete_topic, load_topic, load_topics, save_topic, slugify  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s")
logger = logging.getLogger("radar")

app = FastAPI(title="Yerel Medya Radarı", version=__version__)
store = Store()


class State:
    job: str = ""                              # o an süren uzun işlem (aynı anda yalnız bir tane)
    sem: dict[str, list[dict]] = {}            # konu → yerelde bulunan anlamsal adaylar
    reports: dict[tuple, dict] = {}            # (konu imzası, arşiv sürümü, gün) → rapor önbelleği
    last_digest: dict = {}


state = State()


# ---------------------------------------------------------------------------
# Uzun işlemler: Server-Sent Events ile ilerleme bildirimi
# ---------------------------------------------------------------------------

def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def run_job(name: str, work) -> StreamingResponse:
    """`work(emit)` eşyordamını arka planda çalıştırır; olayları istemciye akıtır.
    İstemci bağlantıyı kesse de iş tamamlanır (sonuçlar veritabanına yazılır)."""
    if state.job:
        raise HTTPException(409, f"Şu an başka bir işlem sürüyor: {state.job}")
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: str, data) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    async def runner() -> None:
        state.job = name
        try:
            result = await work(emit)
            emit("done", result if result is not None else {})
        except (RuntimeError_, expand.client.LLMError) as exc:
            emit("error", {"message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("iş başarısız: %s", name)
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            state.job = ""
            emit("_end", None)

    asyncio.create_task(runner())

    async def stream():
        while True:
            event, data = await queue.get()
            if event == "_end":
                break
            yield _sse(event, data)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Pano verisi (site/data/* ile aynı biçim; burada yerel arşivden anlık üretilir)
# ---------------------------------------------------------------------------

def _first_day() -> str:
    return store.summary()["first_day"] or today_tr().isoformat()


def local_report(topic: Topic, *, cache: bool = True) -> dict:
    key = (topic.slug, topic.signature(), store.version, today_tr().isoformat(), len(state.sem.get(topic.slug, [])))
    if cache and key in state.reports:
        return state.reports[key]
    matches = store.match_topic(topic)
    ids = {m["i"] for m in matches}
    matches += [m for m in state.sem.get(topic.slug, []) if m["i"] not in ids]
    rep = report.build_topic_report(topic, matches, start=_first_day(), end=today_tr().isoformat(),
                                    max_items=load_config()["site"]["items_per_topic"])
    rep["active"] = topic.active
    if cache:
        state.reports = {k: v for k, v in state.reports.items() if k[0] != topic.slug}
        state.reports[key] = rep
    return rep


def _colored(topics: list[Topic]) -> list[Topic]:
    for n, t in enumerate(topics):
        t.color = t.color or report.PALETTE[n % len(report.PALETTE)]
    return topics


@app.get("/data/index.json")
def data_index():
    cfg = load_config()
    sources = load_sources()
    usable = [s for s in sources if s.usable]
    stats = store.stats()
    last_day = max(stats) if stats else today_tr().isoformat()
    cards = []
    for t in _colored(load_topics()):
        card = report.topic_card(local_report(t))
        card["active"] = t.active
        cards.append(card)
    return {
        "generated": now_tr().isoformat(timespec="minutes"), "site": cfg["site"], "mode": "local",
        "topics": cards,
        "last_scan": {"day": last_day, "n": stats.get(last_day, {}).get("n", 0),
                      "src_ok": int(store.get("last_src_ok") or 0), "src_fail": int(store.get("last_src_fail") or 0)},
        "sources": {"total": len(sources), "usable": len(usable), "cities": len({s.city for s in usable})},
        "first_day": _first_day(),
    }


@app.get("/data/coverage.json")
def data_coverage():
    return report.build_coverage(store.stats(), coverage(load_sources()))


@app.get("/data/topics/{slug}.json")
def data_topic(slug: str):
    topics = {t.slug: t for t in _colored(load_topics())}
    if slug not in topics:
        raise HTTPException(404, "Konu bulunamadı")
    return local_report(topics[slug])


@app.get("/data/feeds/{slug}.xml")
def data_feed(slug: str):
    t = load_topic(slug)
    if not t:
        raise HTTPException(404, "Konu bulunamadı")
    return Response(report.build_rss(local_report(t)), media_type="application/rss+xml")


# ---------------------------------------------------------------------------
# Durum, modeller, ayarlar
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    return {"ok": True, "version": __version__, "job": state.job, "store": store.summary(),
            "repo": archive.repo_slug(), "runtime": runtime.status()}


@app.get("/api/models")
def api_models():
    settings = llm_models.load_settings()
    return {"models": llm_models.list_models(settings), "settings": settings,
            "model_dirs": [str(d) for d in llm_models.model_dirs(settings)], "runtime": runtime.status()}


@app.post("/api/settings")
async def api_settings(request: Request):
    patch = await request.json()
    before = llm_models.load_settings()
    after = llm_models.save_settings(patch)
    restart = {"chat": ("chat_model", "ctx", "parallel", "gpu_layers", "reasoning", "backend", "llama_server"),
               "embed": ("embed_model", "gpu_layers", "backend", "llama_server")}
    for role, keys in restart.items():
        if any(before.get(k) != after.get(k) for k in keys):
            runtime.stop(role)
    return {"settings": after, "runtime": runtime.status()}


@app.post("/api/runtime/stop")
def api_runtime_stop():
    runtime.stop()
    return runtime.status()


@app.post("/api/runtime/download")
async def api_runtime_download():
    async def work(emit):
        exe = await asyncio.to_thread(download_llama_cpp, lambda msg: emit("log", {"stage": "indir", "detail": msg}))
        return {"exe": str(exe)}
    return run_job("llama.cpp indiriliyor", work)


# ---------------------------------------------------------------------------
# Tarama ve arşiv eşitleme
# ---------------------------------------------------------------------------

@app.post("/api/scan")
async def api_scan():
    async def work(emit):
        cfg = load_config()["scan"]
        sources = load_sources(usable_only=True)
        emit("log", {"stage": "tarama", "detail": f"{len(sources)} kaynak taranıyor…"})

        def progress(done, total, src, n, status):
            if done % 20 == 0 or done == total:
                emit("progress", {"done": done, "total": total, "city": src.city})

        articles, health = await scan_sources(sources, concurrency=cfg["concurrency"], timeout=cfg["timeout"],
                                              max_items=cfg["max_items_per_source"], on_progress=progress)
        ok = sum(1 for h in health.values() if h["status"] == "ok")
        stats = await asyncio.to_thread(store.add_articles, articles, origin="tarama", max_age_days=cfg["max_age_days"])
        store.set("last_src_ok", str(ok))
        store.set("last_src_fail", str(len(sources) - ok))
        return {"scanned": len(articles), "sources": len(sources), "sources_ok": ok, **stats}
    return run_job("Kaynaklar taranıyor", work)


@app.post("/api/sync")
async def api_sync(request: Request):
    body = await request.json() if await request.body() else {}
    days = int(body.get("days", 60))

    async def work(emit):
        if not archive.repo_slug():
            raise RuntimeError_("Bu klasör henüz bir GitHub deposuna bağlı değil (git remote yok).")
        today = today_tr()
        emit("log", {"stage": "eşitleme", "detail": f"Son {days} günün arşivi GitHub'dan indiriliyor…"})
        got = await asyncio.to_thread(archive.pull_days, [today - timedelta(days=k) for k in range(days + 1)], quiet=True)
        added = await asyncio.to_thread(store.import_archive)
        return {"days": got, "articles": added}
    return run_job("GitHub arşivi eşitleniyor", work)


# ---------------------------------------------------------------------------
# Konular: genişletme, önizleme, kaydetme
# ---------------------------------------------------------------------------

@app.get("/api/topics")
def api_topics():
    return [t.to_dict() for t in load_topics()]


@app.post("/api/expand")
async def api_expand(request: Request):
    body = await request.json()
    keyword = str(body.get("keyword", "")).strip()
    if len(keyword) < 2:
        raise HTTPException(400, "Anahtar kelime gerekli")

    async def work(emit):
        rows = store.recent()
        ids, titles = [r[0] for r in rows], [r[1] for r in rows]

        def embed_titles(keys, texts):
            return semantic.embed_cached(store, [ids[k] for k in keys], texts)

        result = await expand.expand_keyword(
            keyword, note=str(body.get("note", "")), corpus_titles=titles,
            depth=2 if body.get("deep") else 1,
            use_embeddings=body.get("embeddings", True), audit=body.get("audit", True),
            embed_titles=embed_titles,
            progress=lambda stage, detail: emit("log", {"stage": stage, "detail": detail}))
        return result
    return run_job(f"“{keyword}” genişletiliyor", work)


@app.post("/api/preview")
async def api_preview(request: Request):
    body = await request.json()
    topic = Topic.from_dict(body["topic"])
    topic.color = topic.color or report.PALETTE[0]
    return await asyncio.to_thread(local_report, topic, cache=False)


@app.put("/api/topics/{slug}")
async def api_save_topic(slug: str, request: Request):
    body = await request.json()
    topic = Topic.from_dict(body)
    topic.slug = slugify(slug)
    existing = load_topic(topic.slug)
    if existing:
        topic.created = existing.created
    save_topic(topic)
    return {"ok": True, "slug": topic.slug}


@app.delete("/api/topics/{slug}")
def api_delete_topic(slug: str):
    if not delete_topic(slug):
        raise HTTPException(404, "Konu bulunamadı")
    state.sem.pop(slug, None)
    return {"ok": True}


@app.post("/api/semantic/{slug}")
async def api_semantic(slug: str):
    topic = load_topic(slug)
    if not topic:
        raise HTTPException(404, "Konu bulunamadı")

    async def work(emit):
        import numpy as np

        rows = store.rows((today_tr() - timedelta(days=45)).isoformat())[-25000:]
        if len(rows) < 100:
            raise RuntimeError_("Yerel arşivde yeterli başlık yok; önce bir tarama yapın.")
        emit("log", {"stage": "gomme", "detail": f"{len(rows)} başlık vektörleniyor (önbellekte olmayanlar hesaplanır)…"})
        ids, titles = [r[0] for r in rows], [r[2] for r in rows]
        tv = await semantic.embed_cached(
            store, ids, titles,
            on_progress=lambda d, t: emit("progress", {"done": d, "total": t}) if d % 960 == 0 or d == t else None)
        m = topic.matcher()
        hit_title = np.array([bool(m.match_text(t)) for t in titles])
        matched = np.array([bool(m.match(r[2], r[7] or "")) for r in rows])
        probe = semantic.reduce(await expand.client.embed(semantic.probe_texts(topic)))
        scores = semantic.score_titles(tv, probe, tv[hit_title], matched)
        idx = semantic.pick_candidates(scores, matched, z=load_config()["semantic"]["z"], max_n=80)
        state.sem[slug] = [{"d": rows[i][1], "i": rows[i][0], "t": rows[i][2], "u": rows[i][3], "s": rows[i][4],
                            "c": rows[i][5], "k": [], "m": "sem", "sc": round(float(scores[i]), 1)} for i in idx]
        return {"candidates": len(idx)}
    return run_job("Anlamsal adaylar aranıyor", work)


# ---------------------------------------------------------------------------
# Derin arama: sitelerin kendi aramalarıyla geçmişe dönük tarama
# ---------------------------------------------------------------------------

@app.post("/api/deepsearch")
async def api_deepsearch(request: Request):
    body = await request.json()
    topic = Topic.from_dict(body["topic"])
    cities = set(body.get("cities") or [])
    max_terms = int(body.get("max_terms", 2))

    async def work(emit):
        matcher = topic.matcher()
        queries = [t.t.strip('"*') for t in topic.terms
                   if t.on and t.kind == "core" and not t.t.startswith("re:")][:max_terms]
        sources = [s for s in load_sources(usable_only=True) if not cities or s.city in cities]
        cache_file = LOCAL_DIR / "search_templates.json"
        templates = read_json(cache_file, {}) or {}
        found: dict[str, dict] = {}
        sem = asyncio.Semaphore(12)
        done = 0
        emit("log", {"stage": "arama", "detail": f"{len(sources)} sitede aranıyor: {', '.join(queries)}"})

        def relevant(a) -> bool:
            return bool(matcher.match(a.title, a.summary))

        async with make_client(20, 40) as client:
            async def one(src):
                nonlocal done
                # Önbellek: çalışan kalıbın adı, ya da "miss:N" (N kez sonuçsuz kaldı; 3'ten sonra site atlanır)
                known = templates.get(src.domain, "")
                misses = int(known.split(":")[1]) if known.startswith("miss:") else 0
                tpl_name = "" if known.startswith("miss:") else known
                if misses < 3:
                    async with sem:
                        for q in queries:
                            try:
                                arts, tpl = await asyncio.wait_for(site_search(client, src, q, tpl_name, accept=relevant), 90)
                            except Exception:  # noqa: BLE001
                                arts, tpl = [], ""
                            if arts:
                                templates[src.domain] = tpl_name = tpl
                            for a in arts:
                                found.setdefault(a.id, {"a": a})
                    if not tpl_name:
                        templates[src.domain] = f"miss:{misses + 1}"
                done += 1
                if done % 10 == 0 or done == len(sources):
                    emit("progress", {"done": done, "total": len(sources), "found": len(found)})

            await asyncio.gather(*(one(s) for s in sources))

        write_json(cache_file, templates)
        articles = [f["a"] for f in found.values()]
        await asyncio.to_thread(store.add_articles, articles, origin="arama")
        items = []
        for a in articles:
            res = matcher.match(a.title, a.summary)
            items.append({"t": a.title, "u": a.url, "s": a.source, "c": a.city, "p": a.published[:10],
                          "k": res[0] if res else []})
        items.sort(key=lambda x: (x["p"] or "0", x["c"]), reverse=True)
        return {"items": items, "sources": len(sources),
                "searchable": sum(1 for s in sources if templates.get(s.domain, "miss:").split(":")[0] != "miss")}
    return run_job("Derin arama", work)


# ---------------------------------------------------------------------------
# Gündem özeti (önemli yerel haberler)
# ---------------------------------------------------------------------------

@app.post("/api/digest")
async def api_digest(request: Request):
    body = await request.json() if await request.body() else {}
    hours = int(body.get("hours", 24))

    async def work(emit):
        since = (today_tr() - timedelta(days=max(0, (hours - 1) // 24))).isoformat()
        rows = [{"i": r[0], "t": r[2], "u": r[3], "s": r[4], "c": r[5], "x": r[7] or ""} for r in store.rows(since)]
        if not rows:
            raise RuntimeError_("Seçilen aralıkta başlık yok; önce bir tarama yapın.")

        def progress(stage, detail):
            emit("news" if stage == "haber" else "log", detail if stage == "haber" else {"stage": stage, "detail": detail})

        news = await digest.run_digest(rows, progress=progress)
        state.last_digest = {"generated": now_tr().isoformat(timespec="minutes"), "items": news}
        write_json(LOCAL_DIR / "digest-latest.json", state.last_digest)
        return {"total": len(news), "headlines": len(rows)}
    return run_job("Gündem özeti hazırlanıyor", work)


@app.get("/api/digest")
def api_digest_latest():
    return state.last_digest or read_json(LOCAL_DIR / "digest-latest.json", {}) or {}


# ---------------------------------------------------------------------------
# Kaynaklar ve GitHub'a yayınlama
# ---------------------------------------------------------------------------

@app.get("/api/sources")
def api_sources():
    sources = load_sources(scope=None)
    return {"sources": [{"domain": s.domain, "city": s.city, "method": s.method, "status": s.status,
                         "scope": s.scope, "origin": s.origin, "bik": s.bik} for s in sources]}


def _git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    return p.returncode, (p.stdout + p.stderr).strip()


@app.post("/api/publish")
def api_publish():
    """Konu tanımlarını ve ayarları depoya işler ve GitHub'a gönderir (kullanıcı düğmeye bastığında)."""
    if not archive.repo_slug():
        raise HTTPException(400, "Bu klasör bir GitHub deposuna bağlı değil. README'deki kurulum adımlarına bakın.")
    log = []
    paths = [p for p in ("data/topics", "data/config.json", "data/sources.json", "data/extra_sources.csv") if (ROOT / p).exists()]
    _git("add", "-A", "--", *paths)          # -A: silinen konu dosyaları da işlensin
    code, out = _git("diff", "--cached", "--quiet")
    if code == 0:
        return {"ok": True, "message": "Gönderilecek değişiklik yok."}
    for args in (("commit", "-m", "Konu tanımları güncellendi"), ("pull", "--rebase", "--autostash"), ("push",)):
        code, out = _git(*args)
        log.append(f"$ git {' '.join(args)}\n{out}")
        if code != 0:
            return JSONResponse({"ok": False, "message": "\n\n".join(log)}, status_code=500)
    return {"ok": True, "message": "Konular GitHub'a gönderildi; pano birkaç dakika içinde güncellenir.", "log": "\n\n".join(log)}


# ---------------------------------------------------------------------------
# Statik pano
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(SITE / "index.html", headers={"Cache-Control": "no-cache"})


class _FreshStatic(StaticFiles):
    """Tarayıcı her seferinde dosyanın değişip değişmediğini sorsun (güncelleme sonrası eski betik kalmasın)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/assets", _FreshStatic(directory=str(SITE / "assets")), name="assets")


@app.on_event("startup")
async def _startup() -> None:
    added = await asyncio.to_thread(store.import_archive)
    store.prune_match_cache({t.signature() for t in load_topics()})
    logger.info("Yerel arşiv: %s (+%d içe aktarıldı)", store.summary(), added)


@app.on_event("shutdown")
def _shutdown() -> None:
    runtime.stop()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
