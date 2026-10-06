"""Dashboard web server (standard library only)."""
import json
import os
import threading
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, scraper, signals

STATIC = os.path.join(os.path.dirname(__file__), "static")
SORT_KEYS = {"score", "price_min", "discount_pct", "age_days", "bestseller_rank", "sold_out_since_last",
             "stock_pct", "margin_pct", "price_change_pct", "title", "store"}


class State:
    def __init__(self, conn, store_file):
        self.conn = conn
        self.store_file = store_file
        self.log = deque(maxlen=200)
        self.running = False
        self.lock = threading.Lock()

    def start_scrape(self, domains):
        with self.lock:
            if self.running:
                return False
            self.running = True

        def work():
            try:
                scraper.run(self.conn, domains, log=self.log.append)
            except Exception as e:  # keep the server alive and surface the error
                self.log.append(f"Run crashed: {e!r}")
            finally:
                self.running = False

        threading.Thread(target=work, daemon=True).start()
        return True


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def filter_and_sort(rows, q):
    text = (q.get("q") or "").lower()
    store = q.get("store") or ""
    ptype = q.get("type") or ""
    min_price, max_price = _num(q.get("min_price")), _num(q.get("max_price"))
    max_age = _num(q.get("max_age"))
    if text:
        rows = [r for r in rows if text in f"{r['title']} {r['vendor']} {r['tags']} {r['product_type']}".lower()]
    if store:
        rows = [r for r in rows if r["store"] == store]
    if ptype:
        rows = [r for r in rows if (r["product_type"] or "") == ptype]
    if min_price is not None:
        rows = [r for r in rows if r["price_min"] is not None and r["price_min"] >= min_price]
    if max_price is not None:
        rows = [r for r in rows if r["price_min"] is not None and r["price_min"] <= max_price]
    if max_age is not None:
        rows = [r for r in rows if r["age_days"] is not None and r["age_days"] <= max_age]
    if q.get("bestsellers") == "1":
        rows = [r for r in rows if r["bestseller_rank"]]
    if q.get("in_stock") == "1":
        rows = [r for r in rows if r["variants_available"]]
    if q.get("on_sale") == "1":
        rows = [r for r in rows if r["discount_pct"]]
    if q.get("has_cost") == "1":
        rows = [r for r in rows if r["unit_cost"] is not None]

    key = q.get("sort") if q.get("sort") in SORT_KEYS else "score"
    desc = q.get("dir", "desc") == "desc"
    present = [r for r in rows if r[key] is not None]
    missing = [r for r in rows if r[key] is None]
    present.sort(key=lambda r: (r[key].lower() if isinstance(r[key], str) else r[key]), reverse=desc)
    return present + missing  # rows without the value always go last


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/api/products":
                rows = signals.products(state.conn, fee_pct=_num(q.get("fee_pct")) or 0.0)
                facets = {
                    "stores": sorted({r["store"] for r in rows}),
                    "types": sorted({r["product_type"] for r in rows if r["product_type"]}),
                }
                rows = filter_and_sort(rows, q)
                limit = int(q.get("limit") or 300)
                return self._send(200, {"total": len(rows), "rows": rows[:limit], "facets": facets})
            if url.path == "/api/status":
                stores = [dict(r) for r in state.conn.execute("SELECT * FROM stores ORDER BY domain")]
                runs = [dict(r) for r in state.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")]
                return self._send(200, {"running": state.running, "log": list(state.log)[-30:],
                                        "stores": stores, "runs": runs,
                                        "store_list": scraper.read_store_file(state.store_file)
                                        if os.path.exists(state.store_file) else []})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            body = self._body()
            if path == "/api/scrape":
                domains = [d for d in body.get("stores", []) if d.strip() and not d.strip().startswith("#")]
                if not domains:
                    return self._send(400, {"error": "no stores given"})
                with open(state.store_file, "w") as f:
                    f.write("\n".join(scraper.normalize_domain(d) for d in domains) + "\n")
                ok = state.start_scrape(domains)
                return self._send(202 if ok else 409, {"started": ok})
            if path == "/api/cost":
                with db.write_lock():
                    state.conn.execute(
                        "INSERT INTO costs(store, product_id, unit_cost, shipping_cost) VALUES(?,?,?,?) "
                        "ON CONFLICT(store, product_id) DO UPDATE SET unit_cost=excluded.unit_cost, "
                        "shipping_cost=excluded.shipping_cost",
                        (body["store"], int(body["product_id"]), _num(body.get("unit_cost")),
                         _num(body.get("shipping_cost"))),
                    )
                    state.conn.commit()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def serve(conn, store_file, host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), make_handler(State(conn, store_file)))
    print(f"ShopScout dashboard on http://{host}:{port}")
    httpd.serve_forever()
