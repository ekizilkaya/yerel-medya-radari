"""Yerel başlık arşivi (SQLite, data/local/radar.db).

Yerel taramalar, derin aramalar ve GitHub'daki günlük arşiv dosyaları burada birleşir; böylece
bilgisayar kapalıyken bulutta toplanan veriler de yerel analizlerde kullanılabilir.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import archive
from .paths import DB_FILE, TR_TZ, today_tr
from .scraper import Article
from .topics import Topic

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY, day TEXT NOT NULL, title TEXT NOT NULL, url TEXT, source TEXT, city TEXT,
    published TEXT, summary TEXT, origin TEXT, baseline INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_day ON articles(day);
CREATE TABLE IF NOT EXISTS embeddings (id TEXT, model TEXT, vec BLOB, PRIMARY KEY (id, model));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS match_cache (sig TEXT, id TEXT, k TEXT, w TEXT, PRIMARY KEY (sig, id));
CREATE TABLE IF NOT EXISTS match_progress (sig TEXT PRIMARY KEY, last_rowid INTEGER);
"""


def _published_day(published: str) -> date | None:
    try:
        return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TR_TZ).date()
    except (ValueError, TypeError):
        return None


class Store:
    def __init__(self, path=DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self.version = 0                 # içerik değiştikçe artar (rapor önbelleği için)

    # -- yazma --------------------------------------------------------------
    def add_articles(self, articles: list[Article], *, origin: str = "tarama", day: date | None = None,
                     max_age_days: int = 3) -> dict:
        """Yeni makaleleri ekler. Dönüş: {'new': n, 'baseline': n, 'stale': n}"""
        day = day or today_tr()
        with self._lock:
            known_sources = {r[0] for r in self._db.execute("SELECT DISTINCT source FROM articles")}
            first_run = not known_sources
            rows, stats = [], {"new": 0, "baseline": 0, "stale": 0}
            for a in articles:
                pub = _published_day(a.published)
                if origin == "tarama" and pub and (day - pub).days > max_age_days:
                    stats["stale"] += 1
                    continue
                # Tarihsiz kaynak ilk kez görülüyorsa sayfadaki her şey "yeni" görünür; sayıma katma
                baseline = int(origin == "tarama" and not a.published and (first_run or a.source not in known_sources))
                if origin == "arama" and not pub:
                    baseline = 1            # tarihi bilinmeyen arama sonucu zaman serisini bozmasın
                use_day = pub if (origin != "tarama" and pub) else day
                rows.append((a.id, use_day.isoformat(), a.title, a.url, a.source, a.city, a.published, a.summary, origin, baseline))
            before = self._db.total_changes
            self._db.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
            stats["new"] = self._db.total_changes - before
            stats["baseline"] = sum(r[9] for r in rows)
            self.version += 1
            return stats

    def import_archive(self) -> int:
        """data/archive altındaki (GitHub'dan indirilmiş) gün dosyalarını içeri alır."""
        with self._lock:
            done = set((self.get("imported_days") or "").split(","))
            added = 0
            for d in archive.available_days():
                key = d.isoformat()
                if key in done and d != today_tr():
                    continue
                rows = [(r["i"], key, r["t"], r.get("u", ""), r.get("s", ""), r.get("c", ""), r.get("p", ""),
                         r.get("x", ""), "arsiv", int(bool(r.get("b")))) for r in archive.read_day(d)]
                before = self._db.total_changes
                self._db.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                added += self._db.total_changes - before
                done.add(key)
            self.set("imported_days", ",".join(sorted(x for x in done if x)))
            self._db.commit()
            if added:
                self.version += 1
            return added

    # -- okuma --------------------------------------------------------------
    def rows(self, since: str | None = None, *, include_baseline: bool = False) -> list[tuple]:
        """(id, day, title, url, source, city, published, summary)"""
        q = "SELECT id, day, title, url, source, city, published, summary FROM articles WHERE 1=1"
        args: list = []
        # Derin arama sonuçları (origin='arama') eğilim hesaplarına girmez: düzenli tarama yapılmamış
        # günlere tek tük kayıt ekleyip zaman serisini ve paydayı bozarlar. Kendi sonuç listelerinde gösterilirler.
        q += " AND origin != 'arama'"
        if not include_baseline:
            q += " AND baseline = 0"
        if since:
            q += " AND day >= ?"
            args.append(since)
        with self._lock:
            return self._db.execute(q + " ORDER BY day, id", args).fetchall()

    def recent(self, days: int = 120, limit: int = 250_000) -> list[tuple[str, str]]:
        """En güncelden eskiye (kimlik, başlık) — genişletme hattının derlemi."""
        since = (today_tr() - timedelta(days=days)).isoformat()
        with self._lock:
            return self._db.execute(
                "SELECT id, title FROM articles WHERE day >= ? ORDER BY day DESC, id LIMIT ?", (since, limit)).fetchall()

    def stats(self) -> dict[str, dict]:
        """archive.load_stats() ile aynı biçim: {gün: {n, c: {il: n}}}"""
        out: dict[str, dict] = {}
        with self._lock:
            for day, city, n in self._db.execute(
                    "SELECT day, city, COUNT(*) FROM articles WHERE baseline = 0 AND origin != 'arama' GROUP BY day, city"):
                e = out.setdefault(day, {"n": 0, "c": {}})
                e["n"] += n
                for c in (city or "").split(" / "):
                    if c:
                        e["c"][c] = e["c"].get(c, 0) + n
        return out

    def summary(self) -> dict:
        with self._lock:
            n, srcs = self._db.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM articles").fetchone()
            first, last = self._db.execute("SELECT MIN(day), MAX(day) FROM articles WHERE origin != 'arama'").fetchone()
        return {"articles": n, "first_day": first, "last_day": last, "sources": srcs}

    def match_topic(self, topic: Topic, since: str | None = None) -> list[dict]:
        """Arşivi konunun kalıplarıyla tarar; rapor için eşleşme kayıtları üretir.

        Artımlıdır: her konu tanımı (imza) için yalnızca son bakıştan beri eklenen başlıklar taranır,
        sonuçlar veritabanında saklanır. Arşiv yüz binlerce başlığa ulaştığında da pano hızlı kalır;
        tam tarama yalnızca bir konunun terimleri değiştiğinde bir kez yapılır."""
        m = topic.matcher()
        sig = topic.signature()
        with self._lock:
            row = self._db.execute("SELECT last_rowid FROM match_progress WHERE sig = ?", (sig,)).fetchone()
            last = row[0] if row else 0
            hits, newest = [], last
            for rowid, id_, title, summary in self._db.execute(
                    "SELECT rowid, id, title, summary FROM articles "
                    "WHERE rowid > ? AND baseline = 0 AND origin != 'arama' ORDER BY rowid", (last,)):
                newest = rowid
                res = m.match(title, summary or "")
                if res:
                    hits.append((sig, id_, json.dumps(res[0], ensure_ascii=False), res[1]))
            if newest != last or row is None:
                self._db.executemany("INSERT OR REPLACE INTO match_cache VALUES (?,?,?,?)", hits)
                self._db.execute("INSERT OR REPLACE INTO match_progress VALUES (?, ?)", (sig, newest))
                self._db.commit()
            q = ("SELECT a.id, a.day, a.title, a.url, a.source, a.city, a.published, a.summary, c.k, c.w "
                 "FROM match_cache c JOIN articles a ON a.id = c.id WHERE c.sig = ?")
            args: list = [sig]
            if since:
                q += " AND a.day >= ?"
                args.append(since)
            found = self._db.execute(q + " ORDER BY a.day, a.id", args).fetchall()
        out = []
        for id_, day, title, url, source, city, published, summary, k, w in found:
            rec = {"d": day, "i": id_, "t": title, "u": url, "s": source, "c": city, "k": json.loads(k), "w": w}
            if published:
                rec["p"] = published
            if w == "s" and summary:
                rec["x"] = summary
            out.append(rec)
        return out

    def prune_match_cache(self, keep: set[str]) -> None:
        """Artık hiçbir kayıtlı konuya ait olmayan (eski tanım ya da önizleme) eşleşme önbelleklerini siler."""
        with self._lock:
            sigs = [r[0] for r in self._db.execute("SELECT sig FROM match_progress")]
            for sig in sigs:
                if sig not in keep:
                    self._db.execute("DELETE FROM match_cache WHERE sig = ?", (sig,))
                    self._db.execute("DELETE FROM match_progress WHERE sig = ?", (sig,))
            self._db.commit()

    # -- gömme önbelleği ----------------------------------------------------
    def get_vectors(self, ids: list[str], model: str) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self._lock:
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                q = f"SELECT id, vec FROM embeddings WHERE model = ? AND id IN ({','.join('?' * len(chunk))})"
                for id_, blob in self._db.execute(q, [model, *chunk]):
                    out[id_] = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
        return out

    def put_vectors(self, ids: list[str], vecs: np.ndarray, model: str) -> None:
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)",
                                 [(i, model, v.astype(np.float16).tobytes()) for i, v in zip(ids, vecs)])
            self._db.commit()

    # -- anahtar/değer ------------------------------------------------------
    def get(self, key: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
            self._db.commit()
