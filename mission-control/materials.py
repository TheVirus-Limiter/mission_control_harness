"""PILLAR 1 — Materials.

Typed envelopes that move between stages, plus the SQLite store that persists
every stage output and every checkpoint result.

This module owns ALL input and output. Agents never touch storage and never
touch each other -- the harness reads an output here and hands the next agent a
fresh task. That single ownership is what makes the system replayable: a run is
nothing more than the rows in this store, so a killed-and-reloaded process can
re-render history and resume from any checkpoint.

There is NO worker logic in this file. It is pure material handling.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Envelope: the typed package the harness moves between stages.
# ---------------------------------------------------------------------------
@dataclass
class Envelope:
    """A typed package handed between stages.

    Agents read ``payload``. The harness moves the whole envelope and records
    ``meta`` (telemetry: tokens, seconds, attempt number).
    """

    run_id: str
    stage: str
    payload: dict
    meta: dict = field(default_factory=dict)

    def with_payload(self, payload: dict) -> "Envelope":
        return Envelope(self.run_id, self.stage, payload, dict(self.meta))

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "payload": self.payload,
            "meta": self.meta,
        }


# ---------------------------------------------------------------------------
# Store: the single source of truth. Two core tables, both append-friendly.
# ---------------------------------------------------------------------------
class Store:
    """SQLite-backed event log + output store.

    Tables
    ------
    events(run_id, ts, stage, kind, name, ok, detail)
        One row per checkpoint result, alarm, gate decision, approval, post...
        ``kind`` names the category; ``detail`` is a JSON blob of evidence.
    outputs(run_id, stage, payload)
        The latest output of each stage, as JSON. Replay reads from here.

    Every method is a thin, deterministic read or write. The timeline and the
    replay logic are *pure reads* over these tables.
    """

    def __init__(self, path: str = "mission.db"):
        self.path = path
        self._lock = threading.Lock()
        # check_same_thread=False so the FastAPI dashboard can read concurrently;
        # the RLock below serialises *all* access (reads and writes) on this
        # connection, and WAL + busy_timeout handle the engine and dashboard
        # holding separate connections to the same file.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=10000")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass  # e.g. :memory: doesn't support WAL -- fall back silently
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id  TEXT    NOT NULL,
                    ts      REAL    NOT NULL,
                    stage   TEXT    NOT NULL,
                    kind    TEXT    NOT NULL,
                    name    TEXT,
                    ok      INTEGER,            -- 1 / 0 / NULL
                    detail  TEXT                -- JSON
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    run_id  TEXT    NOT NULL,
                    stage   TEXT    NOT NULL,
                    ts      REAL    NOT NULL,
                    payload TEXT    NOT NULL,    -- JSON
                    PRIMARY KEY (run_id, stage)
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                """
            )
            self._conn.commit()

    # -- writes --------------------------------------------------------------
    def log(
        self,
        run_id: str,
        stage: str,
        kind: str,
        name: Optional[str] = None,
        ok: Optional[bool] = None,
        detail: Any = None,
    ) -> None:
        """Append one event. ``kind`` is the category (check/alarm/gate/...)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(run_id, ts, stage, kind, name, ok, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    time.time(),
                    stage,
                    kind,
                    name,
                    None if ok is None else int(bool(ok)),
                    json.dumps(detail, default=str) if detail is not None else None,
                ),
            )
            self._conn.commit()

    def save_output(self, env: Envelope) -> None:
        """Persist (or replace) the latest output for a stage."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO outputs(run_id, stage, ts, payload) "
                "VALUES (?,?,?,?)",
                (env.run_id, env.stage, time.time(), json.dumps(env.payload, default=str)),
            )
            self._conn.commit()

    # -- reads (timeline + replay are built on these). Locked too, because the
    #    one shared connection cannot interleave a read cursor with a write. ----
    def load_output(self, run_id: str, stage: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM outputs WHERE run_id=? AND stage=?",
                (run_id, stage),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_envelope(self, run_id: str, stage: str) -> Optional[Envelope]:
        payload = self.load_output(run_id, stage)
        if payload is None:
            return None
        return Envelope(run_id, stage, payload)

    def has_output(self, run_id: str, stage: str) -> bool:
        return self.load_output(run_id, stage) is not None

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, stage, kind, name, ok, detail FROM events "
                "WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "stage": r["stage"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "ok": None if r["ok"] is None else bool(r["ok"]),
                    "detail": json.loads(r["detail"]) if r["detail"] else None,
                }
            )
        return out

    def stages_with_output(self, run_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage FROM outputs WHERE run_id=? ORDER BY ts", (run_id,)
            ).fetchall()
        return [r["stage"] for r in rows]

    def runs(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, MAX(ts) AS last FROM events GROUP BY run_id ORDER BY last DESC"
            ).fetchall()
        return [r["run_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
