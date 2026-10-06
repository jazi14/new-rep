import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from shopscout import db, ebay, signals


def listing(title, price, ship=None, seller="s1", url="https://www.ebay.com/itm/1"):
    item = {"title": title, "price": {"value": str(price), "currency": "USD"},
            "seller": {"username": seller}, "itemWebUrl": url}
    if ship is not None:
        item["shippingOptions"] = [{"shippingCostType": "FIXED", "shippingCost": {"value": str(ship), "currency": "USD"}}]
    return item


SEARCH = {"total": 812, "itemSummaries": [
    listing("Allbirds Men's Tree Runner Jet Black Size 10 NEW", 90, 10, "a", "https://www.ebay.com/itm/a"),
    listing("Allbirds Tree Runner Jet Black mens 9", 120, 0, "b"),
    listing("Allbirds Tree Runner jet black", 130, 5, "b"),
    listing("Nike Air Max 90 Black", 60, 0, "c"),          # not a match
]}


class QueryTests(unittest.TestCase):
    def test_build_query_drops_brackets_and_adds_vendor(self):
        q = ebay.build_query("Men's Tree Runner - Jet Black (Black Sole)", "Allbirds")
        self.assertEqual(q, "Allbirds Men's Tree Runner Jet Black")

    def test_vendor_not_duplicated(self):
        self.assertEqual(ebay.build_query("Allbirds Flip Flop", "Allbirds"), "Allbirds Flip Flop")

    def test_query_capped_at_100_chars(self):
        self.assertLessEqual(len(ebay.build_query("word " * 60, "Brand")), 100)

    def test_match(self):
        q = "Allbirds Men's Tree Runner Jet Black"
        self.assertTrue(ebay.is_match(q, "Allbirds Tree Runner Jet Black size 9"))
        self.assertFalse(ebay.is_match(q, "Nike Air Max 90 Black"))


class SummaryTests(unittest.TestCase):
    def test_summary_with_shipping(self):
        s = ebay.summarize("Allbirds Men's Tree Runner Jet Black", SEARCH)
        self.assertEqual(s["matched"], 3)
        self.assertEqual(s["sellers"], 2)
        self.assertEqual(s["min_price"], 100.0)       # 90 + 10 shipping
        self.assertEqual(s["median_price"], 120.0)    # 100, 120, 135
        self.assertTrue(s["includes_shipping"])
        self.assertEqual(s["cheapest_url"], "https://www.ebay.com/itm/a")

    def test_unknown_shipping_falls_back_to_price(self):
        resp = {"total": 2, "itemSummaries": [listing("Brand Widget Blue", 10, 2), listing("Brand Widget Blue", 20)]}
        s = ebay.summarize("Brand Widget Blue", resp)
        self.assertFalse(s["includes_shipping"])
        self.assertEqual(s["median_price"], 15.0)

    def test_no_results(self):
        s = ebay.summarize("Brand Widget", {"total": 0})
        self.assertEqual((s["matched"], s["median_price"], s["includes_shipping"]), (0, None, False))


class FakeEbay(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        FakeEbay.seen.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        self._json({"access_token": "TOKEN", "expires_in": 7200, "token_type": "Application Access Token"})

    def do_GET(self):
        FakeEbay.seen.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
        self._json(SEARCH)


class ClientAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), FakeEbay)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        FakeEbay.seen = []
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.httpd.shutdown()
        self.conn.close()
        self.tmp.cleanup()

    def client(self):
        c = ebay.EbayClient("id", "secret")
        c.host = f"http://127.0.0.1:{self.httpd.server_port}"
        return c

    def test_requests_are_shaped_correctly(self):
        c = self.client()
        c.search("Allbirds Tree Runner")
        c.search("again")  # token reused
        posts = [s for s in FakeEbay.seen if s[0] == "POST"]
        gets = [s for s in FakeEbay.seen if s[0] == "GET"]
        self.assertEqual(len(posts), 1)
        _, path, headers, body = posts[0]
        self.assertEqual(path, "/identity/v1/oauth2/token")
        self.assertEqual(headers["authorization"], "Basic aWQ6c2VjcmV0")  # base64("id:secret")
        self.assertEqual(urllib.parse.parse_qs(body),
                         {"grant_type": ["client_credentials"], "scope": ["https://api.ebay.com/oauth/api_scope"]})
        path, headers = gets[0][1], gets[0][2]
        url = urllib.parse.urlsplit(path)
        self.assertEqual(url.path, "/buy/browse/v1/item_summary/search")
        self.assertEqual(urllib.parse.parse_qs(url.query)["filter"], [ebay.SEARCH_FILTER])
        self.assertEqual(headers["authorization"], "Bearer TOKEN")
        self.assertEqual(headers["x-ebay-c-marketplace-id"], "EBAY_US")

    def test_check_products_saves_and_skips_recent(self):
        self.conn.execute("INSERT INTO products(store, product_id, handle, title, vendor) VALUES('s.com', 1, 'h', "
                          "'Men''s Tree Runner - Jet Black', 'Allbirds')")
        self.conn.execute("INSERT INTO snapshots(run_id, store, product_id, taken_at, price_min, price_max, variants, "
                          "variants_available) VALUES(1, 's.com', 1, '2026-10-01T00:00:00+00:00', 100, 100, 5, 5)")
        self.conn.commit()
        rows = signals.products(self.conn)
        self.assertEqual(ebay.check_products(self.conn, rows, self.client(), log=lambda m: None), 1)
        self.assertEqual(ebay.check_products(self.conn, rows, self.client(), log=lambda m: None), 0)
        r = signals.products(self.conn)[0]
        self.assertEqual((r["ebay_matched"], r["ebay_median"], r["ebay_gap_pct"]), (3, 120.0, 20.0))


if __name__ == "__main__":
    unittest.main()
