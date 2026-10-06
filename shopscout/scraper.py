"""Collect product data from Shopify stores via their public JSON endpoints.

Only endpoints that the store's robots.txt allows are fetched. Requests are
spaced out per store and 429 responses are retried after Retry-After.
"""
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import db

USER_AGENT = "ShopScout/0.1 (product research; +https://github.com/jazi14/new-rep)"
PAGE_SIZE = 250
MAX_PAGES = 40            # 40 x 250 = 10,000 products per store
REQUEST_DELAY = 1.5       # seconds between requests to the same store
MAX_RETRIES = 3
BESTSELLER_WORDS = ("best-seller", "bestseller", "best-selling", "top-seller", "most-popular", "trending")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- robots.txt

class Robots:
    """Minimal robots.txt matcher supporting * and $ (Google semantics)."""

    def __init__(self, text):
        self.rules = []  # (allow: bool, pattern: str)
        groups, agents, rules, last_was_agent = [], [], [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if val:
                    rules.append((key == "allow", val))
        if agents:
            groups.append((agents, rules))
        mine = [r for a, r in groups if any(x != "*" and x in USER_AGENT.lower() for x in a)]
        star = [r for a, r in groups if "*" in a]
        for r in (mine or star):
            self.rules.extend(r)

    @staticmethod
    def _regex(pattern):
        anchored = pattern.endswith("$")
        body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        return re.compile(body + ("$" if anchored else ""))

    def allowed(self, path):
        best = None  # (length, allow)
        for allow, pattern in self.rules:
            if self._regex(pattern).match(path):
                cand = (len(pattern), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]


# -------------------------------------------------------------------- client

class StoreClient:
    def __init__(self, domain):
        self.domain = normalize_domain(domain)
        self.base = f"https://{self.domain}"
        self._last = 0.0
        self._ctx = ssl.create_default_context()
        self.robots = Robots(self._get_text("/robots.txt", allow_missing=True) or "")

    def _wait(self):
        delta = time.monotonic() - self._last
        if delta < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - delta)
        self._last = time.monotonic()

    def _get_text(self, path, allow_missing=False):
        url = self.base + path
        for attempt in range(MAX_RETRIES + 1):
            self._wait()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*"})
            try:
                with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < MAX_RETRIES:
                    retry_after = e.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1))
                    continue
                if e.code == 404 and allow_missing:
                    return None
                raise FetchError(f"HTTP {e.code} on {path}") from e
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
                if attempt < MAX_RETRIES:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise FetchError(f"{type(e).__name__}: {e} on {path}") from e
        raise FetchError(f"gave up on {path}")

    def get_json(self, path):
        if not self.robots.allowed(path):
            raise FetchError(f"robots.txt disallows {path}")
        text = self._get_text(path)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"{path} did not return JSON (store may not be Shopify or blocks bots)") from e

    def products(self, endpoint="/products.json"):
        out = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.get_json(f"{endpoint}?limit={PAGE_SIZE}&page={page}").get("products", [])
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return out

    def find_bestseller_collection(self):
        """Return the handle of the store's own best-seller collection, if it has one."""
        try:
            cols = self.get_json(f"/collections.json?limit={PAGE_SIZE}").get("collections", [])
        except FetchError:
            return None
        hits = []
        for c in cols:
            text = f"{c.get('handle', '')} {c.get('title', '')}".lower().replace(" ", "-")
            for i, word in enumerate(BESTSELLER_WORDS):
                if word in text:
                    # Prefer the earliest keyword, then the shortest (most generic) handle.
                    hits.append((i, len(c["handle"]), c["handle"]))
                    break
        return min(hits)[2] if hits else None


def normalize_domain(domain):
    domain = domain.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    return domain.split("/", 1)[0]


# ------------------------------------------------------------------- parsing

def summarize(p):
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price")]
    compare = [float(v["compare_at_price"]) for v in variants if v.get("compare_at_price")]
    images = p.get("images") or []
    tags = p.get("tags")
    return {
        "product_id": p["id"],
        "handle": p.get("handle"),
        "title": p.get("title"),
        "vendor": p.get("vendor"),
        "product_type": p.get("product_type"),
        "tags": ", ".join(tags) if isinstance(tags, list) else (tags or ""),
        "image": images[0]["src"] if images else None,
        "created_at": p.get("created_at"),
        "published_at": p.get("published_at"),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "compare_at_max": max(compare) if compare else None,
        "variants": len(variants),
        "variants_available": sum(1 for v in variants if v.get("available")),
    }


# --------------------------------------------------------------------- store

def scrape_store(conn, run_id, domain, log=print):
    domain = normalize_domain(domain)
    taken_at = now_iso()
    try:
        client = StoreClient(domain)
        products = client.products()
        bs_handle = client.find_bestseller_collection()
        ranks = {}
        if bs_handle:
            for rank, p in enumerate(client.products(f"/collections/{bs_handle}/products.json"), 1):
                ranks.setdefault(p["id"], rank)
    except FetchError as e:
        with db.write_lock():
            conn.execute(
                "INSERT INTO stores(domain, last_scraped, last_status, last_error) VALUES(?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET last_scraped=excluded.last_scraped, "
                "last_status=excluded.last_status, last_error=excluded.last_error",
                (domain, taken_at, "error", str(e)),
            )
            conn.commit()
        log(f"[{domain}] failed: {e}")
        return 0

    with db.write_lock():
        for p in products:
            s = summarize(p)
            conn.execute(
                """INSERT INTO products(store, product_id, handle, title, vendor, product_type, tags, image,
                       created_at, published_at, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(store, product_id) DO UPDATE SET handle=excluded.handle, title=excluded.title,
                       vendor=excluded.vendor, product_type=excluded.product_type, tags=excluded.tags,
                       image=excluded.image, created_at=excluded.created_at,
                       published_at=excluded.published_at, last_seen=excluded.last_seen""",
                (domain, s["product_id"], s["handle"], s["title"], s["vendor"], s["product_type"], s["tags"],
                 s["image"], s["created_at"], s["published_at"], taken_at, taken_at),
            )
            conn.execute(
                """INSERT OR REPLACE INTO snapshots(run_id, store, product_id, taken_at, price_min, price_max,
                       compare_at_max, variants, variants_available, bestseller_rank)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, domain, s["product_id"], taken_at, s["price_min"], s["price_max"], s["compare_at_max"],
                 s["variants"], s["variants_available"], ranks.get(s["product_id"])),
            )
        conn.execute(
            "INSERT INTO stores(domain, last_scraped, last_status, last_error, product_count, bestseller_collection) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET last_scraped=excluded.last_scraped, "
            "last_status=excluded.last_status, last_error=excluded.last_error, "
            "product_count=excluded.product_count, bestseller_collection=excluded.bestseller_collection",
            (domain, taken_at, "ok", None, len(products), bs_handle),
        )
        conn.commit()
    log(f"[{domain}] {len(products)} products, best-seller collection: {bs_handle or 'none found'}")
    return len(products)


def run(conn, domains, workers=4, log=print):
    domains = [normalize_domain(d) for d in domains if d.strip()]
    with db.write_lock():
        run_id = conn.execute("INSERT INTO runs(started_at) VALUES(?)", (now_iso(),)).lastrowid
        conn.commit()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        total = sum(pool.map(lambda d: scrape_store(conn, run_id, d, log), domains))
    with db.write_lock():
        conn.execute("UPDATE runs SET finished_at=?, status='done' WHERE id=?", (now_iso(), run_id))
        conn.commit()
    log(f"Run {run_id} finished: {total} products from {len(domains)} stores")
    return run_id


def read_store_file(path):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
