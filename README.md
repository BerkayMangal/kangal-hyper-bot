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

`KANGAL_MODE=live` şu an bilerek çalışmıyor. Bot, testnet aşaması bitene kadar canlıda başlamayı reddeder.

## Her dakika ne oluyor?

1. **Okur:** Hyperliquid'den fiyatları, funding'i ve hesap durumunu çeker.
2. **Plan yapar:** `kangal/planner.py` hedefi hesaplar: her coin için `sermaye × ağırlık × L / (L + 1 + 0,15)` dolar spot ve aynı miktarda short.
   - **Adım adım gider:** Her turda en fazla `KANGAL_CHUNK_USD` kadar ilerler. İki bacak birlikte büyür; biri geride kalırsa önce o tamamlanır.
   - **Teminatı yönetir:** USDC'yi spot ve perp cüzdanları arasında gerektiği kadar taşır.
   - **Çıkar:** 30 günlük funding `KANGAL_EXIT_APR`'nin altına düşerse o coini kapatır.
   - **Korur:** Likidasyon %35'ten yakınsa teminat ekler, %20'den yakınsa iki bacağı dörtte bir küçültür ve Slack'e acil uyarı atar.
3. **Uygular:** Paper modda emirler mid fiyattan, maker ücretiyle dolar.
4. **Raporlar:** Her 6 saatte Slack'e özet atar. `GET /` botun güncel durumunu JSON olarak verir.

## Ayarlar (environment variables)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `KANGAL_MODE` | `paper` | `paper` veya (ileride) `live` |
| `KANGAL_NETWORK` | `mainnet` | `testnet` için test ağı |
| `KANGAL_CAPITAL_USD` | `100` | Botun kullanacağı sermaye |
| `KANGAL_MAX_CAPITAL` | `200` | Sermayenin asla geçemeyeceği tavan |
| `KANGAL_COINS` | `BTC:1` | Coinler ve ağırlıklar, örn. `BTC:0.5,HYPE:0.5` |
| `KANGAL_LEVERAGE` | `2` | Short tarafının kaldıracı (1–3) |
| `KANGAL_CHUNK_USD` | `25` | Tek emrin en büyük hali |
| `KANGAL_LOOP_S` | `60` | Kaç saniyede bir kontrol |
| `KANGAL_EXIT_APR` | `0` | 30 günlük funding bunun altına düşerse coini kapat |
| `KANGAL_KILL` | `0` | `1` = her şeyi kapat, yeni pozisyon açma |
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

Railway'de ayrı bir servis olarak çalışır (`Procfile`). Paper durumu `state/paper.json` dosyasında tutulur; yeniden başlatmada kaybolmaması için buraya bir volume bağla.
