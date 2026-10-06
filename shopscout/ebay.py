"""Compare Shopify products against active eBay listings via the Browse API.

Needs a production keyset from developer.ebay.com, given as environment
variables EBAY_CLIENT_ID and EBAY_CLIENT_SECRET. Optional: EBAY_ENV=sandbox,
EBAY_MARKETPLACE (default EBAY_US).
"""
import base64
import json
import os
import re
import ssl
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db
from .scraper import now_iso

SCOPE = "https://api.ebay.com/oauth/api_scope"
RESULTS_PER_SEARCH = 50
# Compare like with like: new items, buy-it-now only.
SEARCH_FILTER = "buyingOptions:{FIXED_PRICE},conditionIds:{1000}"
MIN_TOKEN_OVERLAP = 0.6   # share of query words a listing title must contain to count as a match
RECHECK_HOURS = 24
STOPWORDS = {"the", "and", "with", "for", "a", "an", "of", "in", "on", "by", "to", "s", "mens", "womens", "men", "women"}


class EbayError(Exception):
    pass


def configured():
    return bool(os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"))


class EbayClient:
    def __init__(self, client_id=None, client_secret=None, env=None, marketplace=None):
        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise EbayError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are not set")
        env = (env or os.environ.get("EBAY_ENV", "production")).lower()
        self.host = "https://api.sandbox.ebay.com" if env == "sandbox" else "https://api.ebay.com"
        self.marketplace = marketplace or os.environ.get("EBAY_MARKETPLACE", "EBAY_US")
        self._token, self._expires = None, 0.0
        self._ctx = ssl.create_default_context()

    def _request(self, req):
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise EbayError(f"HTTP {e.code} from eBay: {detail}") from e
        except urllib.error.URLError as e:
            raise EbayError(f"cannot reach eBay: {e}") from e

    def token(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.host + "/identity/v1/oauth2/token",
            data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode(),
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        data = self._request(req)
        self._token = data["access_token"]
        self._expires = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def search(self, query, limit=RESULTS_PER_SEARCH):
        params = urllib.parse.urlencode({"q": query[:100], "limit": limit, "filter": SEARCH_FILTER})
        req = urllib.request.Request(
            f"{self.host}/buy/browse/v1/item_summary/search?{params}",
            headers={"Authorization": f"Bearer {self.token()}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
        )
        return self._request(req)


# ------------------------------------------------------------------ matching

def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower().replace("'", "")) if w not in STOPWORDS]


def build_query(title, vendor=None):
    """Brand + product title, without bracketed detail, at most 100 characters."""
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&.]*", title)
    if vendor and vendor.lower() not in title.lower():
        words.insert(0, vendor)
    query = ""
    for w in words:
        if len(query) + len(w) + 1 > 100:
            break
        query = f"{query} {w}".strip()
    return query


def is_match(query, listing_title):
    q = set(_words(query))
    if not q:
        return False
    return len(q & set(_words(listing_title))) / len(q) >= MIN_TOKEN_OVERLAP


def _landed(item):
    """Item price plus the cheapest fixed shipping cost. None if shipping isn't a known amount."""
    try:
        price = float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None, None
    costs = []
    for opt in item.get("shippingOptions") or []:
        try:
            costs.append(float(opt["shippingCost"]["value"]))
        except (KeyError, TypeError, ValueError):
            continue
    return (price + min(costs) if costs else None), price


def summarize(query, response):
    items = response.get("itemSummaries") or []
    matched = [i for i in items if is_match(query, i.get("title"))]
    landed, prices = [], []
    for i in matched:
        total, price = _landed(i)
        if price is not None:
            prices.append(price)
        if total is not None:
            landed.append(total)
    # Use price + shipping when every match has a known shipping cost, otherwise price only.
    values = landed if matched and len(landed) == len(prices) else prices
    cheapest = min(matched, key=lambda i: float(i["price"]["value"])) if prices else None
    return {
        "query": query,
        "total_results": response.get("total"),
        "matched": len(matched),
        "sellers": len({(i.get("seller") or {}).get("username") for i in matched} - {None}),
        "min_price": round(min(values), 2) if values else None,
        "median_price": round(statistics.median(values), 2) if values else None,
        "includes_shipping": bool(values) and values is landed,
        "currency": (matched[0].get("price") or {}).get("currency") if matched else None,
        "cheapest_url": cheapest.get("itemWebUrl") if cheapest else None,
    }


# --------------------------------------------------------------------- store

def save(conn, store, product_id, s):
    with db.write_lock():
        conn.execute(
            """INSERT OR REPLACE INTO ebay_checks(store, product_id, checked_at, query, total_results, matched,
                   sellers, min_price, median_price, includes_shipping, currency, cheapest_url)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (store, product_id, now_iso(), s["query"], s["total_results"], s["matched"], s["sellers"],
             s["min_price"], s["median_price"], int(s["includes_shipping"]), s["currency"], s["cheapest_url"]),
        )
        conn.commit()


def recently_checked(conn, hours=RECHECK_HOURS):
    rows = conn.execute(
        "SELECT store, product_id FROM ebay_checks WHERE julianday(checked_at) >= julianday('now', ?)", (f"-{hours} hours",)
    )
    return {(r[0], r[1]) for r in rows}


def check_products(conn, products, client=None, log=print, force=False):
    """products: dicts with store, product_id, title, vendor. Returns number checked."""
    client = client or EbayClient()
    skip = set() if force else recently_checked(conn)
    done = 0
    for p in products:
        if (p["store"], p["product_id"]) in skip:
            continue
        query = build_query(p["title"], p.get("vendor"))
        try:
            s = summarize(query, client.search(query))
        except EbayError as e:
            log(f"[ebay] stopped: {e}")
            break
        save(conn, p["store"], p["product_id"], s)
        done += 1
        log(f"[ebay] {query!r}: {s['matched']} matching of {s['total_results']} results, "
            f"median {s['median_price']}")
    log(f"[ebay] checked {done} products")
    return done
