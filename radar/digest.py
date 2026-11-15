"""Gündem özeti: ulusal medyaya taşınabilecek önemli yerel haberleri yerel dil modeliyle seçer.

(Önceki sürümün ana özelliği; yeni tarayıcı, arşiv ve model katmanı üzerine taşındı.)
  başlıkları kümele → çok kaynaklı / anahtar sözcüklü kümeleri ön-süz → LLM ile seç ve özetle
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .llm import client
from .textnorm import content_tokens

_IMPORTANT = re.compile(
    r"(deprem|sel\b|heyelan|yangın|patlama|kaza|ölü|öldü|cinayet|saldırı|gözaltı|tutukla|yolsuzluk|rüşvet|"
    r"ihale|soruşturma|operasyon|uyuşturucu|kaçak|çökme|göçük|sızıntı|kirlilik|zehir|maden|fırtına|hortum|"
    r"afet|enkaz|mahsur|kayıp|salgın|virüs|tahliye|keşif|fosil|arkeoloj|tarihi eser|ilk kez|rekor|"
    r"balık ölüm|su kesinti|baraj|taşkın|kuraklık|grev|eylem|protesto|iflas|konkordato|kapandı|işten çıkar)",
    re.IGNORECASE,
)

SYSTEM = ("Türkiye yerel basınını izleyen deneyimli bir haber editörüsün. Yalnızca istenen JSON nesnesini döndür.")

PROMPT = """Aşağıda yerel haber kümeleri var (K = aynı haberi veren kaynak sayısı, köşeli parantezde iller).

{clusters}

Görev: Yalnızca ulusal medyaya taşınmaya değer, YEREL kaynaklı önemli haberleri seç.
SEÇ: afet, büyük kaza, çevre sorunu, ciddi suç, yolsuzluk iddiası, salgın, keşif, yerel ekonomiyi sarsan gelişme, sıra dışı olay.
ELE: ulusal gündem ve Ankara siyaseti, spor, magazin, rutin belediye hizmeti, açılış, ziyaret, anma, kutlama, festival, hava durumu, burç, ilan.
Her seçtiğin haber için küme numarasını, kısa bir başlığı, 1-2 cümlelik özeti ve bir kategori ver. Uygun haber yoksa boş liste döndür."""

SCHEMA = {
    "type": "object",
    "properties": {"haberler": {"type": "array", "items": {
        "type": "object",
        "properties": {"kume_no": {"type": "integer"}, "baslik": {"type": "string"},
                       "ozet": {"type": "string"}, "kategori": {"type": "string"}},
        "required": ["kume_no", "baslik", "ozet", "kategori"],
    }}},
    "required": ["haberler"],
}


def cluster_titles(rows: list[dict], threshold: float = 0.62) -> list[list[dict]]:
    """Benzer başlıkları kümeler (sözcük TF-IDF + kosinüs, birleşik bileşenler). rows: [{t, s, c, u, ...}]"""
    if len(rows) < 2:
        return [[r] for r in rows]
    from sklearn.feature_extraction.text import TfidfVectorizer

    vec = TfidfVectorizer(analyzer=content_tokens, min_df=1, sublinear_tf=True, dtype="float32")
    x = vec.fit_transform(r["t"] for r in rows).tocsr()
    parent = list(range(len(rows)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    step = 2000
    xt = x.T.tocsr()
    for i in range(0, x.shape[0], step):
        sim = (x[i:i + step] @ xt).tocoo()
        for a, b, v in zip(sim.row, sim.col, sim.data):
            if v >= threshold and (a + i) < b:
                ra, rb = find(a + i), find(b)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[int, list[dict]] = defaultdict(list)
    for k, r in enumerate(rows):
        groups[find(k)].append(r)
    return sorted(groups.values(), key=len, reverse=True)


def prefilter(clusters: list[list[dict]], max_total: int = 240) -> list[list[dict]]:
    """Çok ilde/kaynakta çıkan kümeler ile önem sözcüğü içeren tekil haberler."""
    multi, keyword = [], []
    for c in clusters:
        sources = {r["s"] for r in c}
        cities = {r["c"] for r in c}
        if len(sources) >= 2 and len(cities) <= 6:      # 6'dan çok ilde aynı başlık → büyük olasılıkla ajans/ulusal haber
            multi.append(c)
        elif len(sources) == 1 and _IMPORTANT.search(c[0]["t"]):
            keyword.append(c)
    multi.sort(key=lambda c: (-len({r["s"] for r in c}), len({r["c"] for r in c})))
    keyword.sort(key=lambda c: -len(c[0]["t"]))
    half = max_total // 2
    return multi[:half] + keyword[: max_total - min(len(multi), half)]


async def run_digest(rows: list[dict], *, batch: int = 10, max_clusters: int = 240, progress=None) -> list[dict]:
    """rows: son 1-2 günün başlıkları. Dönüş: [{title, summary, category, city, url, source, sources, cities}]"""
    def step(stage: str, detail: str) -> None:
        if progress:
            progress(stage, detail)

    step("kume", f"{len(rows)} başlık kümeleniyor…")
    clusters = cluster_titles(rows)
    picked = prefilter(clusters, max_clusters)
    step("kume", f"{len(clusters)} küme → {len(picked)} aday")
    results: list[dict] = []
    seen_titles: set[str] = set()
    for i in range(0, len(picked), batch):
        group = picked[i:i + batch]
        lines = []
        for n, c in enumerate(group, 1):
            cities = "/".join(sorted({r["c"] for r in c})[:4])
            title = max((r["t"] for r in c), key=len)[:150]
            lines.append(f"{n}. [{len({r['s'] for r in c})}K, {cities}] {title}")
        try:
            out = await client.chat_json(SYSTEM, PROMPT.format(clusters="\n".join(lines)), SCHEMA,
                                         max_tokens=1200, temperature=0.2)
        except client.LLMError as exc:
            step("llm", f"grup atlandı: {exc}")
            continue
        for item in out.get("haberler", []):
            k = item.get("kume_no")
            if not isinstance(k, int) or not (1 <= k <= len(group)):
                continue
            c = group[k - 1]
            best = max(c, key=lambda r: len(r.get("x", "")) + len(r["t"]) / 10)
            by_city = Counter(r["c"] for r in c)          # haberi en çok veren il büyük olasılıkla olayın yeri
            main_city, others = by_city.most_common(1)[0][0], len(by_city) - 1
            news = {
                "title": str(item.get("baslik") or best["t"]).strip(),
                "summary": str(item.get("ozet", "")).strip(),
                "category": str(item.get("kategori", "")).strip(),
                "city": main_city + (f" +{others} il" if others else ""),
                "url": best.get("u", ""), "source": best["s"],
                "sources": len({r["s"] for r in c}), "original": best["t"],
            }
            key = " ".join(content_tokens(news["title"]))
            if key in seen_titles:          # aynı olay iki ayrı kümeden seçilmiş
                continue
            seen_titles.add(key)
            results.append(news)
            step("haber", news)
        step("llm", f"{min(i + batch, len(picked))}/{len(picked)} aday değerlendirildi · {len(results)} haber seçildi")
    return results
