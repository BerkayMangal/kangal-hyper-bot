# Kangal Hyper Bot

Hyperliquid'de **funding carry** yapan bot.

## Ne yapıyor?

Her coin için iki bacak açar:

- **Spot'ta alır.** BTC için UBTC tokenı alır.
- **Perpetual'da aynı miktarda short açar.**

Fiyat yükselince spot kazandırır, short kaybettirir; düşünce tersi olur. Yani fiyat riski birbirini götürür. Geriye kalan şey **funding**: perpetual'da long'lar short'lara her saat ödeme yapar. Hyperliquid'de sakin günlerde bu yıllık ~%11. Son 12 ayda BTC'de ortalama %6, ETH'de %6,3, HYPE'ta %9,8 ödendi (bkz. HYDRA `research/funding/RESULTS.md`).

Sermayenin bir kısmı short'un teminatı olarak durur. Bu yüzden sermayeye düşen getiri funding oranından düşüktür: 2× kaldıraçta yaklaşık üçte ikisi.

## Aşamalar

1. **Paper (şimdi):** Gerçek Hyperliquid fiyatları ve funding'iyle sanal 100$ işletir. Anahtar ya da para gerekmez.
2. **Testnet:** Hyperliquid test ağında sahte parayla gerçek emirler gönderir.
3. **Canlı, 100$:** 2–4 hafta.
4. **Büyütme:** Tavan `KANGAL_MAX_CAPITAL` ile sınırlı; kod bu tavanı aşmaz.

`KANGAL_MODE=live` şimdilik yalnızca `KANGAL_NETWORK=testnet` ile çalışır. Ana ağda canlıya geçiş testnet aşaması bitince açılacak; o zamana kadar bot başlamayı reddeder.

## Testnet'i kurmak

1. **Testnet hesabı:** https://app.hyperliquid-testnet.xyz adresine ana cüzdanınla bağlan ve faucet'ten test USDC al.
2. **API wallet:** Testnet'te *More → API* altından bir API wallet oluştur. Adresini onayla, private key'ini bir kenara yaz. Bu anahtar işlem yapar ama para çekemez.
3. **Railway:** Paper çalışmaya devam etsin diye aynı repodan ikinci bir servis aç (örn. `kangal-testnet`) ve şu değişkenleri gir:

   | Değişken | Değer |
   |---|---|
   | `KANGAL_MODE` | `live` |
   | `KANGAL_NETWORK` | `testnet` |
   | `HL_ACCOUNT_ADDRESS` | ana cüzdan adresi |
   | `HL_AGENT_KEY` | API wallet'ın private key'i |
   | `KANGAL_PANEL_PASSWORD` | panel şifresi |
   | `SLACK_WEBHOOK_URL` | istersen |

4. **Hesap modu:** Bot ilk turda hesabı *unified* moda almayı dener. Bu modda spot USDC short'a da teminat olur; API wallet spot ile perp arasında USDC taşıyamadığı için bu mod gerekli. Alamazsa hiç emir göndermez, Slack'e ve panele "unified moda al" diye yazar.
5. **Coin seçimi:** Testnet'te her coinin spot karşılığı olmayabilir. Panelin funding tablosunda "spot yok" yazmayan bir coin seç.

**Canlıda emirler nasıl gider?**
- Her turda botun bekleyen emirleri iptal edilir ve güncel fiyattan yeniden yazılır. Emirler post-only: alış bid'e, satış ask'e.
- Bir bacak dolup öbürü dolmazsa bot önce yalnızca eksik bacağı pasif olarak tamamlamaya çalışır. `KANGAL_HEDGE_AFTER_S` (60 sn) içinde dolmazsa eksik bacağı taker emirle tamamlar. Böylece pozisyon bir dakikadan uzun korumasız kalmaz.
- İki bacak her zaman aynı coin miktarıyla açılır.

## Her dakika ne oluyor?

1. **Okur:** Hyperliquid'den fiyatları, funding'i ve hesap durumunu çeker.
2. **Plan yapar:** `kangal/planner.py` hedefi hesaplar: her coin için `sermaye × ağırlık × L / (L + 1 + 0,15)` dolar spot ve aynı miktarda short.
   - **Adım adım gider:** Her turda en fazla `KANGAL_CHUNK_USD` kadar ilerler. İki bacak birlikte büyür; biri geride kalırsa önce o tamamlanır.
   - **Teminatı yönetir:** USDC'yi spot ve perp cüzdanları arasında gerektiği kadar taşır.
   - **Girer / çıkar:** Son `KANGAL_AVG_DAYS` günün ortalama funding'i `KANGAL_ENTRY_APR`'nin üstündeyse açar, `KANGAL_EXIT_APR`'nin altına düşerse kapatır. İkisinin arasında pozisyon olduğu gibi kalır; böylece funding bir iki gün dalgalandı diye aç-kapa yapıp ücret ödemez.
   - **Korur:** Likidasyon %35'ten yakınsa teminat ekler, %20'den yakınsa iki bacağı dörtte bir küçültür ve Slack'e acil uyarı atar.
3. **Uygular:** Emirler **pasif**tir (post-only): alırken bid'e, satarken ask'e yazılır, spread'i hiç geçmez ve hep maker ücreti öder. Paper modda o fiyattan dolmuş sayılır. Bu biraz iyimser: gerçekte pasif emir bazen hemen dolmaz.
4. **Raporlar:** Her 6 saatte Slack'e özet atar. Ayar değişiklikleri ve acil uyarılar da Slack'e gider.

## Kontrol paneli

Botun kendi adresinde (Railway'de *Generate Domain*) açılır. Şifre `KANGAL_PANEL_PASSWORD`, kullanıcı adı fark etmez. Şifre yoksa panel salt okunur.

- **Butonlar:**
  - *Duraklat:* Açık pozisyon kalır, yeni açma/kapama yapmaz. Likidasyon koruması yine çalışır.
  - *Devam et:* Normal çalışmaya döner.
  - *Hepsini kapat:* İki bacağı parça parça kapatır.
  - *Şimdi kontrol et:* Bir dakikayı beklemeden bir tur çalıştırır.
  - *Paper'ı sıfırla:* Paper hesabını yeni sermayeyle baştan başlatır.
- **Ayarlar:** Sermaye, coinler ve ağırlıkları, kaldıraç, giriş/çıkış eşiği, ortalama penceresi ve parça büyüklüğü. Kaydedilince `state/settings.json`'a yazılır ve environment'taki değerlerin önüne geçer.
- **Değişmeyenler:** Sermaye tavanı, 3× kaldıraç sınırı, izinli coinler (BTC, ETH, SOL, HYPE) ve mod (paper/testnet/canlı) panelden değiştirilemez. Bunlar yalnızca Railway'den değişir.
- **API:**
  - `GET /api/status`: Her şeyi JSON olarak verir.
  - `POST /api/settings`: Ayar değiştirir.
  - `POST /api/action`: `{"do": "pause" | "resume" | "close_all" | "check_now" | "reset_paper"}`.
  - `GET /health`: Açık sağlık kontrolü.

## Ayarlar (environment variables)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `KANGAL_MODE` | `paper` | `paper` veya (ileride) `live` |
| `KANGAL_NETWORK` | `mainnet` | `testnet` için test ağı |
| `KANGAL_CAPITAL_USD` | `100` | Botun kullanacağı sermaye |
| `KANGAL_MAX_CAPITAL` | `200` | Sermayenin asla geçemeyeceği tavan |
| `KANGAL_COINS` | `BTC:1` | Coinler ve ağırlıklar, örn. `BTC:0.5,HYPE:0.5` (BTC, ETH, SOL, HYPE) |
| `KANGAL_LEVERAGE` | `2` | Short tarafının kaldıracı (1–3) |
| `KANGAL_CHUNK_USD` | `25` | Tek emrin en büyük hali |
| `KANGAL_LOOP_S` | `60` | Kaç saniyede bir kontrol |
| `KANGAL_ENTRY_APR` | `5` | Ortalama funding bunun üstündeyse coini aç (% yıllık) |
| `KANGAL_EXIT_APR` | `0` | Ortalama funding bunun altına düşerse coini kapat |
| `KANGAL_AVG_DAYS` | `7` | Ortalamanın kaç günlük olduğu (1–30) |
| `KANGAL_PANEL_PASSWORD` | — | Panel şifresi; yoksa panel salt okunur |
| `KANGAL_KILL` | `0` | `1` = her şeyi kapat, yeni pozisyon açma |
| `KANGAL_HEDGE_AFTER_S` | `60` | (canlı) Eksik bacak kaç saniye sonra taker emirle tamamlansın |
| `SLACK_WEBHOOK_URL` | — | Raporların gideceği yer |
| `HL_ACCOUNT_ADDRESS` | — | (canlı) Ana cüzdan adresi |
| `HL_AGENT_KEY` | — | (canlı) API wallet'ın anahtarı: işlem yapar, **para çekemez** |

## Güvenlik

- **Ana cüzdan** (Rabby/MetaMask) sende kalır, sunucuya asla girmez.
- **API wallet:** Bot yalnızca bunu kullanır. Hyperliquid arayüzünde *More → API* altından oluşturulur; işlem yapabilir ama para çekemez.
- **Sabit sınırlar:** Sermaye tavanı, en fazla 3× kaldıraç ve izinli coin listesi kodun içinde.

## Çalıştırma

```
pip install -r requirements.txt
python -m kangal            # paper mod, BTC, 100$
python -m pytest -q         # testler (pip install -r requirements-dev.txt)
```

Railway'de ayrı bir servis olarak çalışır (`Procfile`). Paper durumu `state/paper.json`, panel ayarları `state/settings.json` dosyasında tutulur; yeniden başlatmada kaybolmaması için `/app/state`'e bir volume bağla.
