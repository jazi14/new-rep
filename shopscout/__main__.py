import argparse

from . import db, scraper, server


def main():
    ap = argparse.ArgumentParser(prog="shopscout", description="Shopify product research")
    ap.add_argument("--db", default=db.DEFAULT_PATH, help="SQLite file (default: %(default)s)")
    ap.add_argument("--stores", default="stores.txt", help="store list file (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scrape", help="take one snapshot of every store in the list")
    s.add_argument("--workers", type=int, default=4)
    v = sub.add_parser("serve", help="run the dashboard")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    conn = db.connect(args.db)
    if args.cmd == "scrape":
        scraper.run(conn, scraper.read_store_file(args.stores), workers=args.workers)
    else:
        server.serve(conn, args.stores, args.host, args.port)


if __name__ == "__main__":
    main()
