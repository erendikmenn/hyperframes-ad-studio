# HyperFrames Ad Studio

Beğendiğin bir reklam filmini seç ya da linkini yapıştır, brief'ini yaz: aynı kurgu ve ritimde kendi reklam filmin MP4 olarak çıksın. Claude filmi planlar ve sahneleri kod olarak yazar, [HyperFrames](https://github.com/heygen-com/hyperframes) HTML'i kare kare videoya çevirir.

*Pick an ad you like or paste its link, write a brief, and get your own MP4 with the same edit and rhythm. Claude plans the film and writes each scene as code; HyperFrames renders the HTML to video. The UI is in Turkish.*

> Bu bir POC'dir. Müşterinin AWS hesabına kendi alan adıyla kurulum için [deploy/README.md](deploy/README.md) dosyasına bakın. Yerelde çalıştırırken şifre yoktur; `app.py`'yi şifresiz internete açmayın.

## Neler yapıyor

- **Referans galerisi:** HyperFrames showcase'inden 30 film, üstüne gelince oynar.
- **Kendi referansın:** YouTube, TikTok, Instagram, X linki, doğrudan video linki ya da dosya yükleme (en fazla 5 dakika, 300 MB).
- **Kurgu analizi:** Kesimler `hyperframes measure`, ses yapısı `hyperframes audio` ile ölçülür; Claude kareleri inceleyip filmi bölümlere ayırır. Sonuç her referans için önbelleğe alınır.
- **Remix:** Referansın bölüm sırası, yerleşimi, yazı dili ve görünümü korunur; içerik brief'ten gelir.
- **Olgu kontrolü:** Brief'te olmayan iddialar ekran metninden çıkarılır.
- **Kalite döngüsü:** `hyperframes check`, sahne bazında onarım ve render edilmiş kareler üzerinden görsel review.
- **Maliyet görünürlüğü:** Her adımın token, süre ve Bedrock maliyeti sonuç sayfasında görünür.

## Nasıl çalışır

```
referans ─▶ ölçüm + analiz     kesimler, ses yapısı, kareler → bölümler, görünüm, imza hareketler (önbellekli)
brief    ─▶ 1 plan             tek fikir, görünüm, sahne başına blueprint ve referans bölümü
         ─▶ 2 olgu kontrolü    brief'te olmayan iddialar çıkarılır
         ─▶ 3 müzik            HeyGen kataloğu (giriş yapıldıysa, isteğe bağlı)
         ─▶ 4 zamanlama        sahne süreleri, geçişler, referansın ses vuruşları
         ─▶ 5 sahneler         paralel; her sahneye blueprint, örnek kod ve referans karesi verilir
         ─▶ 6 montaj           renk ve font token'ları, geçişler, markalı kapanış kartı
         ─▶ 7 kalite           check → onarım → görsel review → onarım
         ─▶ 8 render           HyperFrames ile MP4
```

Referansın görüntüsü, sesi ve logosu yeni filme girmez; yalnızca kurgusu tarif olarak kullanılır. Bu yüzden sonuç birebir kopya değil, aynı tarzda yeni bir filmdir. Gerçek çekimler ve 3D sahneler HTML/CSS ile yaklaşık olarak kurulur.

## Kurulum

Gereksinimler: Python 3.12+, Node 20+, ffmpeg, [yt-dlp](https://github.com/yt-dlp/yt-dlp) ve Claude modellerine erişimi olan bir AWS hesabı.

```bash
brew install ffmpeg yt-dlp
```

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
AWS_PROFILE=profiliniz AWS_REGION=us-east-1 .venv/bin/python app.py
```

Uygulama http://localhost:8080 adresinde açılır. Komut satırından tek video:

```bash
.venv/bin/python pipeline.py "brief metni" --brand Marka --colors "#111111,#FFFFFF,#E33333" --format 9:16 --duration 20 --reference jake-lovable
```

## Modeller ve maliyet

Varsayılan model Amazon Bedrock'ta Claude Sonnet 4.6'dır. Fiyatlar ve model listesi `pricing.py` dosyasındadır.

| Yol | Ölçülen sonuç |
| --- | --- |
| Bedrock, Sonnet 4.6, 14–20 sn video | Video başına $0.47–1.30; render tahmini yaklaşık $0.001 |
| Yerel test, Sonnet 5.5, 45 sn video | Yaklaşık 5 dakika, referansa en yakın sonuç |
| Yerel test, Haiku 5.5, 45 sn video | Yaklaşık 3 dakika, hızlı taslak kalitesi |

Yerel test modelleri (`sub-sonnet-5.5`, `sub-haiku-5.5`) oturum açılmış `claude` CLI'ını kullanır ve API'de `"model"` alanıyla seçilir. Bu yol kişisel test içindir; çok kullanıcılı bir servis Bedrock üzerinde çalışmalıdır. Bedrock'ta eşzamanlı üretim 429 hatası verebildiği için uygulama işleri sırayla çalıştırır.

## Dosyalar

| Yol | Görev |
| --- | --- |
| `app.py`, `static/index.html` | Flask API, iş kuyruğu ve arayüz |
| `pipeline.py` | Üretim hattı, Bedrock ve CLI istemcileri |
| `reference.py` | Link/dosya alma, ölçüm, kurgu analizi |
| `pricing.py` | Model fiyatları, render tahmini |
| `prompts/` | Planlama ve sahne yazım kuralları |
| `library/blueprints/`, `library/TRANSITION-REGISTRY.md` | HyperFrames sahne kalıpları ve geçişleri |
| `library/blocks/endcard.html` | Markalı kapanış kartı |
| `library/showcase.json` | Referans galerisi (yalnızca linkler) |

`runs/` ve `library/references/` çalışırken oluşur ve repoya dahil değildir. `Dockerfile` ve `deploy/` AWS kurulumunu içerir.

## Bilinen sınırlar

- Yeni videolarda ses efekti yoktur; müzik yalnızca HeyGen girişi yapılmışsa eklenir, aksi halde video sessizdir.
- Bir film en fazla 6 sahne ve kapanış kartıdır, bu yüzden çok hızlı kesimli referanslar sadeleşir.
- Kullanıcı hesabı yoktur, kurulum başına tek ortak şifre vardır. Marka kiti, onay akışı ve kota da yoktur; eklenen referanslar herkese görünür ve arayüzden silinemez.
- Instagram ve X bazı videolar için giriş isteyebilir; bu durumda dosya yüklemek gerekir.
- YouTube, TikTok ve Instagram'dan indirme uçtan uca denenmedi; X ve doğrudan dosya linkleri denendi.

## AWS kurulumu

Her müşteri kendi AWS hesabında kendi alan adıyla çalışır: alan adı → CloudFront → tek EC2 sunucu (Docker). Bedrock erişimi sunucunun IAM rolünden gelir. Sunucu boşta kendini kapatır, ilk ziyaretçi onu uyandırır; böylece aylık sabit maliyet yaklaşık $15 olur (ayda 40 saat kullanımla, Bedrock hariç). Adımlar [deploy/README.md](deploy/README.md) dosyasındadır. Bu kurulum henüz gerçek bir AWS hesabında uçtan uca denenmedi.

## Telif ve lisans

Referans olarak kullandığınız videoların ve ürettiğiniz içeriğin haklarından siz sorumlusunuz. `library/showcase.json` yalnızca HyperFrames sitesindeki videolara link verir; videolar sahiplerine aittir ve bu repoda bulunmaz.

Kod [Apache License 2.0](LICENSE) ile lisanslanmıştır. HyperFrames'ten türetilen dosyalar için [NOTICE](NOTICE) dosyasına bakın. Bu proje HeyGen ile bağlantılı değildir.
