import os
import sqlite3
import threading

DEFAULT_PATH = os.environ.get("SHOPSCOUT_DB", os.path.join("data", "shopscout.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS stores (
    domain TEXT PRIMARY KEY,
    last_scraped TEXT,
    last_status TEXT,
    last_error TEXT,
    product_count INTEGER,
    bestseller_collection TEXT
);
CREATE TABLE IF NOT EXISTS products (
    store TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    handle TEXT,
    title TEXT,
    vendor TEXT,
    product_type TEXT,
    tags TEXT,
    image TEXT,
    created_at TEXT,
    published_at TEXT,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (store, product_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id INTEGER NOT NULL,
    store TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    taken_at TEXT NOT NULL,
    price_min REAL,
    price_max REAL,
    compare_at_max REAL,
    variants INTEGER,
    variants_available INTEGER,
    bestseller_rank INTEGER,
    PRIMARY KEY (run_id, store, product_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_product ON snapshots (store, product_id, run_id);
CREATE TABLE IF NOT EXISTS costs (
    store TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    unit_cost REAL,
    shipping_cost REAL,
    PRIMARY KEY (store, product_id)
);
"""

_lock = threading.Lock()


def connect(path=DEFAULT_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def write_lock():
    """SQLite allows one writer; scraper threads share this lock."""
    return _lock
