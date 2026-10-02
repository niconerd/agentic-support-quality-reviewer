import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .schema import Case

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def connect():
    path = Path(os.environ.get("REVIEWER_DB", str(ROOT / "data/reviewer.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT,
          status TEXT NOT NULL, mode TEXT NOT NULL, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results (
          run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL,
          case_payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          evaluation TEXT, error TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
          cached_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
          cost REAL, response_id TEXT, response_model TEXT,
          PRIMARY KEY(run_id, case_id));
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(results)")}
        for column in ("model_output", "diagnostics", "check_results"):
            if column not in columns:
                db.execute(f"ALTER TABLE results ADD COLUMN {column} TEXT")
        # ponytail: a single local worker; use a durable queue for multi-process deployment.
        db.execute("UPDATE results SET status='failed', error='Interrupted by application restart' WHERE status IN ('pending','running') AND run_id IN (SELECT id FROM runs WHERE status='running')")
        db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        for payload in json.loads((ROOT / "data/tau3-retail-50.json").read_text()):
            case = Case.model_validate(payload)
            db.execute("INSERT OR IGNORE INTO cases VALUES (?, ?)", (case.id, case.model_dump_json()))


def get_cases():
    with connect() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM cases ORDER BY id")]
