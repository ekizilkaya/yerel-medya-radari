"""OpenAI uyumlu uç noktaya (llama-server / LM Studio / Ollama) sohbet ve gömme istekleri."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import numpy as np

from .runtime import runtime


class LLMError(RuntimeError):
    pass


def _repair_json(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)                                   # sondaki virgüller
    s = re.sub(r"(?<=[{,])\s*([a-zA-Z_]\w*)\s*:", r' "\1":', s)            # tırnaksız anahtarlar
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    if s.count("{") > s.count("}") or s.count("[") > s.count("]"):         # yarıda kesilmiş çıktı
        cut = max(s.rfind("}"), s.rfind('"'))
        if cut > 0:
            s = s[: cut + 1]
        s += "]" * max(s.count("[") - s.count("]"), 0) + "}" * max(s.count("{") - s.count("}"), 0)
    return s


def parse_json(content: str) -> dict | None:
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    m = re.search(r"\{[\s\S]*\}", content) or re.search(r"\{[\s\S]*", content)
    if not m:
        return None
    for candidate in (m.group(), _repair_json(m.group())):
        try:
            out = json.loads(candidate)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            continue
    return None


async def chat_json(system: str, user: str, schema: dict, *, max_tokens: int = 2000,
                    temperature: float = 0.2, retries: int = 2, timeout: float = 600) -> dict:
    """Sohbet modelinden şemaya uygun JSON ister (llama.cpp'de dilbilgisi kısıtıyla garanti edilir)."""
    base = await asyncio.to_thread(runtime.ensure, "chat")
    payload = {
        "model": "local",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "cikti", "strict": True, "schema": schema}},
    }
    last = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                r = await client.post(base + "/chat/completions", json=payload)
                if r.status_code >= 400:
                    last = f"HTTP {r.status_code}: {r.text[:300]}"
                    if r.status_code == 400 and "response_format" in payload and attempt == 0:
                        payload["response_format"] = {"type": "json_object"}      # şema desteklemeyen sunucu
                    continue
                msg = r.json()["choices"][0]["message"]
                content = msg.get("content") or msg.get("reasoning_content") or ""
                data = parse_json(content)
                if data is not None:
                    return data
                last = "JSON ayrıştırılamadı: " + content[:200]
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.5)
            payload["temperature"] = min(temperature + 0.2 * (attempt + 1), 0.8)
    raise LLMError(f"Dil modeli geçerli yanıt vermedi ({last})")


async def embed(texts: list[str], *, batch: int = 48, timeout: float = 600, on_progress=None) -> np.ndarray:
    """Metinleri gömme modeliyle vektörler; satırları birim uzunluğa normalleştirilmiş matris döndürür."""
    if not texts:
        return np.zeros((0, 1), dtype=np.float32)
    base = await asyncio.to_thread(runtime.ensure, "embed")
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(0, len(texts), batch):
            chunk = [t[:1500] or " " for t in texts[i:i + batch]]
            for attempt in range(3):
                try:
                    r = await client.post(base + "/embeddings", json={"model": "local", "input": chunk})
                    r.raise_for_status()
                    data = sorted(r.json()["data"], key=lambda d: d["index"])
                    out.extend(d["embedding"] for d in data)
                    break
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    if attempt == 2:
                        raise LLMError(f"Gömme isteği başarısız: {exc}") from exc
                    await asyncio.sleep(2)
            if on_progress:
                on_progress(min(i + batch, len(texts)), len(texts))
    m = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.clip(norms, 1e-9, None)
