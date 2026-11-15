# Yerel Medya Radarı

Türkiye'nin 81 ilindeki **1.270+ yerel haber sitesini** tarar; seçtiğiniz konuların yerel basındaki
seyrini il il, gün gün izler.

- **Anahtar kelime → konu:** "fon" yazarsınız; bilgisayarınızdaki dil modeli onu ilgili terimlere
  genişletir, terimleri gerçek başlıklarla sınar, siz son hâlini verirsiniz.
- **İl haritası ve eğilim:** haber sayısı, tekil hikâye sayısı ve "bin başlıkta kaç" ölçüsüyle
  zaman serisi, il haritası, terim ve kaynak dökümü, CSV ve RSS.
- **LM Studio açık olmadan yerel model:** uygulama, LM Studio'nun indirdiği GGUF modelleri ve
  LM Studio'nun diskteki llama.cpp çalışma zamanını doğrudan kullanır. Modeli siz seçersiniz.
- **Bilgisayar kapalıyken günlük tarama:** GitHub Actions her gün tarar, GitHub Pages panoyu
  yayınlar. Herkese açık depoda ikisi de ücretsizdir.

```
                         ┌──────────────── bu bilgisayar ────────────────┐
 anahtar kelime ──────►  │ yerel dil modeli (GGUF)  →  konu tanımı (JSON) │ ──── git push ───┐
                         │ gömme modeli → anlamsal komşular                │                  │
                         └─────────────────────────────────────────────────┘                  ▼
                         ┌──────────────── GitHub (her gün) ───────────────────────────────────┐
 1.270+ yerel site ───►  │ tara → yeni başlıkları ayır → konu kalıplarıyla eşleştir → pano      │
                         │ başlık arşivi → Releases      eşleşmeler → depo      pano → Pages     │
                         └──────────────────────────────────────────────────────────────────────┘
```

Pahalı iş (anlamsal genişletme) bir kez, sizin bilgisayarınızda yapılır. Günlük tarama yalnızca
üretilen kalıpları kullanır; bulutta büyük dil modeli gerekmez.

## Hızlı başlangıç (yerel)

```bash
pip install -r requirements.txt
python app/server.py          # ya da Windows'ta start.bat
```

Tarayıcıda `http://localhost:8000`:

1. **Kapsam → Şimdi tara** — tüm kaynakları tarar (yaklaşık 1 dakika), yerel arşivi doldurur.
2. **Keşfet** — anahtar kelimeyi yazın, **Genişlet**. Taslak hazır olunca terimleri düzenleyin,
   **Önizle** ile haritayı görün, **Kaydet ve izlemeye al**.
3. **Ayarlar** — sohbet ve gömme modelini seçin; konuları GitHub'a gönderin.

Komut satırından da yapılabilir: `python scripts/make_topic.py "kentsel dönüşüm"`.

## Yerel model: LM Studio'ya bağımlı değil

Uygulama LM Studio'nun **model klasörünü** okur (`~/.lmstudio-home-pointer` →
`settings.json` → `downloadsFolder`), GGUF başlıklarından modelleri tanır (sohbet / gömme) ve
seçtiğiniz modeli `llama-server` ile kendisi başlatır. `llama-server` şu sırayla aranır:

1. Ayarlarda ya da `LLAMA_SERVER` ortam değişkeninde belirttiğiniz yol
2. `runtime/llama.cpp/` (Ayarlar → "Güncel llama.cpp indir")
3. LM Studio'nun diskte tuttuğu kendi llama.cpp motoru (`extensions/backends/…/llama-server`)
4. `PATH`

Yani LM Studio kuruluysa hiçbir şey indirmeden çalışır; LM Studio'nun **açık olması gerekmez**.
LM Studio hiç kurulu değilse GGUF dosyalarını `runtime/models/` içine koyun ya da Ayarlar'dan
klasör ekleyin (`data/local/settings.json` → `extra_model_dirs`).

- Model kullanılmadığında 10 dakika sonra bellekten atılır (ayarlanabilir).
- Ekran kartı belleği iki modele yetmiyorsa sohbet ve gömme modeli sırayla yüklenir.
- Terim üretiminde **büyük modeller belirgin biçimde daha isabetli**dir (27B+ önerilir); küçük
  modeller hızlıdır ama örnekleri kopyalamaya ve kurum adı uydurmaya yatkındır.
- İsterseniz hazır bir OpenAI uyumlu sunucu da kullanabilirsiniz (Ayarlar → Harici sunucu).

## Anlamsal genişletme nasıl çalışır?

`radar/expand.py` — tek anahtar kelimeden izlenebilir bir konu tanımı:

| Adım | Ne yapar |
|---|---|
| 1. LLM genişletme | Arşivde kelimenin geçtiği gerçek başlıklar modele gösterilir; model eş anlamlılar, ilişkili ifadeler, kurumlar, belirsiz sözcükler, bağlam ve dışlama önerir |
| 2. Temizlik | Daha kısa bir terimin zaten yakaladığı tamlamalar atılır; arşivde hiç görülmeyen terimler ve dışlamalar modele ikinci kez sorgulatılır |
| 3. Kalıp üretimi | Her terim Türkçe çekim eklerine duyarlı düzenli ifadeye çevrilir |
| 4. Derlem sınaması | Her terimin arşivde kaç başlık yakaladığı ölçülür |
| 5. LLM denetimi | Her terimin yakaladığı örnek başlıklar "gerçekten bu konuyla mı ilgili?" diye sorulur. Kesinliği yüksek terim **çekirdek** olur, düşük olan **bağlam ister** ya da kapatılır |
| 6. Gömme analizi | Hiçbir terime takılmayan ama anlamca yakın başlıklar (anlamsal komşular) bulunur; onlardan yeni terim önerileri çıkar |
| 7. Birliktelik | Konu haberlerinde orantısız sık geçen sözcükler önerilir (model gerektirmez) |

Yalnızca anahtar kelime ve eş anlamlıları çekirdek başlar; diğer her şey arşivdeki kanıtla terfi
eder. Bu, konunun "ilgili ama farklı" alanlara kaymasını önler. Son söz her zaman sizdedir.

### Terim sözdizimi (`data/topics/*.json`)

| Yazım | Eşleşir | Eşleşmez |
|---|---|---|
| `fon` | fon, fonu, fonun, fona, fondan, fonları, Fon'a | telefon, fonksiyon, senfoni |
| `hibe desteği` | hibe desteğinden, hibe desteğiyle | — |
| `fonla*` | fonlama, fonlanan, fonlayacak | — |
| `"kredi notu"` | kredi notu (tam ifade) | kredi notunu |
| `KOSGEB` | KOSGEB, KOSGEB'den (harf duyarlı) | kosgeb |
| `re:\bipard\b` | ham düzenli ifade | — |

- **Çekirdek** (`"kind": "core"`): tek başına geçmesi yeterli.
- **Bağlam gerektirir** (`"kind": "weak"`): aynı metinde `context` listesinden bir sözcük de geçmeli.
- **Dışlama** (`exclude`): kalıp metinden maskelenir ("fon müziği" → "fon" sayılmaz); haber başka bir
  terimle hâlâ eşleşebilir.
- Son yumuşaması (destek → desteği), ünlü düşmesi (vakıf → vakfı) ve Türkçe karaktersiz yazım
  (teşvik → tesvik) kendiliğinden kapsanır. Testler: `python tests/test_topics.py`.

## GitHub'da yayınlama (bilgisayar kapalıyken günlük tarama)

Bir kez yapılır. Depo **herkese açık** olmalıdır (ücretsiz Pages ve sınırsız Actions için).

```bash
cd yerel-medya-radari
git init -b main && git add -A && git commit -m "Yerel Medya Radarı"
gh repo create yerel-medya-radari --public --source=. --push
gh api -X POST repos/{owner}/{repo}/pages -f build_type=workflow      # Pages'i Actions ile yayınla
gh workflow run "Günlük tarama"                                        # ilk taramayı başlat
```

Sonrasında iş akışı (`.github/workflows/daily-scan.yml`) her gün 06:20 ve 18:20'de (TSİ):

1. Son 21 günün başlık arşivini indirir (hangi başlıkların "yeni" olduğunu bilmek için),
2. tüm kaynakları tarar, yeni başlıkları günün arşiv dosyasına yazar ve aylık **Release**'e yükler,
3. her konu için eşleşmeleri `data/matches/` altına, günlük sayıları `data/stats/` altına işler,
4. küçük bir gömme modeliyle (CPU) **anlamsal adayları** arar — kalıplara takılmayan ama bilinen
   konu haberlerine çok benzeyen başlıklar; sayımlara katılmaz, ayrı listelenir,
5. panoyu **GitHub Pages**'te yayınlar: `https://<kullanıcı>.github.io/<depo>/`.

Yeni konu eklemek ya da terimleri değiştirmek: yerel uygulamada düzenleyin → Ayarlar →
**Konuları GitHub'a gönder** (ya da `git add data/topics && git commit && git push`). Değişen konu,
eldeki 21 günlük arşivle kendiliğinden yeniden eşleştirilir. Tüm geçmişi doldurmak için Actions
sekmesinden iş akışını **backfill** seçeneğiyle elle çalıştırın.

Buluttaki arşivi yerel analizlere katmak için: Ayarlar → **Buluttaki arşivi indir**.

### Bilinmesi gerekenler

- GitHub zamanlanmış işleri yoğun saatlerde gecikebilir; günde iki tarama bunu telafi eder.
- GitHub sunucuları yurt dışındadır; bazı siteler yurt dışı veri merkezi adreslerini engelleyebilir.
  Her çalıştırmanın özetinde kaç kaynağın yanıt verdiği ve yanıt vermeyenlerin listesi yer alır.
- Depoda 60 gün hiç etkinlik olmazsa GitHub zamanlanmış işleri durdurur; günlük tarama kendi
  sonuçlarını depoya işlediği için pratikte bu süre dolmaz.
- Anlamsal aday adımı isteğe bağlıdır; `data/config.json` → `{"semantic": {"enabled": false}}` ile
  kapatılabilir. Başarısız olursa tarama kalıp eşleştirmeyle devam eder.

## Ölçüler ve yöntem notları

- **Haber sayısı:** o gün ilk kez görülen ve konuyla eşleşen başlıklar. Başlık + RSS özeti taranır;
  yalnızca özette geçenler listede "özette" diye işaretlenir.
- **Tekil hikâye:** aynı ajans haberinin onlarca sitedeki kopyaları tek sayılır (başlık benzerliği).
- **Bin başlıkta (‰):** taranan her 1.000 yeni başlıktan kaçı konuyla ilgili. İller arasında kaynak
  sayısı çok farklıdır (Bursa 117, bazı iller 1-2); bu ölçü farkı dengeler. 50'den az başlık
  taranan il/aralık "yetersiz veri" olarak gösterilir.
- **Sıçrama:** günlük sayı, önceki 28 günün ortalamasının 3 standart sapma üstündeyse işaretlenir.
- **Taban kayıtlar:** yayın tarihi vermeyen bir site ilk kez tarandığında sayfasındaki eski
  haberler "bugün" sayılmaz; arşive girer ama sayımlara katılmaz.
- İlk tarama günü, RSS'lerdeki son birkaç günün birikimini içerdiğinden sonraki günlerden yüksek
  görünür.

## Kaynak listesi

`data/sources.json` üç yerden derlenir ve her aday için RSS beslemesi keşfedilir:

1. **BİK** (Basın İlan Kurumu) aylık süreli yayınlar listesindeki internet haber siteleri — il il,
   resmî ve her ay güncellenen liste,
2. `data/seed_sources.csv` — önceki sürümün il başına 3 sitelik çekirdek listesi,
3. `data/extra_sources.csv` — sizin eklediğiniz siteler (`il,alan_adi`).

```bash
python scripts/update_sources.py            # BİK'i indir, yeni/başarısız adayları dene
python scripts/update_sources.py --recheck  # tüm kaynakları yeniden doğrula
```

Ülke geneline yayın yapan siteler `ulusal` olarak işaretlenir ve taranmaz.

## Klasör yapısı

```
radar/            çekirdek paket
  scraper.py        RSS/Atom keşfi ve okuma; RSS yoksa ana sayfa başlıkları; site içi arama
  topics.py         konu tanımı + Türkçe çekime duyarlı eşleştirici
  expand.py         anlamsal genişletme hattı
  semantic.py       gömme tabanlı anlamsal adaylar
  pipeline.py       günlük tarama hattı (dosya tabanlı; Actions bunu çalıştırır)
  report.py         pano verisi: seriler, il dağılımı, hikâye grupları, sıçramalar, RSS
  archive.py        günlük arşiv, eşleşmeler, istatistikler, Release eşitleme
  store.py          yerel SQLite arşiv + gömme önbelleği
  digest.py         Gündem: önemli yerel haberleri kümele + LLM ile seç
  llm/              GGUF okuyucu, model keşfi, llama-server yöneticisi, istemci
app/server.py     yerel uygulama (FastAPI)
site/             pano (statik; Pages'te ve yerelde aynı kod)
scripts/          daily_scan · make_topic · update_sources · build_map
data/
  sources.json      kaynak listesi
  topics/           konu tanımları            ← elle düzenlenebilir
  matches/          konu eşleşmeleri          ← Actions yazar
  stats/            günlük taranan başlık sayıları
  archive/          günlük başlık arşivi      (depoya girmez; Releases'ta)
  local/            SQLite, ayarlar           (depoya girmez)
```

## Diğer özellikler

- **Derin arama** (Keşfet): çekirdek terimleri sitelerin kendi arama sayfalarında arar; geçmişe
  dönük sonuç verir. Çalışan arama kalıbı site başına bir kez keşfedilip hatırlanır.
- **Gündem:** taranan başlıkları kümeler; yerel model, ulusal medyaya taşınmaya değer yerel
  haberleri seçip özetler (önceki sürümün ana özelliği).
- **Konu başına RSS:** `data/feeds/<konu>.xml` — yeni eşleşmeleri RSS okuyucunuzdan izleyin.
- **CSV dışa aktarma**, açık/koyu tema, klavye ile gezilebilir grafikler ve tablo görünümleri.

## Geliştirme fikirleri

- Sıçrama olduğunda GitHub Issue / e-posta / Telegram bildirimi
- Konu karşılaştırma (iki konuyu aynı ölçekte, küçük çoklu grafiklerle)
- Haftalık yapay zekâ özeti: "bu hafta yerel basında <konu>" (yerelde üretilip depoya gönderilir)
- Varlık çıkarımı: konu haberlerinde öne çıkan kurum ve kişiler
- Ton / çerçeve analizi (destekleyici – eleştirel – nötr)
- İlçe düzeyinde konumlandırma (başlıktaki yer adlarından)
- BİK listesindeki basılı gazetelerin e-gazete sayfaları

## Teşekkür ve lisans notları

- İl sınırları: [alpers/Turkey-Maps-GeoJSON](https://github.com/alpers/Turkey-Maps-GeoJSON) (Apache-2.0),
  `scripts/build_map.py` ile sadeleştirilmiştir.
- Kaynak listesi: Basın İlan Kurumu'nun herkese açık aylık listesi.
- Haber başlıkları ilgili yayın kuruluşlarına aittir; pano yalnızca başlık, kısa özet ve bağlantı gösterir.
