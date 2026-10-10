# Gate ↔ Binance არბიტრაჟ სკანერი (AVAX/ETH)

ყველა ფასი მოდის **order book-იდან** (bids/asks), არა ticker-იდან / last price-დან.
Python 3.10+, მხოლოდ stdlib, API key არ სჭირდება.

```bash
python3 arb_scanner.py                 # ყოველ 5 წამში სკანი, AVAX/ETH
python3 arb_scanner.py --once --sizes 5,25,100 --depth 200
python3 arb_scanner.py --routes all    # direct + USDT-ით ორივე, triangular-იც ჩანს
python3 arb_scanner.py --fee-binance 0.075 --fee-gate 0.1 --min-profit 0.05
python3 arb_scanner.py --json          # ერთი JSON ხაზი ყოველ სკანზე
python3 -m unittest test_arb_scanner   # ტესტები
```

## ლოგიკა

**მარშრუტი ბირჟაზე:**

| route | ყიდვა AVAX (ვიხდით ETH) | გაყიდვა AVAX (ვიღებთ ETH) |
|---|---|---|
| `direct` (AVAX/ETH) | AVAX/ETH **asks** | AVAX/ETH **bids** |
| `synthetic` (USDT-ით) | ETH→USDT ETH/USDT **bids**-ზე, შემდეგ AVAX AVAX/USDT **asks**-ზე | AVAX→USDT AVAX/USDT **bids**-ზე, შემდეგ ETH ETH/USDT **asks**-ზე |

Synthetic top-of-book: `bid = AVAXUSDT_bid / ETHUSDT_ask`, `ask = AVAXUSDT_ask / ETHUSDT_bid`.

- `--routes auto` (default): ჯერ ამოწმებს, არსებობს თუ არა AVAX/ETH ბირჟაზე და ვაჭრობადია
  (Binance `exchangeInfo` status=TRADING, Gate `trade_status=tradable`). თუ კი — იყენებს მას,
  თუ არა — USDT-ით ითვლის. ეს ცალ-ცალკე წყდება თითო ბირჟაზე.
- **სიღრმე:** თითო ზომაზე (`--sizes`, AVAX-ში) book იკითხება level-by-level (VWAP), ანუ
  დიდ ზომაზე slippage ჩათვლილია. თუ fetched სიღრმე არ ყოფნის — ზომა გამოტოვებულია.
- **საკომისიო:** taker fee ყოველ ფეხზე, მიღებულ აქტივზე (synthetic = 2 fee). Defaults:
  Binance 0.10%, Gate 0.20% — შეცვალე შენი VIP დონის / BNB / GT ფასდაკლების მიხედვით.
- **შესაძლებლობა:** იყიდე N AVAX route A-ზე (ask), გაყიდე B-ზე (bid):
  `profit = sell_B(N) − buy_A(N)` ETH-ში, `profit% = profit / buy_A(N)`.
  ორივე მიმართულება მოწმდება. ბოლოს ბეჭდავს ზომას, რომელზეც absolute profit მაქსიმალურია.
- ყველა book პარალელურად მოდის; თუ მათ შორის დროის სხვაობა `--max-skew-ms`-ზე მეტია ან book
  crossed-ია, გამოაქვს გაფრთხილება.

## რას არ ითვლის

- გადარიცხვა ბირჟებს შორის (withdraw fee, დრო) — cross-exchange arb გულისხმობს, რომ
  ETH გაქვს buy-ბირჟაზე და AVAX sell-ბირჟაზე წინასწარ; rebalance ცალკე ხარჯია.
- lot size / min notional / tick size ფილტრები და REST snapshot-ის დაყოვნება
  (რეალური შესრულებისთვის WebSocket depth stream ჯობია).
