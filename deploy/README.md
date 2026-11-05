# Müşteri hesabına kurulum

Her müşteri için aynı adımlar: kendi AWS hesabı, kendi alan adı, tek sunucu. Kullanıcılar `https://alan-adi` adresinden ortak şifreyle girer ve aynı kuyruğu görür.

```
kullanıcı ─▶ studio.marka.com ─▶ CloudFront (HTTPS) ─▶ EC2 (Docker, 50 GB disk) ─▶ Bedrock
                                       │ sunucu uyuyorsa
                                       └─▶ "Uygulama açılıyor" sayfası (Lambda) ─▶ sunucuyu başlatır
```

## Uyku modu

Sunucu boşta para yakmasın diye 30 dakika kullanılmazsa ve çalışan iş yoksa kendini kapatır. Sonraki ziyaretçi "Uygulama açılıyor" sayfasını görür; sunucu yaklaşık 1–2 dakikada açılır ve sayfa kendiliğinden yenilenir. Videolar, geçmiş, adres ve şifre aynı kalır. Süreyi değiştirmek için imaja `IDLE_MINUTES` ortam değişkeni verilir.

Sunucuya yalnızca CloudFront ulaşabilir; SSH portu yoktur, yönetim SSM üzerindendir. Hiçbir AWS anahtarı taşınmaz; Bedrock erişimi sunucunun IAM rolünden gelir. Giriş şifresi Secrets Manager'da oluşur.

## Gerekenler

Kurulumu yapan bilgisayarda AWS CLI v2 ve Python 3, müşteri hesabına yönetici yetkisiyle bir AWS profili. Docker gerekmez; imaj sunucuda derlenir.

Müşteri hesabında Bedrock'ta Claude modellerinin açık olması gerekir. Varsayılan model `global.anthropic.claude-sonnet-4-6`'dır. Modeli bir kez konsolda deneyip kullanım formunu doldurun.

## 1. Sertifika

```bash
AWS_PROFILE=musteri deploy/deploy.sh cert studio.marka.com
```

Çıkan **CNAME** kaydını müşteriye verin; DNS'i neredeyse oraya eklesin (Cloudflare'de proxy kapalı olmalı). Eklendikten sonra aynı komutu çalıştırın, durum `ISSUED` olduğunda devam edin.

## 2. Kurulum

```bash
AWS_PROFILE=musteri deploy/deploy.sh up studio.marka.com
```

20–30 dakika sürer. Sunucunun bölgesi için başa `AWS_REGION=eu-central-1` ekleyin; varsayılanı `us-east-1`'dir. Sertifika her durumda `us-east-1`'de oluşur, CloudFront bunu ister.

## 3. Müşteriye verilecek kayıt

Komutun sonunda ya da istediğiniz zaman:

```bash
AWS_PROFILE=musteri deploy/deploy.sh info studio.marka.com
```

| Tür | Ad | Değer |
| --- | --- | --- |
| CNAME | `studio.marka.com` | `d123abc.cloudfront.net` |

CloudFront'un sabit IP'si olmadığı için A kaydı kullanılamaz. Kök alan adı (`marka.com`) için CNAME yerine DNS sağlayıcısının ALIAS/ANAME ya da CNAME flattening özelliği gerekir; en kolayı alt alan adıdır. Route 53'te A kaydı Alias olarak CloudFront'a bağlanabilir.

Aynı çıktıda giriş şifresini okuyan komut da yazar.

## Güncelleme

Bilgisayarınızdaki kodu gönderir ve imajı yeniden derler. HyperFrames ve yt-dlp o an en güncel sürüme geçer.

```bash
AWS_PROFILE=musteri deploy/deploy.sh update studio.marka.com
```

Kuyrukta çalışan iş varsa kesilir ve hata olarak görünür; videolar ve geçmiş `/data` altında korunur. Müşterileri tek tek güncellersiniz, böylece her kurulum siz güncelleyene kadar aynı sürümde kalır.

## Tahmini aylık maliyet (us-east-1)

| Kalem | Yaklaşık |
| --- | --- |
| EC2 `c7i.xlarge` (4 vCPU, 8 GB), saatte $0.18, yalnızca açıkken | Ayda 40 saatte $7 |
| 50 GB gp3 disk | $4 |
| Sabit IP | $4 |
| Lambda, CloudFront, S3, Secrets Manager | Küçük kullanımda birkaç dolar altı, video trafiğine göre artar |
| Bedrock | Video başına yaklaşık $1 |

Toplam ayda 40 saat kullanımda yaklaşık $15, günde 8 saat kullanımda yaklaşık $40. Fiyatları kurulumdan önce müşterinin bölgesi için kontrol edin.

## Sınırlar

- Tek sunucudur: bakımda ya da arızada hizmet durur, aynı anda bir video üretilir. Uyuyorken ilk ziyaretçi 1–2 dakika bekler.
- Yedek otomatik değildir. Sunucu silinse de disk kalır, ama düzenli yedek için EBS snapshot planı kurun.
- CloudFront ile sunucu arasındaki bağlantı HTTP'dir; kullanıcı ile CloudFront arası HTTPS'tir.
- Herkes tek şifre kullanır, kişiye özel hesap yoktur. Şifreyi değiştirmek için Secrets Manager'daki değeri güncelleyip `update` çalıştırın.
- `up` var olan kurulumu değiştirmez. Şablonu değiştirirseniz güncellemeyi elle yapın; sunucu imajının değişmesi sunucuyu yeniden yaratabilir.
