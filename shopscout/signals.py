"""Turn stored snapshots into per-product research signals.

The score is a plain heuristic. Each component is returned separately so the
dashboard can show why a product ranks where it does.
"""
from datetime import datetime, timezone

QUERY = """
WITH ranked AS (
    SELECT s.*,
           ROW_NUMBER() OVER (PARTITION BY store, product_id ORDER BY run_id DESC) AS rn_desc,
           ROW_NUMBER() OVER (PARTITION BY store, product_id ORDER BY run_id ASC)  AS rn_asc,
           COUNT(*)     OVER (PARTITION BY store, product_id)                      AS n_snapshots
    FROM snapshots s
)
SELECT p.store, p.product_id, p.handle, p.title, p.vendor, p.product_type, p.tags, p.image,
       p.published_at, p.first_seen, p.last_seen,
       cur.price_min, cur.price_max, cur.compare_at_max, cur.variants, cur.variants_available,
       cur.bestseller_rank, cur.n_snapshots, cur.run_id AS last_run,
       prev.variants_available AS prev_available, prev.price_min AS prev_price,
       first.price_min AS first_price,
       c.unit_cost, c.shipping_cost
FROM products p
JOIN ranked cur   ON cur.store = p.store AND cur.product_id = p.product_id AND cur.rn_desc = 1
LEFT JOIN ranked prev  ON prev.store = p.store AND prev.product_id = p.product_id AND prev.rn_desc = 2
LEFT JOIN ranked first ON first.store = p.store AND first.product_id = p.product_id AND first.rn_asc = 1
LEFT JOIN costs c ON c.store = p.store AND c.product_id = p.product_id
"""


def _days_since(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _pct(new, old):
    if new is None or not old:
        return None
    return round((new - old) / old * 100, 1)


def compute(row, fee_pct=0.0):
    r = dict(row)
    price = r["price_min"]
    r["url"] = f"https://{r['store']}/products/{r['handle']}"
    r["age_days"] = _days_since(r["published_at"])

    cmp = r["compare_at_max"]
    r["discount_pct"] = round((cmp - price) / cmp * 100, 1) if cmp and price and cmp > price else None
    r["price_change_pct"] = _pct(price, r["first_price"])
    r["stock_pct"] = round(r["variants_available"] / r["variants"] * 100) if r["variants"] else None
    prev = r["prev_available"]
    r["sold_out_since_last"] = max(0, prev - r["variants_available"]) if prev is not None else None
    r["restocked_since_last"] = max(0, r["variants_available"] - prev) if prev is not None else None

    if r["unit_cost"] is not None and price:
        landed = r["unit_cost"] + (r["shipping_cost"] or 0) + price * fee_pct / 100
        r["margin"] = round(price - landed, 2)
        r["margin_pct"] = round((price - landed) / price * 100, 1)
    else:
        r["margin"] = r["margin_pct"] = None

    parts = {}
    rank = r["bestseller_rank"]
    if rank:
        parts["bestseller"] = 40 if rank <= 10 else 30 if rank <= 25 else 20 if rank <= 50 else 10
    if r["sold_out_since_last"]:
        parts["selling_out"] = min(30, 10 * r["sold_out_since_last"])
    if r["stock_pct"] is not None and 0 < r["stock_pct"] <= 50 and r["variants"] > 1:
        parts["low_stock"] = 10
    if r["age_days"] is not None:
        if r["age_days"] <= 30:
            parts["new"] = 20
        elif r["age_days"] <= 90:
            parts["new"] = 10
    if r["margin_pct"] is not None and r["margin_pct"] >= 40:
        parts["margin"] = 10
    r["score_parts"] = parts
    r["score"] = sum(parts.values())
    return r


def products(conn, fee_pct=0.0):
    return [compute(row, fee_pct) for row in conn.execute(QUERY)]
