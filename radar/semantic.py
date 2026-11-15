"""Gömme (embedding) tabanlı anlamsal katman.

Kalıplara takılmayan ama anlamca konuya yakın başlıkları "anlamsal aday" olarak bulur.
Eşik modele göre değişmesin diye kendini ayarlar: günün tüm başlıkları arka plan dağılımını,
konunun bilinen (kalıpla yakalanmış) başlıkları ise hedef dağılımını verir.
"""

from __future__ import annotations

import numpy as np

from .topics import Topic


MAX_DIM = 1024


def reduce(vecs: np.ndarray) -> np.ndarray:
    """Çok boyutlu gömmeleri (ör. 4096) ilk 1024 boyuta kırpıp yeniden normalleştirir.
    Büyük gömme modelleri (Qwen3-Embedding vb.) Matryoshka eğitimlidir; kırpma anlamı korur,
    önbellek ise dört kat küçülür. 1024 ve altı boyutlu modellerde hiçbir şey değişmez."""
    if vecs.ndim != 2 or vecs.shape[1] <= MAX_DIM:
        return vecs
    cut = vecs[:, :MAX_DIM]
    return cut / np.clip(np.linalg.norm(cut, axis=1, keepdims=True), 1e-9, None)


async def embed_cached(store, ids: list[str], texts: list[str], on_progress=None) -> np.ndarray:
    """Başlık vektörlerini SQLite önbelleğinden getirir; eksikleri gömme modeliyle üretip saklar."""
    from .llm import client
    from .llm.models import resolve_model

    model = resolve_model("embed")
    tag = model["file"] if model else "harici"
    have = store.get_vectors(ids, tag)
    missing = [k for k, i in enumerate(ids) if i not in have]
    if missing:
        fresh = reduce(await client.embed([texts[k] for k in missing], on_progress=on_progress))
        store.put_vectors([ids[k] for k in missing], fresh, tag)
        for k, v in zip(missing, fresh):
            have[ids[k]] = v
    return np.stack([have[i] for i in ids]).astype(np.float32)


def probe_texts(topic: Topic) -> list[str]:
    """Konuyu temsil eden kısa metinler: tanım, çekirdek terimler ve örnek başlıklar."""
    texts = []
    head = topic.keyword or topic.title
    if topic.description:
        texts.append(f"{head}: {topic.description}")
    else:
        texts.append(head)
    core = [t.t for t in topic.terms if t.on and t.kind == "core" and not t.t.startswith("re:")]
    if core:
        texts.append(", ".join(x.strip('"*') for x in core[:25]))
    texts += [e for e in topic.examples[:12] if e]
    return texts


def _robust_z(x: np.ndarray, background: np.ndarray) -> np.ndarray:
    """Arka planın ortancasına göre, sağlam sapma (MAD) biriminde uzaklık. Model ne olursa olsun
    aynı ölçekte bir puan verir; bu yüzden eşik gömme modeline göre ayarlanmak zorunda kalmaz."""
    bg = x[background] if background.sum() >= 50 else x
    med = float(np.median(bg))
    sigma = float(np.median(np.abs(bg - med))) * 1.4826 or float(bg.std()) or 1e-6
    return (x - med) / sigma


def score_titles(title_vecs: np.ndarray, probe_vecs: np.ndarray, positive_vecs: np.ndarray | None,
                 matched: np.ndarray, k: int = 3) -> np.ndarray:
    """Her başlık için konu puanı (σ biriminde; büyük = arka plandan daha belirgin ayrışıyor).

    İki ölçünün KÜÇÜĞÜ alınır, yani başlık ikisini birden sağlamalıdır:
      • tanım benzerliği  — konu tanımı ve örnek başlıkların merkezine yakınlık
      • komşu benzerliği  — kalıpla yakalanmış bilinen konu haberlerinden en yakın `k` tanesine yakınlık
    Tek başına komşu benzerliği aynı kişinin başka konudaki haberini ("Bakan X'ten zam açıklaması"),
    tek başına tanım benzerliği ise genel alan haberlerini öne çıkarır; ikisi birlikte daha isabetlidir.
    Yeterli bilinen eşleşme yoksa yalnızca tanım benzerliği kullanılır.
    """
    background = ~matched
    v = probe_vecs.mean(axis=0)
    v = (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)
    z = _robust_z(title_vecs @ v, background)
    if positive_vecs is not None and len(positive_vecs) >= 20:
        pos = positive_vecs[-300:]
        sims = title_vecs @ pos.T
        kk = min(k, pos.shape[0])
        knn = np.partition(sims, -kk, axis=1)[:, -kk:].mean(axis=1)
        z = np.minimum(z, _robust_z(knn, background))
    return z


def pick_candidates(z_scores: np.ndarray, matched: np.ndarray, *, z: float = 3.5, max_n: int = 25) -> list[int]:
    """Puanı eşiği aşan, kalıpla eşleşmemiş başlıkların indeksleri (yüksek puandan düşüğe)."""
    if len(z_scores) < 50:
        return []
    order = np.argsort(-z_scores)
    return [int(i) for i in order if not matched[i] and z_scores[i] >= z][:max_n]
