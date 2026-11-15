"""GGUF dosya başlığından model üst verisini okur (mimari, ad, bağlam uzunluğu, gömme modeli mi?).

Tüm dosyayı değil yalnızca baştaki anahtar/değer bölümünü okur; 15 GB'lık model için de anlıktır.
"""

from __future__ import annotations

import struct
from pathlib import Path

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_SIZE = {k: struct.calcsize(v) for k, v in _SCALAR.items()}
_STRING, _ARRAY = 8, 9
_MAX_STR = 1 << 20


class GGUFError(ValueError):
    pass


def _read(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("beklenmeyen dosya sonu")
    return b


def _u64(f) -> int:
    return struct.unpack("<Q", _read(f, 8))[0]


def _string(f, keep: bool = True) -> str:
    n = _u64(f)
    if n > (1 << 26):
        raise GGUFError("bozuk dize uzunluğu")
    if keep and n <= _MAX_STR:
        return _read(f, n).decode("utf-8", "replace")
    f.seek(n, 1)
    return ""


def _value(f, typ: int, keep: bool):
    if typ in _SCALAR:
        return struct.unpack(_SCALAR[typ], _read(f, _SIZE[typ]))[0]
    if typ == _STRING:
        return _string(f, keep)
    if typ == _ARRAY:
        item_type = struct.unpack("<I", _read(f, 4))[0]
        count = _u64(f)
        if item_type in _SIZE:
            f.seek(_SIZE[item_type] * count, 1)
        elif item_type == _STRING:
            for _ in range(count):
                f.seek(_u64(f), 1)
        else:
            for _ in range(count):
                _value(f, item_type, False)
        return f"<dizi:{count}>"
    raise GGUFError(f"bilinmeyen değer türü {typ}")


def read_metadata(path: str | Path) -> dict:
    """Skaler ve kısa dize üst verisini sözlük olarak döndürür. GGUF değilse GGUFError."""
    meta: dict = {}
    with open(path, "rb", buffering=1 << 20) as f:
        if _read(f, 4) != b"GGUF":
            raise GGUFError("GGUF dosyası değil")
        version = struct.unpack("<I", _read(f, 4))[0]
        if version < 2:
            raise GGUFError(f"desteklenmeyen GGUF sürümü {version}")
        _u64(f)                      # tensör sayısı
        n_kv = _u64(f)
        if n_kv > 100_000:
            raise GGUFError("bozuk başlık")
        for _ in range(n_kv):
            key = _string(f)
            typ = struct.unpack("<I", _read(f, 4))[0]
            keep = not key.startswith("tokenizer.") or key == "tokenizer.chat_template"
            val = _value(f, typ, keep)
            if key == "tokenizer.chat_template":
                meta["has_chat_template"] = bool(val)
            elif keep:
                meta[key] = val
    return meta


def describe(path: str | Path) -> dict:
    """Model seçici için özet: tür (chat | embedding | mmproj | diger), mimari, bağlam uzunluğu…"""
    p = Path(path)
    info = {"kind": "chat", "arch": "", "name": p.stem, "ctx": 0, "params": "", "error": ""}
    try:
        m = read_metadata(p)
    except (GGUFError, OSError, struct.error) as exc:
        info.update(kind="diger", error=str(exc))
        return info
    arch = str(m.get("general.architecture", ""))
    gtype = str(m.get("general.type", "")).lower()
    info["arch"] = arch
    info["name"] = str(m.get("general.name") or m.get("general.basename") or p.stem)
    info["params"] = str(m.get("general.size_label", ""))
    info["ctx"] = int(m.get(f"{arch}.context_length", 0) or 0)
    low = p.name.lower()
    if gtype in ("mmproj", "projector") or arch == "clip" or "mmproj" in low:
        info["kind"] = "mmproj"
    elif gtype in ("adapter", "lora", "vocab"):
        info["kind"] = "diger"
    elif f"{arch}.pooling_type" in m or "bert" in arch or arch == "t5encoder" \
            or (not m.get("has_chat_template") and any(k in low for k in ("embed", "bge-", "e5-", "gte-"))) \
            or "embedding" in info["name"].lower() or "embed" in low:
        info["kind"] = "embedding"
    return info
