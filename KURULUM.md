# The Long Ago — Kurulum Rehberi (tek seferlik, ~1,5 saat)

Bu rehberi **bir kere** yapıyorsunuz. Sonrasında sistem aylarca kendi kendine çalışır:
konu seçer, araştırır, yazar, seslendirir, çizer, kurgular, yükler ve yayın takvimine koyar.
Hepsi **ücretsiz** servislerle çalışır.

## Sistem nasıl çalışıyor?

| Ne zaman (Türkiye saati) | Ne olur? | Sizin işiniz |
|---|---|---|
| Pazartesi 08:00 | 6 konu önerilir (GitHub'da "issue" olarak) | İsterseniz 2 tanesini işaretleyin. Yapmazsanız ertesi sabah sistem kendisi seçer. |
| Salı ve Perşembe 11:30 | Bölüm + 2 Shorts hazırlanmaya başlar (ücretsiz kota biterse ertesi gün devam eder) | Yok |
| Video hazır olunca | Telefonunuza "🎬 Onay bekliyor" bildirimi gelir | **Hiçbir şey.** 48 saat içinde `/iptal` yazmazsanız kendiliğinden onaylanır. (Doğruluk kontrolü çalışmamış bir bölüm ise otomatik onaylanmaz, `/onay` beklenir.) |
| Her gün 09:30 | Onaylanan video YouTube'a yüklenir ve zamanlanır | Yok |
| Ayın 3'ü | 2-3 saatlik uyku derlemesi hazırlanır | Yok |
| Bir hata olursa | "⚠️" bildirimi gelir, sistem bir sonraki çalışmada tekrar dener (aynı hata tekrar ederse aynı konuya yorum düşer, yeni bildirim yağmuru olmaz) | Yok (isterseniz bana iletin) |
| Her Pazar 10:15 | Haftalık sağlık kontrolü: YouTube bağlantısı, Gemini ve Cloudflare anahtarları denenir; videolar yüklenmeden birikiyorsa "📦" uyarısı gelir | Sadece uyarı gelirse |

Yayın saatleri: ana videolar **Çarşamba ve Cumartesi 17:00**, Shorts **Salı, Cuma, Pazar 15:00**.

Hangi servis ne yapıyor:
- **Senaryo:** Gemini (ücretsiz katman). Model adı değişirse sistem yenisini kendisi bulur.
- **Ses:** Kokoro (açık kaynak, ücretsiz). GitHub'ın bilgisayarında çalışır.
- **Görseller:** Cloudflare FLUX.2 (günlük ücretsiz kota). Her görseli Gemini kontrol eder, bozuk olanı yeniden çizdirir.
- **Kurgu:** ffmpeg. Yağlı boya tarzı resimler, yavaş kamera hareketi, yumuşak geçişler, film dokusu ve bölüm başlıkları.

---

## Adım 1 — YouTube kanalı (10 dk)
1. Bilgisayardan https://www.youtube.com adresine kanalın Gmail hesabıyla girin → profil resmi → **Kanal oluştur**.
2. Ad: **The Long Ago**, kullanıcı adı **@TheLongAgo2** ✅ (tamamlandı)
3. https://www.youtube.com/verify adresinden **telefon doğrulaması** yapın (kapak resmi yüklemek için şart).

## Adım 2 — GitHub deposu (5 dk)
1. https://github.com/new → Repository name: **the-long-ago** → **Private** → Create.
2. Telefona **GitHub** uygulamasını kurun, bildirimleri açın.
3. Bana "depo hazır" yazın; kodu ben yüklerim.

## Adım 3 — Gemini anahtarı (3 dk, ücretsiz)
1. https://aistudio.google.com/apikey → **Create API key** → kopyalayın.
2. Faturalandırma (billing) **eklemeyin**; ücretsiz katman yeterli.

## Adım 4 — Cloudflare (10 dk, ücretsiz)
1. https://dash.cloudflare.com/sign-up adresinden ücretsiz hesap açın.
2. Giriş yapınca sağ tarafta veya **Account Home** sayfasında yazan **Account ID**'yi kopyalayın.
3. Sağ üst profil → **My Profile → API Tokens → Create Token** → **"Workers AI"** şablonunu seçin
   (yoksa Custom token: *Account → Workers AI → Read* ve *Edit*). Süre sınırı koymayın → oluşturup kopyalayın.

## Adım 5 — YouTube'a otomatik yükleme izni (30 dk)
1. https://console.cloud.google.com → üstten yeni proje: **the-long-ago**.
2. **APIs & Services → Library** → "YouTube Data API v3" → **Enable**.
3. **OAuth consent screen** → External → uygulama adı "The Long Ago Uploader" → e-postanız → kaydedin.
   Sonra **Publish app → In production** (bunu yapmazsanız bağlantı 7 günde bir kopar).
4. **Credentials → Create credentials → OAuth client ID** → **Web application** →
   Authorized redirect URI: `https://developers.google.com/oauthplayground` → Client ID ve Client Secret'ı kopyalayın.
5. https://developers.google.com/oauthplayground → sağ üst ⚙️ → **Use your own OAuth credentials** → ID ve Secret'ı girin.
   Soldaki kutuya şunları yazın: `https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl`
   → **Authorize APIs** → kanalın hesabını seçin ("doğrulanmadı" uyarısında **Gelişmiş → Devam et**)
   → **Exchange authorization code for tokens** → **Refresh token**'ı kopyalayın.
6. **Denetim başvurusu:** https://support.google.com/youtube/contact/yt_api_form
   Google, API ile yüklenen videoları bu denetimden geçene kadar gizli tutar. Formu birlikte dolduralım;
   bana "forma geldim" yazın, cevapları hazırlayıp vereyim. Onay e-postası birkaç hafta içinde gelir.

## Adım 6 — Anahtarları GitHub'a koyun (10 dk)
Depo → **Settings → Secrets and variables → Actions → New repository secret**. İsimler birebir aynı olmalı:

| Secret adı | Nereden |
|---|---|
| `GEMINI_API_KEY` | Adım 3 |
| `CLOUDFLARE_ACCOUNT_ID` | Adım 4.2 |
| `CLOUDFLARE_API_TOKEN` | Adım 4.3 |
| `YT_CLIENT_ID` | Adım 5.4 |
| `YT_CLIENT_SECRET` | Adım 5.4 |
| `YT_REFRESH_TOKEN` | Adım 5.5 |

Anahtarları sohbete yapıştırmayın, sadece GitHub'a girin.

## Adım 7 — Kontrol (5 dk)
Depo → **Actions** → **"0 - Ayarları kontrol et"** → **Run workflow**. Yeşil tik = her şey hazır.
Kırmızı çıkarsa bağlantısını bana gönderin.

## Adım 8 — Google onay e-postası gelince (1 dk, telefondan)
Depo → **Settings → Secrets and variables → Actions → Variables** sekmesi → **New repository variable**:
ad `UPLOAD_MODE`, değer `api`. O andan itibaren birikmiş videolar her gün birer birer yüklenip zamanlanır.

---

## Sizin yapmanız gereken başka bir şey kalıyor mu?
- **Para kazanma başvurusu:** Kanal eşiği geçince YouTube size e-posta atar. Başvuru ve AdSense hesabı kimlik bilgisi
  istediği için sizin yapmanız gerekir (yaklaşık 15 dk). 2027-02-01'den sonra açılan kanallarda eşik iki katına çıkıyor.
- **İsteğe bağlı kalite artışı:** Google AI Pro aboneliğiniz ayda 10 $ API kredisi veriyor. Bu kredi, 5 $'lık bir
  bakiye yüklenince açılıyor. Açarsanız kapak resimleri daha kaliteli Nano Banana ile çizilir. Zorunlu değil.

## Dürüst riskler
- Ücretsiz kotalar Google veya Cloudflare tarafından düşürülebilir. Sistem yedek modele geçer, olmazsa o gün atlar.
- 9 ay kimse izlemezse küçük hatalar videoda kalabilir. Doğruluk kontrolü ve görsel kontrolü bu riski azaltır ama sıfırlamaz.
- YouTube "inauthentic content" kuralı yüzünden para kazanma başvurusu reddedilebilir. Her bölüm farklı yapıda yazılıyor,
  kaynaklar açıklamada veriliyor ve yapay zekâ etiketi açık. Bu riski azaltmak için elimizden geleni yapıyoruz.
