"""SQLite-lager for målinger, priser og elbil-opladning."""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_reading (
    ts INTEGER PRIMARY KEY,          -- unix-tid (sek)
    lifetime_wh REAL NOT NULL        -- Wall Connectorens samlede tæller
);
CREATE TABLE IF NOT EXISTS ev_interval (
    start INTEGER PRIMARY KEY,       -- start på 15-min interval (unix sek, UTC)
    kwh REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meter (
    start INTEGER PRIMARY KEY,       -- start på måleinterval (unix sek)
    seconds INTEGER NOT NULL,        -- intervallets længde (900 eller 3600)
    kwh REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spot (
    start INTEGER PRIMARY KEY,       -- start på prisinterval (unix sek)
    seconds INTEGER NOT NULL,
    dkk_per_mwh REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql, rows):
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- key/value ----
    def get_kv(self, key, default=None):
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_kv(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                     (key, json.dumps(value)))

    # ---- Wall Connector ----
    def last_ev_reading(self):
        rows = self.query("SELECT ts, lifetime_wh FROM ev_reading ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def first_ev_ts(self):
        """Tidspunkt hvorfra elbil-opladning er målt (ikke skønnet)."""
        for sql in ("SELECT MIN(ts) AS t FROM ev_reading",
                    "SELECT MIN(start) AS t FROM ev_interval"):
            rows = self.query(sql)
            if rows and rows[0]["t"] is not None:
                return rows[0]["t"]
        return None

    def add_ev_reading(self, ts, lifetime_wh):
        self.execute("INSERT OR REPLACE INTO ev_reading(ts, lifetime_wh) VALUES (?, ?)",
                     (ts, lifetime_wh))

    def add_ev_kwh(self, buckets):
        """buckets: dict {interval_start: kwh} – lægges oven i eksisterende."""
        with self.lock:
            for start, kwh in buckets.items():
                self.conn.execute(
                    "INSERT INTO ev_interval(start, kwh) VALUES (?, ?) "
                    "ON CONFLICT(start) DO UPDATE SET kwh = kwh + excluded.kwh",
                    (start, kwh))
            self.conn.commit()

    def ev_intervals(self, t0, t1):
        return self.query("SELECT start, kwh FROM ev_interval WHERE start>=? AND start<? "
                          "ORDER BY start", (t0, t1))

    # ---- Måler (Eloverblik) ----
    def upsert_meter(self, rows):
        self.executemany("INSERT OR REPLACE INTO meter(start, seconds, kwh) VALUES (?, ?, ?)", rows)

    def meter(self, t0, t1):
        return self.query("SELECT start, seconds, kwh FROM meter WHERE start>=? AND start<? "
                          "ORDER BY start", (t0, t1))

    def last_meter_end(self):
        rows = self.query("SELECT MAX(start + seconds) AS t FROM meter")
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    # ---- Spotpriser ----
    def upsert_spot(self, rows):
        self.executemany("INSERT OR REPLACE INTO spot(start, seconds, dkk_per_mwh) VALUES (?, ?, ?)", rows)

    def spot(self, t0, t1):
        return self.query("SELECT start, seconds, dkk_per_mwh FROM spot WHERE start>=? AND start<? "
                          "ORDER BY start", (t0, t1))

    def last_spot_end(self):
        rows = self.query("SELECT MAX(start + seconds) AS t FROM spot")
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None
