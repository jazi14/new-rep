# ShopScout

Finds product ideas by tracking public catalog data from Shopify stores. It takes snapshots of each store's products over time and ranks them in a local web dashboard.

No third-party packages. Needs Python 3.9+.

## Run it

```bash
python3 -m shopscout serve          # dashboard at http://127.0.0.1:8000
```

Put store domains in the Stores box and click **Scrape now**, or from the command line:

```bash
python3 -m shopscout scrape         # reads stores.txt, one domain per line
```

Data goes to `data/shopscout.db` (SQLite). Run a scrape once or twice a day, for example with cron:

```
0 8,20 * * * cd /path/to/new-rep && python3 -m shopscout scrape
```

## Where the data comes from

| Source | Gives |
| --- | --- |
| `/products.json` | title, type, vendor, tags, image, publish date, every variant's price, compare-at price, availability |
| `/collections.json` | used to find the store's own best-seller collection (handle or title containing "best seller", "top seller", "trending" and similar) |
| `/collections/<best-seller handle>/products.json` | position in that collection, used as the best-seller rank |

Every path is checked against the store's `robots.txt` first. Shopify's default robots.txt blocks `?sort_by=best-selling`, so the tool does not use it. Requests to one store are spaced 1.5 s apart and 429 responses wait for `Retry-After`.

## Signals and score

| Column | Meaning |
| --- | --- |
| Best-seller # | Position in the store's own best-seller collection. Blank if the store has none. |
| Sold out since last | Variants (sizes, colors) that went from available to unavailable between the last two scrapes. Needs two runs. |
| In stock | Available variants / total variants right now. |
| Age | Days since the product was published. |
| Discount | Compare-at price vs. current price. |
| Price chg | Change since the first snapshot. |
| Margin | Price minus your unit cost, shipping, and the fee % you set. Only shown after you type your cost into the row. |

Score (heuristic, every part shown on the row):

- best-seller rank 1-10: +40, 11-25: +30, 26-50: +20, lower: +10
- variants sold out since last scrape: +10 each, max +30
- 50% or fewer variants in stock (multi-variant products): +10
- published in the last 30 days: +20, last 90 days: +10
- margin 40% or more: +10

## Limits

- Shopify stores only. Some stores block `/products.json` (403) or rate-limit (429); these show as errors in the Stores panel.
- Shopify does not publish sales counts. Best-seller rank and sell-out tracking are proxies, not units sold.
- Supplier cost is not scraped. Enter it yourself per product to get margin.
- At most 10,000 products per store per run (`MAX_PAGES` in `shopscout/scraper.py`).
- Check each store's terms of service before using its data commercially.
