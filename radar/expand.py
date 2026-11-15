"""Anlamsal genişletme hattı: tek bir anahtar kelimeden izlenebilir bir konu tanımı üretir.

Adımlar
  1. LLM genişletme     — arşivde anahtar kelimenin geçtiği gerçek başlıklar modele gösterilir; model
                          çekirdek/ilişkili terimler, kurumlar, belirsiz terimler, bağlam, dışlama ve örnek başlık üretir
  2. LLM derinleştirme  — alt temalar ve ek terimler (isteğe bağlı, depth=2)
  3. Temizlik           — gereksiz tamlamalar atılır; her terim için Türkçe çekim eklerine duyarlı kalıp (radar.topics)
  4. Derlem sınaması    — her terimin yerel arşivde kaç başlık yakaladığı; arşivde görülmeyen terimler ve
                          dışlamalar dil modeline ikinci kez sorgulatılır
  5. LLM denetimi       — her terimin yakaladığı örnek başlıklar gerçekten konuyla ilgili mi? Kesinliği yüksek
                          terimler "çekirdek" olur, düşükler bağlam ister ya da kapatılır (kanıta dayalı terfi)
  6. Gömme analizi      — terim-konu benzerliği; hiçbir terime takılmayan ama anlamca yakın başlıklar (anlamsal
                          komşular) ve onlardan çıkan yeni terim önerileri
  7. Birliktelik        — konu haberlerinde orantısız sık geçen sözcükler (model gerektirmez)

Sonuç bir `Topic` taslağı ve kullanıcıya sunulacak öneri listesidir; son söz kullanıcıdadır.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter

import numpy as np

from .cities import CITY_NAMES
from . import semantic
from .llm import client
from .textnorm import content_tokens, tr_lower
from .topics import Term, Topic, slugify, term_regex

SYSTEM = (
    "Türkiye yerel basınını izleyen deneyimli bir medya analisti ve Türkçe dil uzmanısın. "
    "Görevin, verilen konuyu yerel haber başlıklarında yakalayacak terimleri üretmek ve değerlendirmek. "
    "Yalnızca istenen JSON nesnesini döndür."
)

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA_EXPAND = {
    "type": "object",
    "properties": {
        "tanim": {"type": "string"},
        "cekirdek": _STR_LIST, "iliskili": _STR_LIST, "kurumlar": _STR_LIST,
        "belirsiz": _STR_LIST, "baglam": _STR_LIST, "dislama": _STR_LIST, "ornek_basliklar": _STR_LIST,
    },
    "required": ["tanim", "cekirdek", "iliskili", "kurumlar", "belirsiz", "baglam", "dislama", "ornek_basliklar"],
}
SCHEMA_DEEPEN = {
    "type": "object",
    "properties": {"alt_temalar": {"type": "array", "items": {
        "type": "object",
        "properties": {"ad": {"type": "string"}, "terimler": _STR_LIST},
        "required": ["ad", "terimler"],
    }}},
    "required": ["alt_temalar"],
}
SCHEMA_AUDIT = {
    "type": "object",
    "properties": {"ilgili": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ilgili"],
}

PROMPT_EXPAND = """Konu anahtar kelimesi: "{keyword}"
{note}{samples}
Görev: Bu konuyla ilgili haberleri Türkiye'deki yerel gazete ve haber sitelerinin BAŞLIKLARINDA yakalamak için terim listeleri hazırla.

Alanlar:
- tanim: Konunun kapsamını anlatan 1-2 cümle.
- cekirdek: Anahtar kelimenin kendisi, eş/yakın anlamlıları ve başlıkta tek başına geçtiğinde konuyu kesin gösteren terimler (en çok 12).
- iliskili: Anahtar kelimeyi İÇERMEYEN ama konuyla doğrudan ilgili, yerel haberlerde sık geçen ifadeler; mümkünse 2-3 sözcüklü tamlamalar (en çok 25).
- kurumlar: Konuyla doğrudan ilgili, GERÇEKTEN VAR OLAN kurum ve program adları. Yaygın kısaltması varsa yalnızca kısaltmayı BÜYÜK harfle yaz. Emin olmadığın adı yazma, kısaltma uydurma (en çok 15).
- belirsiz: Konuyla ilgili olabilen ama başka anlamlarda da çok kullanılan genel sözcükler (en çok 8).
- baglam: Belirsiz sözcüklerin bu konuda kullanıldığını doğrulayan bağlam sözcükleri (en çok 12).
- dislama: Anahtar kelimenin BAŞKA ANLAMDA (mecaz, eşsesli kullanım, deyim) geçtiği çok sözcüklü kalıplar. Konuyla ilgili olabilecek hiçbir ifadeyi buraya yazma; emin değilsen boş bırak (en çok 6).
- ornek_basliklar: Bu konuda yerel basında çıkabilecek 8 gerçekçi, birbirinden farklı haber başlığı.

Kurallar:
- Terimleri yalın (sözlük) biçiminde yaz; çekim eki ekleme, ekleri sistem kendisi üretir. Doğru: "ihale", "yapım işi". Yanlış: "ihaleler", "yapım işinde".
- Anahtar kelimeyi içeren tamlamaları terim olarak yazma; anahtar kelime onları zaten yakalar.
- Yerel basının gerçekten kullandığı doğal Türkçe ifadeleri seç; yapay, çeviri kokan ya da uydurma terim yazma.
- Kişi adı ve siyasi parti adı yazma; il/ilçe adı yazma.
- Aynı terimi birden fazla alana yazma."""

PROMPT_EXCLUDE = """Konu: "{keyword}" — {description}

Aşağıdaki numaralı kalıpları içeren haberlerin bu konunun taramasından ÇIKARILMASI öneriliyor.
Bir kalıp ancak anahtar kelimeyi bambaşka bir anlamda (mecaz, deyim, eşsesli sözcük) kullanıyorsa çıkarılmalıdır.
Kalıbı içeren bir haber bu konuyla ilgili OLABİLİYORSA o kalıp çıkarılmamalıdır.
Yalnızca gerçekten çıkarılması gereken (konuyla ilgisiz) kalıpların numaralarını "uygun" listesinde döndür.

{terms}"""

PROMPT_DEEPEN = """Konu: "{keyword}" — {description}

Şu terimler zaten listede:
{existing}

Bu konunun yerel basında karşımıza çıkabilecek, yukarıdaki listenin KAÇIRDIĞI alt temalarını düşün
(ör. farklı sektörler, yararlanıcı grupları, başvuru/ödeme/denetim/soruşturma gibi haber türleri, yerel kurumlar).
En çok 6 alt tema ver; her biri için başlıklarda geçebilecek en çok 6 yeni terim yaz.
Terimleri yalın biçimde yaz, listede olanları tekrarlama, konudan uzaklaşma.
Anahtar kelimeyi içeren tamlama yazma (anahtar kelime onları zaten yakalar); uydurma kurum adı ya da yapay tamlama yazma."""

PROMPT_AUDIT = """Konu: "{keyword}" — {description}

Aşağıda numaralı yerel haber başlıkları var. Yalnızca gerçekten bu konuyla ilgili olanların numaralarını "ilgili" listesinde döndür.
Konuyla ilgisi yoksa ya da sözcük başka anlamda kullanılmışsa numarayı listeye KOYMA.

{titles}"""

PROMPT_PRUNE = """Konu: "{keyword}" — {description}

Aşağıdaki numaralı terimler bu konunun haberlerini yerel basın başlıklarında yakalamak için önerildi.
Yalnızca şu koşulların HEPSİNİ sağlayanların numaralarını "uygun" listesinde döndür:
(a) Türkçede gerçekten kullanılan doğal bir ifade ya da gerçekten var olan bir kurum/program adı,
(b) yerel haber başlıklarında geçmesi olası,
(c) bu konuyla doğrudan ilgili.
Uydurma kurum adlarını, yapay tamlamaları ve fazla genel sözcükleri listeye KOYMA.

{terms}"""

SCHEMA_PRUNE = {
    "type": "object",
    "properties": {"uygun": {"type": "array", "items": {"type": "integer"}}},
    "required": ["uygun"],
}


def _clean_terms(items, limit: int) -> list[str]:
    out, seen = [], set()
    for x in items or []:
        if not isinstance(x, str):
            continue
        t = re.sub(r"\s*\([^)]*\)", "", x)                      # "(TDF)" gibi parantez içlerini at
        t = re.sub(r"\s+", " ", t).strip(" .,;:-\"'“”*")
        if len(t.split()) >= 2 and t.isupper():                  # BÜYÜK HARFLİ TAMLAMA kısaltma değildir
            t = tr_lower(t)
        key = tr_lower(t)
        if 2 <= len(t) <= 60 and key not in seen and len(t.split()) <= 4:
            seen.add(key)
            out.append(t)
    return out[:limit]


def _drop_redundant(terms: list[Term]) -> list[str]:
    """Daha kısa bir çekirdek terimin zaten yakaladığı tamlamaları ayıklar ("hibe fonu" ⊂ "fon")."""
    compiled = []
    for t in terms:
        if not t.t.startswith("re:"):
            pat, cs = term_regex(t.t)
            try:
                compiled.append((t, re.compile(pat) if cs else re.compile(pat, re.I), cs))
            except re.error:
                pass
    dropped = []
    for t in list(terms):
        if t.src == "seed" or t.t.startswith("re:"):
            continue
        for other, rx, cs in compiled:
            if other is t or len(other.t) >= len(t.t) or other not in terms:
                continue
            if other.kind != "core" and t.kind == "core":
                continue                    # zayıf bir terim, çekirdek bir tamlamayı gereksiz kılmaz
            if rx.search(t.t if cs else tr_lower(t.t)):
                terms.remove(t)
                dropped.append(t.t)
                break
    return dropped


def _dedupe_by_prefix(cands: list[tuple], n: int = 6) -> list[tuple]:
    """"yatırım / yatırımı / yatırımları" gibi aynı gövdenin çekimlerinden yalnızca ilkini tutar."""
    seen, out = set(), []
    for c in cands:
        key = " ".join(w[:n] for w in c[0].split())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _is_acronym(t: str) -> bool:
    letters = [c for c in t if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters) and " " not in t


class Corpus:
    """Yerel arşivden alınan başlıklar üzerinde hızlı sınama."""

    def __init__(self, titles: list[str]):
        self.titles = titles
        self.low = [tr_lower(t) for t in titles]

    def hits(self, term: str, cap: int = 5000) -> list[int]:
        pat, cs = term_regex(term)
        try:
            rx = re.compile(pat) if cs else re.compile(pat, re.I)
        except re.error:
            return []
        src = self.titles if cs else self.low
        out = []
        for i, s in enumerate(src):
            if rx.search(s):
                out.append(i)
                if len(out) >= cap:
                    break
        return out


async def expand_keyword(
    keyword: str,
    *,
    note: str = "",
    corpus_titles: list[str] | None = None,
    depth: int = 1,                     # 2: alt temaları da tara (daha geniş ama daha yavaş, sapma riski daha yüksek)
    use_embeddings: bool = True,
    audit: bool = True,
    embed_titles=None,                  # async (anahtarlar, başlıklar) → vektörler; önbellekli gömme (bkz. semantic.embed_cached)
    semantic_sample: int = 15000,       # anlamsal komşu aramasında kullanılacak en güncel başlık sayısı
    progress=None,                      # callable(aşama, ayrıntı)
) -> dict:
    """Anahtar kelimeden konu taslağı üretir.
    Dönüş: {'topic': dict, 'suggestions': [...], 'neighbors': [...], 'log': [...]}"""
    log: list[str] = []
    neighbors: list[dict] = []

    def step(stage: str, detail: str) -> None:
        log.append(f"{stage}: {detail}")
        if progress:
            progress(stage, detail)

    keyword = keyword.strip()
    rng = random.Random(42)
    corpus = Corpus(corpus_titles) if corpus_titles else None

    # ── 1. LLM genişletme (arşivdeki gerçek kullanımlara dayandırılır) ────
    samples = ""
    if corpus:
        seed_hits = corpus.hits(keyword, cap=3000)
        if seed_hits:
            picked, seen_keys = [], set()
            for j in rng.sample(seed_hits, len(seed_hits)):
                key = corpus.low[j][:40]
                if key not in seen_keys:
                    seen_keys.add(key)
                    picked.append(corpus.titles[j][:150])
                if len(picked) >= 30:
                    break
            samples = ("\nYerel basında bu anahtar kelimenin geçtiği güncel başlıklardan örnekler "
                       "(kelimenin gerçekte nasıl kullanıldığını görmen için; terimleri bunlarla sınırlama):\n"
                       + "\n".join(f"- {p}" for p in picked) + "\n")
            step("derlem", f"Anahtar kelime arşivde {len(seed_hits)} başlıkta geçiyor; {len(picked)} örnek modele gösteriliyor")
    step("llm", f"“{keyword}” için terimler üretiliyor…")
    note_line = f'Kullanıcının notu (kapsamı buna göre belirle): "{note.strip()}"\n' if note.strip() else ""
    raw = await client.chat_json(SYSTEM, PROMPT_EXPAND.format(keyword=keyword, note=note_line, samples=samples),
                                 SCHEMA_EXPAND, max_tokens=2500, temperature=0.3)
    description = str(raw.get("tanim", "")).strip()
    core = _clean_terms([keyword] + list(raw.get("cekirdek", [])), 13)
    related = _clean_terms(raw.get("iliskili"), 25)
    orgs = _clean_terms(raw.get("kurumlar"), 15)
    ambiguous = _clean_terms(raw.get("belirsiz"), 8)
    context = _clean_terms(raw.get("baglam"), 12)
    exclude = _clean_terms(raw.get("dislama"), 8)
    examples = [e.strip() for e in raw.get("ornek_basliklar", []) if isinstance(e, str) and len(e.strip()) > 15][:10]

    terms: dict[str, Term] = {}

    def add(t: str, kind: str, src: str) -> None:
        key = tr_lower(t)
        if key not in terms:
            terms[key] = Term(t=t, kind=kind, src=src)

    # Yalnızca anahtar kelime ve eş anlamlıları "çekirdek" başlar. Kurumlar ve ilişkili ifadeler
    # "bağlam gerektirir" olarak başlar; arşivdeki örnekleri denetimden yüksek kesinlikle geçerse
    # çekirdeğe yükseltilir (kanıta dayalı terfi — konudan sapmayı önler).
    for t in core:
        add(t, "core", "seed" if tr_lower(t) == tr_lower(keyword) else "llm")
    for t in orgs:
        add(t, "weak", "llm-kurum")
    for t in related:
        add(t, "weak", "llm-iliskili")
    for t in ambiguous:
        add(t, "weak", "llm-belirsiz")
    step("llm", f"{len(terms)} terim, {len(exclude)} dışlama, {len(examples)} örnek başlık")

    # ── 2. LLM derinleştirme ─────────────────────────────────────────────
    if depth >= 2:
        step("llm", "Alt temalar aranıyor…")
        try:
            deep = await client.chat_json(
                SYSTEM,
                PROMPT_DEEPEN.format(keyword=keyword, description=description, existing=", ".join(t.t for t in terms.values())),
                SCHEMA_DEEPEN, max_tokens=1500, temperature=0.4)
            themes = []
            for theme in deep.get("alt_temalar", [])[:6]:
                added = 0
                for t in _clean_terms(theme.get("terimler"), 6):
                    if tr_lower(t) not in terms:
                        add(t, "weak", "llm-alt")
                        added += 1
                if added:
                    themes.append(str(theme.get("ad", "")).strip())
            step("llm", f"Alt temalar: {', '.join(t for t in themes if t) or '—'} → toplam {len(terms)} terim")
        except client.LLMError as exc:
            step("llm", f"Alt tema adımı atlandı ({exc})")

    # ── 3. Temizlik ve kalıp üretimi ─────────────────────────────────────
    term_list = list(terms.values())
    dropped = _drop_redundant(term_list)
    if dropped:
        step("temizlik", f"Daha kısa bir terimin zaten yakaladığı {len(dropped)} tamlama çıkarıldı")
    # Bağlam sözcüğü aynı zamanda terim olarak da önerildiyse bağlam olarak kalsın, terim listesinden çıksın
    # (anahtar kelimenin kendisi hariç): genel bir sözcük tek başına terim olmaktan çok doğrulayıcı olarak işe yarar.
    context = [c for c in context if tr_lower(c) != tr_lower(keyword)]
    ctx_keys = {tr_lower(c) for c in context}
    term_list = [t for t in term_list if t.src == "seed" or tr_lower(t.t) not in ctx_keys]
    # Dışlama yalnızca anahtar kelimeyi/çekirdek terimi içeren çok sözcüklü kalıplar için anlamlıdır
    core_rx = [re.compile(term_regex(t.t)[0], re.I) for t in term_list
               if t.kind == "core" and not t.t.startswith("re:") and not _is_acronym(t.t)]
    exclude = [e for e in exclude if len(e.split()) >= 2 and any(rx.search(tr_lower(e)) for rx in core_rx)]

    topic = Topic(slug=slugify(keyword), title=keyword[:1].upper() + keyword[1:], keyword=keyword,
                  description=description, terms=term_list, context=context,
                  exclude=exclude, examples=examples)
    suggestions: list[dict] = []

    # ── 4. Derlem sınaması ───────────────────────────────────────────────
    term_hits: dict[str, list[int]] = {}
    if corpus:
        step("derlem", f"{len(corpus.titles):,} başlıkta terimler sınanıyor…".replace(",", "."))
        for t in topic.terms:
            idx = corpus.hits(t.t)
            term_hits[t.t] = idx
            t.hits = len(idx)
        step("derlem", f"{sum(1 for t in topic.terms if t.hits)}/{len(topic.terms)} terim arşivde en az bir başlık yakaladı")
    else:
        step("derlem", "Yerel arşiv boş — önce bir tarama yaparsanız terimler gerçek başlıklarla sınanır.")

    # ── 4b. Arşivde karşılığı olmayan terimleri dil modeline ayıklat ─────
    if audit:
        unseen = [t for t in topic.terms if t.src != "seed" and not t.hits]
        if len(unseen) >= 3:
            listing = "\n".join(f"{n + 1}. {t.t}" for n, t in enumerate(unseen))
            try:
                res = await client.chat_json(
                    SYSTEM, PROMPT_PRUNE.format(keyword=keyword, description=description, terms=listing),
                    SCHEMA_PRUNE, max_tokens=300, temperature=0.0)
                keep = {int(x) for x in res.get("uygun", []) if isinstance(x, (int, float))}
                off = [t for n, t in enumerate(unseen) if n + 1 not in keep]
                if keep:                    # model hiçbirini seçmediyse yanıtı güvenilmez say
                    for t in off:
                        t.on = False
                    step("temizlik", f"Arşivde görülmeyen {len(unseen)} terimden {len(off)} tanesi doğal/ilgili bulunmadı ve kapatıldı")
            except client.LLMError as exc:
                step("temizlik", f"Ayıklama adımı atlandı ({exc})")
        # Yanlış bir dışlama ilgili haberleri sessizce gizler; bu yüzden dışlamalar ikinci kez sorgulanır
        if topic.exclude:
            listing = "\n".join(f"{n + 1}. {e}" for n, e in enumerate(topic.exclude))
            try:
                res = await client.chat_json(
                    SYSTEM, PROMPT_EXCLUDE.format(keyword=keyword, description=description, terms=listing),
                    SCHEMA_PRUNE, max_tokens=200, temperature=0.0)
                keep = {int(x) for x in res.get("uygun", []) if isinstance(x, (int, float))}
                before = len(topic.exclude)
                topic.exclude = [e for n, e in enumerate(topic.exclude) if n + 1 in keep]
                step("temizlik", f"Dışlamalar yeniden sorgulandı: {before} öneriden {len(topic.exclude)} tanesi kaldı")
            except client.LLMError:
                topic.exclude = []

    # ── 5. LLM denetimi ──────────────────────────────────────────────────
    async def judge(pairs: list[tuple[Term, list[int]]], per_call: int = 8) -> dict[str, tuple[int, int]]:
        """Her terim için örnek başlıkları modele sorar. Dönüş: {terim: (ilgili, sorulan)}"""
        tally: dict[str, list[int]] = {}
        for i in range(0, len(pairs), per_call):
            numbered, owner = [], []
            for t, idx in pairs[i:i + per_call]:
                for j in rng.sample(idx, min(5, len(idx))):
                    numbered.append(corpus.titles[j][:160])
                    owner.append(t)
            order = list(range(len(numbered)))
            rng.shuffle(order)
            listing = "\n".join(f"{n + 1}. {numbered[j]}" for n, j in enumerate(order))
            try:
                res = await client.chat_json(
                    SYSTEM, PROMPT_AUDIT.format(keyword=keyword, description=description, titles=listing),
                    SCHEMA_AUDIT, max_tokens=400, temperature=0.0)
            except client.LLMError:
                continue
            relevant = {int(x) for x in res.get("ilgili", []) if isinstance(x, (int, float))}
            for n, j in enumerate(order):
                cell = tally.setdefault(owner[j].t, [0, 0])
                cell[1] += 1
                cell[0] += int(n + 1 in relevant)
            step("denetim", f"{min(i + per_call, len(pairs))}/{len(pairs)} terim denetlendi")
        return {k: (v[0], v[1]) for k, v in tally.items()}

    if audit and corpus:
        todo = [t for t in topic.terms if t.on and term_hits.get(t.t)]
        if todo:
            step("denetim", f"{len(todo)} terimin yakaladığı örnek başlıklar dil modeline soruluyor…")
        result = await judge([(t, term_hits[t.t]) for t in todo])
        for t in todo:
            ok, tot = result.get(t.t, (0, 0))
            if not tot:
                continue
            t.prec = ok / tot
            if t.src == "seed":
                continue
            if (ok == 0 and tot >= 2) or (tot >= 4 and t.prec < 0.25):
                t.on = False                     # yakaladıklarının neredeyse hiçbiri konuyla ilgili değil
            elif tot >= 3 and t.prec >= 0.8:
                t.kind = "core"                  # kanıtla çekirdeğe terfi
            elif t.prec < 0.7:
                t.kind = "weak"

        # İkinci tur: "bağlam gerektirir" terimleri bağlam sözcüğüyle birlikte geçtikleri başlıklarda sına.
        # Bağlamla bile isabetsizse (ör. "bina" + "belediye") terim kapatılır.
        ctx_rx = topic.matcher()._context
        if ctx_rx:
            pairs = []
            for t in topic.terms:
                if t.on and t.kind == "weak" and term_hits.get(t.t):
                    with_ctx = [j for j in term_hits[t.t] if ctx_rx.search(corpus.low[j])]
                    if len(with_ctx) >= 2:
                        pairs.append((t, with_ctx))
            if pairs:
                step("denetim", f"{len(pairs)} bağlam gerektiren terim, bağlamıyla birlikte sınanıyor…")
                result = await judge(pairs)
                for t, _ in pairs:
                    ok, tot = result.get(t.t, (0, 0))
                    if tot >= 2:
                        t.prec = ok / tot
                        if t.prec < 0.5:
                            t.on = False
        if todo:
            core_now = [t.t for t in topic.terms if t.on and t.kind == "core"]
            off = [t.t for t in topic.terms if not t.on]
            step("denetim", f"Çekirdek: {', '.join(core_now) or '—'} | Kapatılan: {len(off)} terim")

    # ── 6. Gömme analizi ─────────────────────────────────────────────────
    if use_embeddings:
        try:
            step("gomme", "Terimler gömme modeliyle karşılaştırılıyor…")
            seed_texts = [f"{keyword}: {description}"] + examples[:8]
            term_texts = [t.t.strip('"*') for t in topic.terms if not t.t.startswith("re:")]
            vecs = await client.embed(seed_texts + term_texts)
            seed = vecs[: len(seed_texts)].mean(axis=0)
            seed /= max(float(np.linalg.norm(seed)), 1e-9)
            sims = vecs[len(seed_texts):] @ seed
            k = 0
            for t in topic.terms:
                if not t.t.startswith("re:"):
                    t.sim = float(sims[k])
                    k += 1
            floor = float(np.median(sims) - 2.0 * (np.median(np.abs(sims - np.median(sims))) * 1.4826 + 1e-6))
            for t in topic.terms:
                if t.sim is not None and t.sim < floor and t.src != "seed" and not t.hits:
                    t.on = False            # konudan belirgin biçimde uzak ve arşivde karşılığı yok
            # Anlamsal komşular: hiçbir terime takılmayan ama anlamca konuya yakın BAŞLIKLAR bulunur,
            # sonra bu başlıklarda orantısız sık geçen sözcükler yeni terim önerisi olur.
            if corpus and embed_titles and len(corpus.titles) >= 500:
                n_sem = min(len(corpus.titles), semantic_sample)
                step("gomme", f"Son {n_sem} başlık vektörleniyor (ilk seferde uzun sürer, sonra önbellekten gelir)…")
                tv = await embed_titles(list(range(n_sem)), corpus.titles[:n_sem])
                matcher = topic.matcher()
                core_only = _core_matcher(topic)
                matched = np.array([bool(matcher.match_text(s)) for s in corpus.titles[:n_sem]])
                core_hit = np.array([bool(core_only.match_text(s)) for s in corpus.titles[:n_sem]])
                probe = semantic.reduce(await client.embed(semantic.probe_texts(topic)))
                scores = semantic.score_titles(tv, probe, tv[core_hit], matched)
                idx = semantic.pick_candidates(scores, matched, z=3.0, max_n=120)    # keşif amaçlı: eşik biraz gevşek
                neighbors = [{"t": corpus.titles[i], "sc": round(float(scores[i]), 1)} for i in idx[:40]]
                step("gomme", f"{len(idx)} anlamsal komşu başlık bulundu")
                if len(idx) >= 8:
                    sub = Corpus(corpus.titles[:n_sem])
                    for text, n, ratio, example in _dedupe_by_prefix(_overrepresented(sub, set(idx), matcher, min_n=3))[:15]:
                        suggestions.append({"t": text, "src": "anlamsal", "hits": n, "lift": round(ratio, 1), "example": example})
        except Exception as exc:  # noqa: BLE001 — gömme modeli yoksa hat yine de tamamlanmalı
            step("gomme", f"Gömme adımı atlandı ({type(exc).__name__}: {exc})")

    # ── 7. Birliktelik ───────────────────────────────────────────────────
    if corpus:
        core_only = _core_matcher(topic)
        pos = {i for i, s in enumerate(corpus.titles) if core_only.match_text(s)}
        cooc = _dedupe_by_prefix(_overrepresented(corpus, pos, topic.matcher())) if len(pos) >= 15 else []
        have = {s["t"] for s in suggestions}
        for text, n, ratio, example in cooc[:15]:
            if text not in have:
                suggestions.append({"t": text, "src": "birliktelik", "hits": n, "lift": round(ratio, 1), "example": example})
        if cooc:
            step("birliktelik", "Konu haberlerinde öne çıkanlar: " + ", ".join(c[0] for c in cooc[:8]))

    # Kapatılmış ve arşivde hiç karşılığı olmayan terimler listeyi kalabalıklaştırmasın
    topic.terms = [t for t in topic.terms if t.on or t.hits]
    topic.terms.sort(key=lambda t: (not t.on, t.kind != "core", -(t.hits or 0), -(t.sim or 0)))
    if not topic.context and any(t.on and t.kind == "weak" for t in topic.terms):
        step("uyari", "Bağlam sözcüğü yok: 'bağlam gerektirir' türündeki terimler bağlam eklenene kadar eşleşmez.")
    step("bitti", f"{sum(t.on for t in topic.terms)} etkin terim hazır")
    return {"topic": topic.to_dict(), "suggestions": suggestions, "neighbors": neighbors, "log": log}


# ---------------------------------------------------------------------------
# Derlem tabanlı yardımcılar
# ---------------------------------------------------------------------------

_CITY_TOKENS = {tok for c in CITY_NAMES for tok in content_tokens(c)}


def _core_matcher(topic: Topic):
    """Yalnızca etkin çekirdek terimlerden oluşan eşleştirici (zayıf terimler ve bağlam hariç)."""
    return Topic(slug="_", title="_", terms=[t for t in topic.terms if t.on and t.kind == "core"],
                 exclude=topic.exclude).matcher()


def _overrepresented(corpus: Corpus, pos: set[int], matcher, min_n: int = 4) -> list[tuple[str, int, float, str]]:
    """`pos` kümesindeki başlıklarda, arşivin geri kalanına göre orantısız sık geçen sözcük ve ikili öbekler.
    Dönüş: (öbek, pos içindeki belge sayısı, oran, örnek başlık) — konunun zaten yakaladıkları hariç."""
    if not pos:
        return []
    pos_df: Counter = Counter()
    neg_df: Counter = Counter()
    example: dict[str, str] = {}
    seen_titles: set[str] = set()
    n_pos = 0
    for i, title in enumerate(corpus.titles):
        key = corpus.low[i][:60]
        if key in seen_titles:              # aynı ajans haberinin kopyaları tek belge sayılır
            continue
        seen_titles.add(key)
        toks = [t for t in content_tokens(title) if len(t) >= 4 and not t.isdigit() and t not in _CITY_TOKENS]
        grams = set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:])}
        if i in pos:
            n_pos += 1
            for g in grams:
                pos_df[g] += 1
                example.setdefault(g, title)
        else:
            neg_df.update(grams)
    n_neg = max(len(seen_titles) - n_pos, 1)
    n_pos = max(n_pos, 1)
    # Mevcut terimlerin parçası olan sözcükleri ("kentsel dönüşüm" → "kentsel") öneri olarak sunma
    known = {w[:5] for t in matcher.topic.terms for w in content_tokens(t.t)}
    scored = []
    for gram, a in pos_df.items():
        if a < min_n or matcher.match_text(gram) or all(w[:5] in known for w in gram.split()):
            continue
        ratio = ((a + 0.5) / n_pos) / ((neg_df.get(gram, 0) + 0.5) / n_neg)
        if ratio >= 4:
            scored.append((math.log(ratio) * math.sqrt(a), gram, a, ratio))
    scored.sort(reverse=True)
    # Bir ikili öbek öne çıktıysa, onu oluşturan tek sözcükleri ayrıca listeleme
    out, covered = [], set()
    for _, gram, a, ratio in scored[:60]:
        if gram in covered:
            continue
        if " " in gram:
            covered.update(gram.split())
        out.append((gram, a, ratio, example[gram]))
    return out[:30]
