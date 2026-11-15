"""LM Studio'nun (ve kullanıcının eklediği klasörlerin) indirdiği GGUF modelleri bulur.

LM Studio'nun çalışıyor olması gerekmez; yalnızca diskteki model klasörü okunur:
  ~/.lmstudio-home-pointer → <ev>/settings.json içindeki "downloadsFolder" → <ev>/models
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..paths import LOCAL_DIR, RUNTIME_DIR, SETTINGS_FILE, read_json, write_json
from . import gguf

DEFAULT_SETTINGS = {
    "backend": "managed",            # managed: uygulama llama-server'ı kendi başlatır | external: hazır bir OpenAI uyumlu sunucu
    "external_url": "http://127.0.0.1:1234/v1",
    "chat_model": "",                # model kimliği (model klasörüne göre göreli yol)
    "embed_model": "",
    "ctx": 16384,                    # sohbet modeli toplam bağlam (paralel yuvalara bölünür)
    "parallel": 4,
    "gpu_layers": "auto",            # auto | all | sayı
    "reasoning": "off",              # düşünen modellerde düşünmeyi kapat (hız için) | auto | on
    "idle_stop_min": 10,             # bu kadar dakika kullanılmayan model bellekten atılır
    "llama_server": "",              # elle belirtilen llama-server yolu (boşsa otomatik bulunur)
    "extra_model_dirs": [],
}

_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_CACHE_FILE = LOCAL_DIR / "models_cache.json"


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(read_json(SETTINGS_FILE, {}) or {})
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    s.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
    write_json(SETTINGS_FILE, s)
    return s


def lmstudio_homes() -> list[Path]:
    homes: list[Path] = []
    pointer = Path.home() / ".lmstudio-home-pointer"
    try:
        if pointer.exists():
            homes.append(Path(pointer.read_text(encoding="utf-8").strip()))
    except OSError:
        pass
    homes += [Path.home() / ".lmstudio", Path.home() / ".cache" / "lm-studio"]
    seen, out = set(), []
    for h in homes:
        key = str(h).lower()
        if key not in seen and h.is_dir():
            seen.add(key)
            out.append(h)
    return out


def model_dirs(settings: dict | None = None) -> list[Path]:
    settings = settings or load_settings()
    dirs: list[Path] = []
    for home in lmstudio_homes():
        folder = (read_json(home / "settings.json", {}) or {}).get("downloadsFolder")
        if folder:
            dirs.append(Path(folder))
        dirs.append(home / "models")
    dirs += [Path(p) for p in settings.get("extra_model_dirs", []) if p]
    if os.environ.get("RADAR_MODELS_DIR"):
        dirs.append(Path(os.environ["RADAR_MODELS_DIR"]))
    dirs.append(RUNTIME_DIR / "models")
    seen, out = set(), []
    for d in dirs:
        key = str(d.resolve()).lower() if d.exists() else ""
        if key and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def list_models(settings: dict | None = None) -> list[dict]:
    """Bulunan tüm GGUF modeller (mmproj ve bölünmüş dosyaların devam parçaları hariç)."""
    cache = read_json(_CACHE_FILE, {}) or {}
    fresh: dict = {}
    models: list[dict] = []
    for root in model_dirs(settings):
        for p in sorted(root.rglob("*.gguf")):
            m = _SPLIT_RE.search(p.name)
            if m and m.group(1) != "00001":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            size = st.st_size
            if m:   # bölünmüş model: tüm parçaların toplamı
                size = sum(q.stat().st_size for q in p.parent.glob(_SPLIT_RE.sub("-*-of-" + m.group(2) + ".gguf", p.name)))
            key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
            info = cache.get(key) or gguf.describe(p)
            fresh[key] = info
            if info["kind"] in ("mmproj", "diger"):
                continue
            models.append({
                "id": p.relative_to(root).as_posix(),
                "path": str(p),
                "file": p.name,
                "name": info["name"],
                "kind": info["kind"],
                "arch": info["arch"],
                "params": info["params"],
                "ctx": info["ctx"],
                "size_gb": round(size / 2**30, 1),
                "root": str(root),
            })
    if fresh != cache:
        write_json(_CACHE_FILE, fresh)
    models.sort(key=lambda x: (x["kind"], -x["size_gb"]))
    return models


def gpu_memory() -> dict | None:
    """NVIDIA ekran kartı belleği (MiB). Kart ya da nvidia-smi yoksa None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        name, total, free = [x.strip() for x in out[0].split(",")]
        return {"name": name, "total": int(total), "free": int(free)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def resolve_model(role: str, settings: dict | None = None, models: list[dict] | None = None) -> dict | None:
    """Rol için (chat | embed) seçili modeli; seçim yoksa donanıma uygun bir varsayılanı döndürür."""
    settings = settings or load_settings()
    models = models if models is not None else list_models(settings)
    kind = "chat" if role == "chat" else "embedding"
    wanted = settings.get("chat_model" if role == "chat" else "embed_model", "")
    pool = [m for m in models if m["kind"] == kind]
    for m in pool:
        if wanted and (m["id"] == wanted or m["path"] == wanted):
            return m
    if not pool:
        return None
    gpu = gpu_memory()
    # Terim üretiminde büyük modeller belirgin biçimde daha isabetli; bu yüzden sohbet için ekran kartı
    # belleğine (kısmen CPU'ya taşsa da) sığabilecek en büyük model seçilir. Gömme için küçük olan yeter.
    budget = (gpu["total"] / 1024) * (1.0 if role == "chat" else 0.45) if gpu else (8 if role == "chat" else 2)
    fitting = [m for m in pool if m["size_gb"] <= budget]
    return max(fitting, key=lambda m: m["size_gb"]) if fitting else min(pool, key=lambda m: m["size_gb"])
