import io
import unittest
import unittest.mock
from contextlib import redirect_stdout

import arb_scanner as s


def book(bids, asks, ex="x", sym="A/B"):
    return s.OrderBook(ex, sym, bids, asks, received_at=0.0, latency_ms=0.0)


class DepthWalk(unittest.TestCase):
    asks = [(10.0, 1.0), (11.0, 2.0)]
    bids = [(9.0, 1.0), (8.0, 2.0)]

    def test_buy_base_cost_walks_levels(self):
        self.assertAlmostEqual(s.buy_base_cost(self.asks, 2.0), 10 + 11)
        self.assertIsNone(s.buy_base_cost(self.asks, 3.5))

    def test_sell_base_proceeds_walks_levels(self):
        self.assertAlmostEqual(s.sell_base_proceeds(self.bids, 2.0), 9 + 8)
        self.assertIsNone(s.sell_base_proceeds(self.bids, 4))

    def test_inverse_walks_match_forward_walks(self):
        cost = s.buy_base_cost(self.asks, 2.5)
        self.assertAlmostEqual(s.base_bought_with_quote(self.asks, cost), 2.5)
        proceeds = s.sell_base_proceeds(self.bids, 2.5)
        self.assertAlmostEqual(s.base_needed_for_quote(self.bids, proceeds), 2.5)

    def test_book_sorted_and_empty_levels_dropped(self):
        b = book([(1, 1), (3, 0), (2, 1)], [(5, 1), (4, 1), (3.5, 0)])
        self.assertEqual(b.best_bid, 2)
        self.assertEqual(b.best_ask, 4)


class Routes(unittest.TestCase):
    def test_direct_fees(self):
        r = s.Route("x", "direct", 0.001, direct=book([(0.01, 100)], [(0.011, 100)]))
        # to keep 10 AVAX net you must buy 10/0.999
        self.assertAlmostEqual(r.buy_cost(10), 10 / 0.999 * 0.011)
        self.assertAlmostEqual(r.sell_proceeds(10), 10 * 0.01 * 0.999)

    def test_synthetic_uses_correct_sides(self):
        avax = book([(20.0, 1000)], [(20.1, 1000)])      # AVAX/USDT
        eth = book([(2000.0, 1000)], [(2001.0, 1000)])   # ETH/USDT
        r = s.Route("x", "synthetic", 0.0, base_bridge=avax, quote_bridge=eth)
        bid, ask = r.top_of_book()
        self.assertAlmostEqual(bid, 20.0 / 2001.0)   # sell AVAX @bid, buy ETH @ask
        self.assertAlmostEqual(ask, 20.1 / 2000.0)   # sell ETH @bid, buy AVAX @ask
        self.assertAlmostEqual(r.buy_cost(1), ask)
        self.assertAlmostEqual(r.sell_proceeds(1), bid)
        self.assertLess(bid, ask)

    def test_synthetic_fees_on_both_legs(self):
        avax = book([(20.0, 1000)], [(20.0, 1000)])
        eth = book([(2000.0, 1000)], [(2000.0, 1000)])
        r = s.Route("x", "synthetic", 0.001, base_bridge=avax, quote_bridge=eth)
        self.assertAlmostEqual(r.sell_proceeds(1), 0.01 * 0.999 ** 2)
        self.assertAlmostEqual(r.buy_cost(1), 0.01 / 0.999 ** 2)


class Scanner(unittest.TestCase):
    def setUp(self):
        # binance AVAX cheap, gate AVAX expensive => buy binance, sell gate
        self.bn = s.Route("binance", "direct", 0.001,
                          direct=book([(0.0099, 50)], [(0.0100, 5), (0.0102, 50)], "binance"))
        self.gt = s.Route("gate", "direct", 0.002,
                          direct=book([(0.0105, 5), (0.0100, 50)], [(0.0106, 50)], "gate"))

    def test_finds_cross_exchange_arb_and_depth_kills_it(self):
        opps = {(o.buy_route, o.sell_route, o.qty): o
                for o in s.evaluate([self.bn, self.gt], (1, 40))}
        small = opps[("binance:direct", "gate:direct", 1)]
        self.assertGreater(small.profit, 0)
        self.assertAlmostEqual(small.profit_pct,
                               (0.0105 * 0.998 / (0.0100 / 0.999) - 1) * 100)
        self.assertLess(opps[("binance:direct", "gate:direct", 40)].profit, 0)
        self.assertLess(opps[("gate:direct", "binance:direct", 1)].profit, 0)

    def test_best_size_within_depth(self):
        o = s.best_size(self.bn, self.gt)
        self.assertIsNotNone(o)
        self.assertGreater(o.profit, 0)
        self.assertLessEqual(o.qty, 5.01)
        self.assertIsNone(s.best_size(self.gt, self.bn))

    def test_report_renders(self):
        cfg = s.Config(sizes=(1, 10))
        out = io.StringIO()
        with redirect_stdout(out):
            s.report(cfg, [self.bn, self.gt], as_json=False)
        self.assertIn("ARB", out.getvalue())


class FakeExchange(s.Exchange):
    def __init__(self, name, books):
        self.name, self.books = name, books

    def pair_tradable(self, base, quote):
        return (base, quote) in self.books

    def order_book(self, base, quote, depth):
        b, a = self.books[(base, quote)]
        return book(b, a, self.name, f"{base}/{quote}")


class Discovery(unittest.TestCase):
    def test_falls_back_to_usdt_when_direct_missing(self):
        clients = {
            "binance": FakeExchange("binance", {
                ("AVAX", "ETH"): ([(0.0099, 9)], [(0.0101, 9)]),
                ("AVAX", "USDT"): ([(20, 9)], [(20.1, 9)]),
                ("ETH", "USDT"): ([(2000, 9)], [(2001, 9)]),
            }),
            "gate": FakeExchange("gate", {
                ("AVAX", "USDT"): ([(20, 9)], [(20.1, 9)]),
                ("ETH", "USDT"): ([(2000, 9)], [(2001, 9)]),
            }),
        }
        cfg = s.Config()
        with redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            plan = s.discover_routes(cfg, clients)
        self.assertEqual(plan, {"binance": ["direct"], "gate": ["synthetic"]})
        routes = s.fetch_routes(cfg, clients, plan)
        self.assertEqual({r.name for r in routes}, {"binance:direct", "gate:synthetic"})

        cfg.routes = "all"
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            plan = s.discover_routes(cfg, clients)
        self.assertEqual(plan["binance"], ["direct", "synthetic"])



if __name__ == "__main__":
    unittest.main()
