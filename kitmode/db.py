"""SQLite storage with atomic updates and forward-only schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,data TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS habits(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,title TEXT NOT NULL,sensitive INTEGER NOT NULL,archived INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(habit_id TEXT REFERENCES habits(id) ON DELETE CASCADE,effective TEXT NOT NULL,spec TEXT NOT NULL,PRIMARY KEY(habit_id,effective));
CREATE TABLE IF NOT EXISTS records(habit_id TEXT REFERENCES habits(id) ON DELETE CASCADE,day TEXT NOT NULL,status TEXT NOT NULL,value REAL NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',verification TEXT NOT NULL DEFAULT 'none',evidence TEXT NOT NULL DEFAULT '{}',snapshot TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(habit_id,day));
CREATE TABLE IF NOT EXISTS actions(user_id INTEGER,key TEXT,kind TEXT,habit_id TEXT,day TEXT,before TEXT,after TEXT,created_at TEXT,PRIMARY KEY(user_id,key));
CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS buttons(token TEXT PRIMARY KEY,user_id INTEGER,payload TEXT,expires TEXT,used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pauses(habit_id TEXT REFERENCES habits(id) ON DELETE CASCADE,start TEXT,end TEXT,PRIMARY KEY(habit_id,start));
CREATE TABLE IF NOT EXISTS archives(habit_id TEXT PRIMARY KEY REFERENCES habits(id) ON DELETE CASCADE,day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id INTEGER,due TEXT,day TEXT,kind TEXT,payload TEXT,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rewards(habit_id TEXT REFERENCES habits(id) ON DELETE CASCADE,opportunity TEXT,user_id INTEGER,xp INTEGER NOT NULL,PRIMARY KEY(habit_id,opportunity));
CREATE TABLE IF NOT EXISTS system(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS habits_owner ON habits(user_id);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,due);
CREATE INDEX IF NOT EXISTS actions_record ON actions(habit_id,day,created_at);
INSERT OR IGNORE INTO migrations VALUES(1);
INSERT OR IGNORE INTO migrations VALUES(2);
"""


class DB:
    def __init__(self, path: str | Path = "data/kitmode.sqlite3") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'").fetchone():
            latest = self.conn.execute("SELECT max(version) FROM migrations").fetchone()[0]
            if latest and latest > 2:
                self.conn.close()
                raise RuntimeError("Database is from a newer KitMode version; upgrade the application")
        self.conn.executescript(SCHEMA)
        latest = self.conn.execute("SELECT max(version) FROM migrations").fetchone()[0]
        if latest > 2:
            self.conn.close()
            raise RuntimeError("Database is from a newer KitMode version; upgrade the application")
        self.depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        depth = self.depth
        self.conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT tx{depth}")
        self.depth += 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO tx{depth}")
            if depth:
                self.conn.execute(f"RELEASE tx{depth}")
            raise
        else:
            self.conn.execute("COMMIT" if depth == 0 else f"RELEASE tx{depth}")
        finally:
            self.depth -= 1

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
