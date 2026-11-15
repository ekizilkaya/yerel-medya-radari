"""Proje dizinleri, tarih yardımcıları ve basit JSON ayar okuma/yazma."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
SOURCES_FILE = DATA / "sources.json"
CONFIG_FILE = DATA / "config.json"
TOPICS_DIR = DATA / "topics"
MATCHES_DIR = DATA / "matches"
STATS_DIR = DATA / "stats"
ARCHIVE_DIR = DATA / "archive"      # günlük başlık arşivi (git'e girmez; GitHub Releases'ta saklanır)
LOCAL_DIR = DATA / "local"          # yalnızca bu bilgisayar (git'e girmez)
SETTINGS_FILE = LOCAL_DIR / "settings.json"
DB_FILE = LOCAL_DIR / "radar.db"
RUNTIME_DIR = ROOT / "runtime"      # indirilen llama.cpp (git'e girmez)

SITE = ROOT / "site"
SITE_DATA = SITE / "data"

# Türkiye 2016'dan beri sabit UTC+3 kullanıyor; tzdata bağımlılığına gerek yok.
TR_TZ = timezone(timedelta(hours=3), "TRT")


def now_tr() -> datetime:
    return datetime.now(TR_TZ)


def today_tr() -> date:
    return now_tr().date()


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj, *, indent: int | None = 1, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.write("\n")
    os.replace(tmp, path)


DEFAULT_CONFIG = {
    # Günlük tarama (GitHub Actions ve yerel) için ortak ayarlar
    "scan": {
        "concurrency": 24,          # eşzamanlı kaynak sayısı
        "timeout": 20,              # saniye
        "max_items_per_source": 40,
        "seen_window_days": 21,     # "yeni haber" tespiti için geriye bakılan gün
        "max_age_days": 3,          # yayın tarihi bundan eskiyse "yeni" sayılmaz
    },
    # Bulutta (GitHub Actions, yalnız CPU) anlamsal aday taraması için küçük gömme modeli
    "semantic": {
        "enabled": True,
        "model_url": "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf",
        "z": 3.5,                   # hem tanıma hem bilinen konu haberlerine benzerlik arka plandan kaç σ ayrışmalı
        "max_per_day": 15,          # konu başına günlük en fazla anlamsal aday
        "max_titles": 20000,        # bir çalıştırmada vektörlenecek en fazla başlık (süreyi sınırlar)
        "time_budget_sec": 1200,    # bu süre aşılırsa anlamsal adım o çalıştırmada atlanır
    },
    "site": {
        "title": "Yerel Medya Radarı",
        "items_per_topic": 1200,    # panoda listelenecek son eşleşme sayısı
    },
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = read_json(CONFIG_FILE, {}) or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg
