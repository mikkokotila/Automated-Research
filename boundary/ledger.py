"""Crash-safe rolling-window reservations, shared by service processes."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from .policy import TOKEN_LIMIT, WINDOW_NS, BudgetBlocked, StateBlocked

MAX_TOKEN_TTL_S = 7 * 86_400
OUTCOME_KEY = "last_provider_outcome"


def system_clock():
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return boot, time.monotonic_ns()


class Ledger:
    def __init__(self, path, clock=system_clock):
        self.path = Path(path).absolute()
        self.clock = clock
        with self._connect() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateBlocked("Ledger integrity check failed")
            row = db.execute("SELECT version, ceiling, window_ns FROM policy").fetchone()
            if row != (1, TOKEN_LIMIT, WINDOW_NS):
                raise StateBlocked("Ledger policy mismatch")
            db.execute("""CREATE TABLE IF NOT EXISTS run_tokens(
                id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
                expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE TABLE IF NOT EXISTS notes(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.commit()

    @classmethod
    def initialize(cls, path, clock=system_clock):
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Operator-only first initialization; never truncate existing state.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        boot, mono = clock()
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE policy(version INTEGER NOT NULL, ceiling INTEGER NOT NULL,
                    window_ns INTEGER NOT NULL, boot TEXT NOT NULL, mono INTEGER NOT NULL,
                    elapsed INTEGER NOT NULL, halted TEXT NOT NULL);
                CREATE TABLE entries(id TEXT PRIMARY KEY, reserved INTEGER NOT NULL CHECK(reserved>0),
                    charged INTEGER NOT NULL CHECK(charged>=0), started INTEGER NOT NULL,
                    finished INTEGER, usage TEXT, CHECK(charged<=reserved));
                CREATE INDEX entries_finished ON entries(finished);
            """)
            db.execute("INSERT INTO policy VALUES(1,?,?,?,?,0,'')", (TOKEN_LIMIT, WINDOW_NS, boot, mono))
        return cls(path, clock)

    @contextmanager
    def _connect(self):
        db = None
        try:
            if self.path.is_symlink():
                raise StateBlocked("Ledger symlinks are not accepted")
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except (sqlite3.Error, OSError) as exc:
            raise StateBlocked("Ledger unavailable; no request was authorized") from exc
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _transaction(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT version,ceiling,window_ns,boot,mono,elapsed,halted FROM policy").fetchone()
                if row is None or row[:3] != (1, TOKEN_LIMIT, WINDOW_NS):
                    raise StateBlocked("Ledger policy mismatch")
                boot, mono = self.clock()
                if type(mono) is not int or mono < 0:
                    raise StateBlocked("Invalid trusted clock")
                previous_boot, previous_mono, elapsed, halted = row[3:]
                if boot == previous_boot:
                    if mono < previous_mono:
                        raise StateBlocked("Monotonic clock went backwards")
                    elapsed += mono - previous_mono
                # On a new kernel boot, credit no downtime: conservative expiry.
                db.execute("UPDATE policy SET boot=?,mono=?,elapsed=?", (boot, mono, elapsed))
                yield db, elapsed, halted
                db.commit()
            except BudgetBlocked:
                db.commit()  # retain trusted clock progress even when admission is denied
                raise
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _used(db, now):
        return db.execute("SELECT COALESCE(SUM(charged),0) FROM entries WHERE finished IS NULL OR finished>?",
                          (now-WINDOW_NS,)).fetchone()[0]

    def reserve(self, amount):
        if type(amount) is not int or not 1 <= amount <= TOKEN_LIMIT:
            raise BudgetBlocked("Invalid reservation")
        with self._transaction() as (db, now, halted):
            if halted:
                raise StateBlocked("Request service halted: " + halted)
            if self._used(db, now) + amount > TOKEN_LIMIT:
                raise BudgetBlocked("200000000-token rolling 24-hour ceiling reached; request not sent")
            ident = uuid.uuid4().hex
            db.execute("INSERT INTO entries VALUES(?,?,?,?,NULL,NULL)", (ident, amount, amount, now))
            return ident

    def settle(self, ident, amount, usage):
        with self._transaction() as (db, now, _):
            row = db.execute("SELECT reserved,finished FROM entries WHERE id=?", (ident,)).fetchone()
            if row is None or row[1] is not None:
                raise StateBlocked("Unknown or already settled reservation")
            if type(amount) is not int or not 0 <= amount <= row[0]:
                raise StateBlocked("Usage exceeds reserved capacity")
            # Count tokens until 24h after completion, not request admission.
            db.execute("UPDATE entries SET charged=?,finished=?,usage=? WHERE id=?", (amount, now, usage, ident))

    def halt(self, reason):
        with self._transaction() as (db, _, __):
            db.execute("UPDATE policy SET halted=?", (reason,))

    def mint_run_token(self, ttl_s):
        """Short-lived worker credential. The secret is shown once, never stored."""
        if type(ttl_s) is not int or not 1 <= ttl_s <= MAX_TOKEN_TTL_S:
            raise StateBlocked("Invalid token TTL")
        with self._transaction() as (db, now, halted):
            if halted:
                raise StateBlocked("Request service halted: " + halted)
            db.execute("DELETE FROM run_tokens WHERE expires<=?", (now,))
            ident, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
            digest = hashlib.sha256(secret.encode()).hexdigest()
            db.execute("INSERT INTO run_tokens VALUES(?,?,?,0)",
                       (ident, digest, now + ttl_s * 1_000_000_000))
            return ident, secret

    def check_run_token(self, secret):
        with self._transaction() as (db, now, halted):
            if halted or not secret:
                return False
            digest = hashlib.sha256(secret.encode()).hexdigest()
            row = db.execute("SELECT expires,revoked FROM run_tokens WHERE digest=?",
                             (digest,)).fetchone()
            return row is not None and not row[1] and row[0] > now

    def revoke_run_token(self, ident):
        with self._transaction() as (db, _, __):
            row = db.execute("UPDATE run_tokens SET revoked=1 WHERE id=?", (ident,))
            return row.rowcount > 0

    def note_outcome(self, outcome):
        """Record the last provider interaction for preflight. Never secrets."""
        with self._transaction() as (db, _, __):
            db.execute("INSERT OR REPLACE INTO notes VALUES(?,?)",
                       (OUTCOME_KEY, json.dumps(outcome, sort_keys=True)))

    def status(self):
        with self._transaction() as (db, now, halted):
            used = self._used(db, now)
            pending = db.execute("SELECT COUNT(*) FROM entries WHERE finished IS NULL").fetchone()[0]
            row = db.execute("SELECT value FROM notes WHERE key=?", (OUTCOME_KEY,)).fetchone()
            return {"ceiling": TOKEN_LIMIT, "window_seconds": WINDOW_NS//1_000_000_000,
                    "charged_and_reserved": used, "remaining": TOKEN_LIMIT-used,
                    "pending_requests": pending, "halted": halted,
                    "last_provider_outcome": json.loads(row[0]) if row else None}
