"""Eşleştirici testleri:  python -m pytest tests  (ya da doğrudan: python tests/test_topics.py)"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.topics import Term, Topic  # noqa: E402


def _topic(**kw) -> Topic:
    base = dict(slug="fon", title="Fon", terms=[Term("fon")])
    base.update(kw)
    return Topic(**base)


def hit(topic: Topic, text: str) -> bool:
    return bool(topic.matcher().match_text(text))


def test_inflections_match():
    t = _topic()
    for s in ["Belediyeye AB fonu", "Fonun yüzde 40'ı kullanıldı", "Projeye fondan kaynak", "FONLAR açıklandı",
              "Kalkınma fonlarından 3 milyon", "fona başvurular başladı", "Fon'a ilgi büyük", "afet fonuyla onarıldı",
              "Yatırım Fonu kuruldu", "İŞ BİRLİĞİ FONU"]:
        assert hit(t, s), s


def test_lookalikes_do_not_match():
    t = _topic()
    for s in ["Telefon dolandırıcıları yakalandı", "Mikrofon arızası", "Fonksiyonel tıp semineri",
              "Senfoni orkestrası konseri", "Fonetik alfabe", "Saksafon dinletisi", "fondöten satışları"]:
        assert not hit(t, s), s


def test_exclusion_masks_only_the_phrase():
    t = _topic(exclude=["fon müziği", "arka fon"])
    assert not hit(t, "Törende fon müziği çalındı")
    assert not hit(t, "Arka fonda Erciyes manzarası")
    assert hit(t, "Fon müziği eşliğinde hibe fonu tanıtıldı")


def test_weak_terms_need_context():
    t = _topic(terms=[Term("destek", kind="weak"), Term("kaynak", kind="weak")], context=["milyon", "hibe"])
    assert not hit(t, "Taraftardan takıma destek")
    assert hit(t, "Çiftçiye 5 milyon lira destek")
    assert not hit(t, "Projeye destek için kaynak aranıyor")  # iki zayıf terim birbirinin bağlamı sayılmaz
    t2 = _topic(terms=[Term("destek", kind="weak")], context=["destek", "hibe"])
    assert not hit(t2, "Taraftardan takıma destek")           # terim kendi kendisinin bağlamı olamaz
    t3 = _topic(terms=[Term("sermaye piyasası", kind="weak")], context=["sermaye", "piyasa", "borsa"])
    assert not hit(t3, "Sermaye Piyasası Kurulu toplandı")    # bağlam terimin kendi sözcüklerinden gelemez
    assert hit(t3, "Sermaye piyasasında borsa rekoru")


def test_softening_vowel_drop_and_ascii():
    t = _topic(terms=[Term("destek"), Term("vakıf"), Term("teşvik")])
    assert hit(t, "Üreticiye desteği artırdı")
    assert hit(t, "Vakfın bursları açıklandı")
    assert hit(t, "Yatirim tesvikleri aciklandi")              # Türkçe karaktersiz yazım
    assert not hit(t, "Destekleme alımları")                   # farklı türetim: önek isteniyorsa destekle*


def test_phrase_prefix_exact_acronym_regex():
    t = _topic(terms=[Term("hibe desteği"), Term("fonla*"), Term('"kredi notu"'), Term("KOSGEB"), Term(r"re:\bipard(?:-\w+)?\b")])
    assert hit(t, "Hibe  desteğinden yararlanan çiftçiler")
    assert hit(t, "Proje AB tarafından fonlanacak")
    assert hit(t, "Kredi notu yükseldi") and not hit(t, "Kredi notunu yükseltti")
    assert hit(t, "KOSGEB'den esnafa destek") and not hit(t, "kosgeb yazımı küçük")
    assert hit(t, "IPARD-III başvuruları")


def test_summary_only_match_is_flagged():
    t = _topic()
    m = t.matcher()
    assert m.match("Belediye meclisi toplandı", "Toplantıda AB fonu görüşüldü") == (["fon"], "s")
    assert m.match("AB fonu geldi", "") == (["fon"], "t")
    assert m.match("Belediye meclisi toplandı", "Gündem yoğundu") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"{len(fns)} test geçti")
