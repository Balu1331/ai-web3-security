#!/usr/bin/env python3
"""
Gate.io <-> Binance order-book arbitrage scanner.

Main pair: AVAX/ETH (configurable with --base/--quote).

Every price comes from the live order book (bids/asks), never from the
"last price" / ticker. For each trade size the books are walked level by
level (VWAP), taker fees are applied on every leg, and the result tells you
how much ETH you really pay to buy N AVAX and how much ETH you really get
for selling N AVAX.

Routes per exchange:
  direct     AVAX/ETH book                                     (1 trade)
  synthetic  AVAX/USDT + ETH/USDT books                         (2 trades)
             buy  AVAX: sell ETH -> USDT (ETH/USDT bids), buy AVAX (AVAX/USDT asks)
             sell AVAX: sell AVAX -> USDT (AVAX/USDT bids), buy ETH (ETH/USDT asks)

The synthetic route is used only when the direct pair does not exist / is
not trading, unless --routes all is given (then both are compared, which
also exposes intra-exchange triangular opportunities).

An opportunity = buy AVAX on route A at its ASK side, sell the same AVAX on
route B at its BID side:   profit_eth = sell_B(N) - buy_A(N)

Stdlib only, no API keys needed (public endpoints).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

Level = tuple[float, float]  # (price, qty)


# --------------------------------------------------------------------------
# Order book + depth walking
# --------------------------------------------------------------------------

@dataclass
class OrderBook:
    exchange: str
    symbol: str          # normalized "BASE/QUOTE"
    bids: list[Level]    # sorted best (highest) first
    asks: list[Level]    # sorted best (lowest) first
    received_at: float   # local unix time (s) when the response arrived
    latency_ms: float

    def __post_init__(self) -> None:
        self.bids = sorted(((p, q) for p, q in self.bids if q > 0), key=lambda x: -x[0])
        self.asks = sorted(((p, q) for p, q in self.asks if q > 0), key=lambda x: x[0])

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def crossed(self) -> bool:
        return bool(self.bids and self.asks and self.best_bid >= self.best_ask)


def buy_base_cost(asks: list[Level], base_qty: float) -> float | None:
    """Quote spent to buy `base_qty` base by lifting asks. None = not enough depth."""
    remaining, cost = base_qty, 0.0
    for price, qty in asks:
        take = min(remaining, qty)
        cost += take * price
        remaining -= take
        if remaining <= 1e-15:
            return cost
    return None


def sell_base_proceeds(bids: list[Level], base_qty: float) -> float | None:
    """Quote received for selling `base_qty` base into bids. None = not enough depth."""
    remaining, proceeds = base_qty, 0.0
    for price, qty in bids:
        take = min(remaining, qty)
        proceeds += take * price
        remaining -= take
        if remaining <= 1e-15:
            return proceeds
    return None


def base_bought_with_quote(asks: list[Level], quote_amt: float) -> float | None:
    """Base received when spending exactly `quote_amt` quote on asks."""
    remaining, base = quote_amt, 0.0
    for price, qty in asks:
        level_quote = price * qty
        if level_quote >= remaining:
            return base + remaining / price
        base += qty
        remaining -= level_quote
    return None


def base_needed_for_quote(bids: list[Level], quote_target: float) -> float | None:
    """Base that must be sold into bids to receive exactly `quote_target` quote."""
    remaining, base = quote_target, 0.0
    for price, qty in bids:
        level_quote = price * qty
        if level_quote >= remaining:
            return base + remaining / price
        base += qty
        remaining -= level_quote
    return None


# --------------------------------------------------------------------------
# Routes (how to get BASE/QUOTE exposure on one exchange)
# --------------------------------------------------------------------------

@dataclass
class Route:
    """Buy/sell `base` against `quote` on one exchange, with taker fees.

    Fees are modelled as charged on the asset you receive (the default on
    both Binance and Gate when you don't pay fees in BNB/GT).
    """
    exchange: str
    kind: str                         # "direct" | "synthetic"
    fee: float                        # taker fee as fraction, e.g. 0.001
    direct: OrderBook | None = None   # BASE/QUOTE
    base_bridge: OrderBook | None = None   # BASE/BRIDGE  (e.g. AVAX/USDT)
    quote_bridge: OrderBook | None = None  # QUOTE/BRIDGE (e.g. ETH/USDT)

    @property
    def name(self) -> str:
        return f"{self.exchange}:{self.kind}"

    @property
    def books(self) -> list[OrderBook]:
        return [b for b in (self.direct, self.base_bridge, self.quote_bridge) if b]

    def buy_cost(self, qty: float) -> float | None:
        """QUOTE spent to end up with `qty` BASE net of fees."""
        keep = 1.0 - self.fee
        if self.kind == "direct":
            return buy_base_cost(self.direct.asks, qty / keep)
        # 1) buy BASE with BRIDGE on BASE/BRIDGE asks
        bridge_needed = buy_base_cost(self.base_bridge.asks, qty / keep)
        if bridge_needed is None:
            return None
        # 2) get that BRIDGE (net) by selling QUOTE into QUOTE/BRIDGE bids
        return base_needed_for_quote(self.quote_bridge.bids, bridge_needed / keep)

    def sell_proceeds(self, qty: float) -> float | None:
        """QUOTE received (net of fees) for selling `qty` BASE."""
        keep = 1.0 - self.fee
        if self.kind == "direct":
            p = sell_base_proceeds(self.direct.bids, qty)
            return None if p is None else p * keep
        # 1) sell BASE into BASE/BRIDGE bids
        bridge = sell_base_proceeds(self.base_bridge.bids, qty)
        if bridge is None:
            return None
        # 2) buy QUOTE with the BRIDGE on QUOTE/BRIDGE asks
        quote = base_bought_with_quote(self.quote_bridge.asks, bridge * keep)
        return None if quote is None else quote * keep

    def top_of_book(self) -> tuple[float | None, float | None]:
        """Raw (fee-free) best bid / ask expressed in QUOTE per BASE."""
        if self.kind == "direct":
            return self.direct.best_bid, self.direct.best_ask
        bb, ba = self.base_bridge.best_bid, self.base_bridge.best_ask
        qb, qa = self.quote_bridge.best_bid, self.quote_bridge.best_ask
        if None in (bb, ba, qb, qa):
            return None, None
        # selling BASE -> BRIDGE at bb, then buying QUOTE at qa  => bid = bb/qa
        # buying  BASE with BRIDGE at ba, bridge from QUOTE at qb => ask = ba/qb
        return bb / qa, ba / qb

    def max_qty(self) -> float:
        """Rough upper bound on BASE size the fetched depth can absorb both ways."""
        if self.kind == "direct":
            return min(sum(q for _, q in self.direct.asks), sum(q for _, q in self.direct.bids))
        lo, hi = 0.0, min(sum(q for _, q in self.base_bridge.asks),
                          sum(q for _, q in self.base_bridge.bids))
        for _ in range(50):  # bisection on what the bridge legs can carry
            mid = (lo + hi) / 2
            if self.buy_cost(mid) is not None and self.sell_proceeds(mid) is not None:
                lo = mid
            else:
                hi = mid
        return lo


# --------------------------------------------------------------------------
# Exchange clients (public REST)
# --------------------------------------------------------------------------

HTTP_TIMEOUT = 10


def http_get_json(url: str, params: dict | None = None) -> tuple[object, float]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "arb-scanner/1.0",
                                               "Accept": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return data, (time.time() - t0) * 1000


class Exchange:
    name = ""

    def symbol(self, base: str, quote: str) -> str:
        raise NotImplementedError

    def pair_tradable(self, base: str, quote: str) -> bool:
        raise NotImplementedError

    def order_book(self, base: str, quote: str, depth: int) -> OrderBook:
        raise NotImplementedError


class Binance(Exchange):
    name = "binance"
    API = "https://api.binance.com"
    DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000, 5000)

    def symbol(self, base, quote):
        return f"{base}{quote}".upper()

    def pair_tradable(self, base, quote):
        try:
            data, _ = http_get_json(f"{self.API}/api/v3/exchangeInfo",
                                    {"symbol": self.symbol(base, quote)})
        except urllib.error.HTTPError as e:
            if e.code == 400:  # -1121 Invalid symbol
                return False
            raise
        syms = data.get("symbols", [])
        return bool(syms) and syms[0].get("status") == "TRADING"

    def order_book(self, base, quote, depth):
        limit = next((l for l in self.DEPTH_LIMITS if l >= depth), 5000)
        data, lat = http_get_json(f"{self.API}/api/v3/depth",
                                  {"symbol": self.symbol(base, quote), "limit": limit})
        return OrderBook(
            exchange=self.name, symbol=f"{base}/{quote}",
            bids=[(float(p), float(q)) for p, q in data["bids"]],
            asks=[(float(p), float(q)) for p, q in data["asks"]],
            received_at=time.time(), latency_ms=lat,
        )


class Gate(Exchange):
    name = "gate"
    API = "https://api.gateio.ws/api/v4"

    def symbol(self, base, quote):
        return f"{base}_{quote}".upper()

    def pair_tradable(self, base, quote):
        try:
            data, _ = http_get_json(f"{self.API}/spot/currency_pairs/{self.symbol(base, quote)}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return False
            raise
        return data.get("trade_status") == "tradable"

    def order_book(self, base, quote, depth):
        data, lat = http_get_json(f"{self.API}/spot/order_book",
                                  {"currency_pair": self.symbol(base, quote),
                                   "limit": min(depth, 1000), "with_id": "true"})
        return OrderBook(
            exchange=self.name, symbol=f"{base}/{quote}",
            bids=[(float(p), float(q)) for p, q in data["bids"]],
            asks=[(float(p), float(q)) for p, q in data["asks"]],
            received_at=time.time(), latency_ms=lat,
        )


EXCHANGES: dict[str, type[Exchange]] = {"binance": Binance, "gate": Gate}


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

@dataclass
class Opportunity:
    buy_route: str
    sell_route: str
    kind: str            # "cross-exchange" | "triangular"
    qty: float           # BASE
    buy_cost: float      # QUOTE
    sell_proceeds: float  # QUOTE
    profit: float        # QUOTE
    profit_pct: float

    @property
    def buy_vwap(self) -> float:
        return self.buy_cost / self.qty

    @property
    def sell_vwap(self) -> float:
        return self.sell_proceeds / self.qty


@dataclass
class Config:
    base: str = "AVAX"
    quote: str = "ETH"
    bridge: str = "USDT"
    exchanges: tuple[str, ...] = ("binance", "gate")
    fees: dict[str, float] = field(default_factory=lambda: {"binance": 0.001, "gate": 0.002})
    depth: int = 100
    sizes: tuple[float, ...] = (1, 10, 50, 100)
    routes: str = "auto"         # "auto" | "direct" | "synthetic" | "all"
    min_profit_pct: float = 0.0
    max_skew_ms: float = 1500.0


def discover_routes(cfg: Config, clients: dict[str, Exchange]) -> dict[str, list[str]]:
    """Which route kinds to use per exchange (checks if the direct pair exists)."""
    plan: dict[str, list[str]] = {}

    def check(name):
        c = clients[name]
        direct_ok = c.pair_tradable(cfg.base, cfg.quote)
        bridge_ok = (c.pair_tradable(cfg.base, cfg.bridge)
                     and c.pair_tradable(cfg.quote, cfg.bridge))
        kinds = []
        if cfg.routes in ("auto", "direct", "all") and direct_ok:
            kinds.append("direct")
        if bridge_ok and (cfg.routes in ("synthetic", "all")
                          or (cfg.routes == "auto" and not direct_ok)):
            kinds.append("synthetic")
        return name, kinds, direct_ok, bridge_ok

    with ThreadPoolExecutor(max_workers=len(clients)) as ex:
        for name, kinds, direct_ok, bridge_ok in ex.map(check, clients):
            print(f"[info] {name}: {cfg.base}/{cfg.quote} direct={'yes' if direct_ok else 'NO'}"
                  f", via {cfg.bridge}={'yes' if bridge_ok else 'NO'} -> routes {kinds or 'NONE'}",
                  file=sys.stderr)
            plan[name] = kinds
    return plan


def fetch_routes(cfg: Config, clients: dict[str, Exchange],
                 plan: dict[str, list[str]]) -> list[Route]:
    needed: set[tuple[str, str, str]] = set()
    for name, kinds in plan.items():
        if "direct" in kinds:
            needed.add((name, cfg.base, cfg.quote))
        if "synthetic" in kinds:
            needed.add((name, cfg.base, cfg.bridge))
            needed.add((name, cfg.quote, cfg.bridge))

    # fetch all books in parallel to keep them as close in time as possible
    with ThreadPoolExecutor(max_workers=max(1, len(needed))) as ex:
        futs = {k: ex.submit(clients[k[0]].order_book, k[1], k[2], cfg.depth) for k in needed}
        books = {}
        for k, f in futs.items():
            try:
                books[k] = f.result()
            except Exception as e:  # noqa: BLE001 - report and skip this book
                print(f"[warn] {k[0]} {k[1]}/{k[2]} order book failed: {e}", file=sys.stderr)

    routes = []
    for name, kinds in plan.items():
        fee = cfg.fees.get(name, 0.002)
        if "direct" in kinds and (d := books.get((name, cfg.base, cfg.quote))):
            routes.append(Route(name, "direct", fee, direct=d))
        if "synthetic" in kinds:
            b = books.get((name, cfg.base, cfg.bridge))
            q = books.get((name, cfg.quote, cfg.bridge))
            if b and q:
                routes.append(Route(name, "synthetic", fee, base_bridge=b, quote_bridge=q))
    return routes


def evaluate(routes: list[Route], sizes: tuple[float, ...]) -> list[Opportunity]:
    opps = []
    for a in routes:
        for b in routes:
            if a is b:
                continue
            kind = "cross-exchange" if a.exchange != b.exchange else "triangular"
            for qty in sizes:
                cost, proceeds = a.buy_cost(qty), b.sell_proceeds(qty)
                if cost is None or proceeds is None or cost <= 0:
                    continue
                profit = proceeds - cost
                opps.append(Opportunity(a.name, b.name, kind, qty, cost, proceeds,
                                        profit, profit / cost * 100))
    return opps


def best_size(buy: Route, sell: Route, steps: int = 60) -> Opportunity | None:
    """Size (on a log grid up to available depth) that maximizes absolute profit."""
    hi = min(buy.max_qty(), sell.max_qty())
    if hi <= 0:
        return None
    lo = hi * 1e-6
    best = None
    for i in range(steps + 1):
        qty = lo * (hi / lo) ** (i / steps)
        cost, proceeds = buy.buy_cost(qty), sell.sell_proceeds(qty)
        if cost is None or proceeds is None:
            continue
        profit = proceeds - cost
        if profit > 0 and (best is None or profit > best.profit):
            kind = "cross-exchange" if buy.exchange != sell.exchange else "triangular"
            best = Opportunity(buy.name, sell.name, kind, qty, cost, proceeds,
                               profit, profit / cost * 100)
    return best


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def fmt(x: float | None, digits: int = 8) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def report(cfg: Config, routes: list[Route], as_json: bool) -> None:
    pair = f"{cfg.base}/{cfg.quote}"
    books = [bk for r in routes for bk in r.books]
    skew = (max(b.received_at for b in books) - min(b.received_at for b in books)) * 1000 if books else 0
    opps = evaluate(routes, cfg.sizes)
    best = [o for a in routes for b in routes if a is not b and (o := best_size(a, b))]

    if as_json:
        print(json.dumps({
            "ts": time.time(), "pair": pair, "book_skew_ms": skew,
            "routes": [{"route": r.name, "fee": r.fee,
                        "bid": r.top_of_book()[0], "ask": r.top_of_book()[1],
                        "books": [{"symbol": b.symbol, "bid": b.best_bid, "ask": b.best_ask,
                                   "latency_ms": round(b.latency_ms, 1)} for b in r.books]}
                       for r in routes],
            "opportunities": [o.__dict__ for o in opps],
            "best_size": [o.__dict__ for o in best],
        }))
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== {ts}  {pair}  (prices = {cfg.quote} per 1 {cfg.base}, from order books) ===")
    if skew > cfg.max_skew_ms:
        print(f"!! book fetch skew {skew:.0f} ms > {cfg.max_skew_ms:.0f} ms - results may be stale")
    for b in books:
        if b.crossed:
            print(f"!! {b.exchange} {b.symbol} book is crossed (bid >= ask) - snapshot is inconsistent")

    print(f"\n{'route':<20}{'fee%':>6}{'best bid':>16}{'best ask':>16}{'spread bps':>12}   legs")
    for r in routes:
        bid, ask = r.top_of_book()
        spread = (ask - bid) / ((ask + bid) / 2) * 1e4 if bid and ask else None
        legs = ", ".join(f"{b.symbol} {fmt(b.best_bid, 6)}/{fmt(b.best_ask, 6)} "
                         f"({b.latency_ms:.0f}ms)" for b in r.books)
        print(f"{r.name:<20}{r.fee * 100:>6.3f}{fmt(bid):>16}{fmt(ask):>16}{fmt(spread, 1):>12}   {legs}")

    print(f"\n{'buy @ask on':<20}{'sell @bid on':<20}{'type':<16}{'size':>9}"
          f"{'buy vwap':>15}{'sell vwap':>15}{'profit ' + cfg.quote:>16}{'profit %':>10}")
    for o in sorted(opps, key=lambda o: (o.buy_route, o.sell_route, o.qty)):
        mark = "  <== ARB" if o.profit_pct > cfg.min_profit_pct and o.profit > 0 else ""
        print(f"{o.buy_route:<20}{o.sell_route:<20}{o.kind:<16}{o.qty:>9g}"
              f"{fmt(o.buy_vwap):>15}{fmt(o.sell_vwap):>15}{o.profit:>+16.8f}{o.profit_pct:>+10.4f}{mark}")

    if best:
        print("\nprofit-maximizing size (within fetched depth, after fees):")
        for o in sorted(best, key=lambda o: -o.profit):
            print(f"  buy {o.buy_route} -> sell {o.sell_route}: {o.qty:.4f} {cfg.base}, "
                  f"profit {o.profit:+.8f} {cfg.quote} ({o.profit_pct:+.4f}%)")
    else:
        print("\nno profitable direction after fees at any size.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None) -> tuple[Config, argparse.Namespace]:
    p = argparse.ArgumentParser(description="Gate <-> Binance order-book arbitrage scanner")
    p.add_argument("--base", default="AVAX")
    p.add_argument("--quote", default="ETH")
    p.add_argument("--bridge", default="USDT", help="bridge asset when the direct pair is missing")
    p.add_argument("--routes", choices=("auto", "direct", "synthetic", "all"), default="auto",
                   help="auto = direct pair if it exists, otherwise via bridge; "
                        "all = compare both (shows triangular arb too)")
    p.add_argument("--sizes", default="1,10,50,100", help="trade sizes in BASE units, comma separated")
    p.add_argument("--depth", type=int, default=100, help="order book levels to fetch")
    p.add_argument("--fee-binance", type=float, default=0.10, help="taker fee %% on Binance")
    p.add_argument("--fee-gate", type=float, default=0.20, help="taker fee %% on Gate")
    p.add_argument("--min-profit", type=float, default=0.0, help="highlight threshold, %%")
    p.add_argument("--max-skew-ms", type=float, default=1500.0)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between scans")
    p.add_argument("--once", action="store_true", help="scan once and exit")
    p.add_argument("--json", action="store_true", help="one JSON object per scan")
    a = p.parse_args(argv)
    cfg = Config(
        base=a.base.upper(), quote=a.quote.upper(), bridge=a.bridge.upper(),
        fees={"binance": a.fee_binance / 100, "gate": a.fee_gate / 100},
        depth=a.depth, sizes=tuple(float(s) for s in a.sizes.split(",") if s.strip()),
        routes=a.routes, min_profit_pct=a.min_profit, max_skew_ms=a.max_skew_ms,
    )
    return cfg, a


def main(argv=None) -> int:
    cfg, args = parse_args(argv)
    clients = {n: EXCHANGES[n]() for n in cfg.exchanges}
    try:
        plan = discover_routes(cfg, clients)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[error] cannot reach exchange API: {e}", file=sys.stderr)
        return 2
    if sum(len(k) for k in plan.values()) < 2:
        print("need at least two routes to compare - nothing to scan", file=sys.stderr)
        return 1
    while True:
        try:
            report(cfg, fetch_routes(cfg, clients, plan), args.json)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            print(f"[error] scan failed: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
