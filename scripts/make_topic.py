"""Komut satırından konu oluşturma: anahtar kelime → anlamsal genişletme → data/topics/<konu>.json

  python scripts/make_topic.py fon
  python scripts/make_topic.py "kentsel dönüşüm" --note "riskli yapı, rezerv alan, hak sahipliği"
  python scripts/make_topic.py deprem --no-audit --depth 1 --dry-run

Yerel dil modeli (LM Studio'nun indirdiği GGUF) uygulama tarafından doğrudan yüklenir; LM Studio'nun
açık olması gerekmez. Model seçimi için: python app/server.py → Ayarlar sekmesi, ya da
data/local/settings.json içindeki "chat_model" / "embed_model".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import semantic  # noqa: E402
from radar.expand import expand_keyword  # noqa: E402
from radar.llm.runtime import runtime  # noqa: E402
from radar.store import Store  # noqa: E402
from radar.topics import Topic, load_topic, save_topic  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keyword")
    ap.add_argument("--note", default="", help="kapsamı daraltan/genişleten açıklama")
    ap.add_argument("--depth", type=int, default=1, choices=[1, 2], help="2: alt temaları da tara (daha geniş, daha yavaş)")
    ap.add_argument("--no-embeddings", action="store_true")
    ap.add_argument("--no-audit", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="dosyaya yazma, yalnızca göster")
    ap.add_argument("--force", action="store_true", help="aynı adlı konu varsa üzerine yaz")
    args = ap.parse_args()

    store = Store()
    store.import_archive()
    rows = store.recent()
    ids, titles = [r[0] for r in rows], [r[1] for r in rows]
    print(f"Yerel arşiv: {len(titles)} başlık")

    def embed_titles(keys, texts):
        return semantic.embed_cached(store, [ids[k] for k in keys], texts)

    try:
        result = asyncio.run(expand_keyword(
            args.keyword, note=args.note, corpus_titles=titles, depth=args.depth,
            use_embeddings=not args.no_embeddings, audit=not args.no_audit, embed_titles=embed_titles,
            progress=lambda stage, detail: print(f"  [{stage}] {detail}", flush=True)))
    finally:
        runtime.stop()

    topic = Topic.from_dict(result["topic"])
    print(f"\n── {topic.title} ──\n{topic.description}\n")
    for t in topic.terms:
        mark = "●" if t.on and t.kind == "core" else "◐" if t.on else "○"
        extra = "  ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                          for k, v in (("isabet", t.hits), ("benzerlik", t.sim), ("kesinlik", t.prec)) if v is not None)
        print(f"  {mark} {t.t:<34} {t.src:<8} {extra}")
    print("\n  ● çekirdek   ◐ bağlam gerektirir   ○ kapalı")
    print("\nBağlam  :", ", ".join(topic.context))
    print("Dışlama :", ", ".join(topic.exclude))
    if result["suggestions"]:
        print("\nÖneriler (eklenmedi; uygunsa dosyaya ya da arayüzden ekleyin):")
        for s in result["suggestions"]:
            print(f"  + {s['t']:<28} {s['src']:<12} isabet={s.get('hits', 0):<5} örn: {s.get('example', '')[:70]}")
    if result.get("neighbors"):
        print("\nAnlamsal komşular (hiçbir terime takılmayan ama konuya yakın başlıklar):")
        for nb in result["neighbors"][:20]:
            print(f"  {nb['sc']:.2f}  {nb['t'][:110]}")

    if args.dry_run:
        return
    if load_topic(topic.slug) and not args.force:
        print(f"\n! data/topics/{topic.slug}.json zaten var; üzerine yazmak için --force kullanın.")
        return
    save_topic(topic)
    print(f"\nKaydedildi: data/topics/{topic.slug}.json — günlük taramaya girmesi için depoya gönderin (git push).")


if __name__ == "__main__":
    main()
