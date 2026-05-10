from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 1


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_database(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          root TEXT NOT NULL,
          priority INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS folder_quality_overrides (
          source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          directory TEXT NOT NULL,
          quality INTEGER NOT NULL CHECK (quality BETWEEN 0 AND 10),
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (source_id, directory)
        );

        CREATE TABLE IF NOT EXISTS assets (
          asset_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          relative_path TEXT NOT NULL,
          absolute_path TEXT NOT NULL,
          filename TEXT NOT NULL,
          stem TEXT NOT NULL,
          extension TEXT NOT NULL,
          directory TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          media_type TEXT NOT NULL,
          thumbnail_path TEXT,
          thumbnail_width INTEGER,
          thumbnail_height INTEGER,
          stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
          indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (source_id, relative_path)
        );

        CREATE INDEX IF NOT EXISTS idx_assets_source_stale
          ON assets (source_id, stale);

        CREATE INDEX IF NOT EXISTS idx_assets_directory
          ON assets (source_id, directory, stale);

        CREATE VIRTUAL TABLE IF NOT EXISTS asset_search USING fts5(
          asset_id UNINDEXED,
          source_id UNINDEXED,
          filename,
          stem,
          directory,
          relative_path,
          tokenize = 'unicode61 tokenchars ''_-'''
        );
        """
    )
    db.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    _ensure_column(db, "assets", "thumbnail_path", "TEXT")
    _ensure_column(db, "assets", "thumbnail_width", "INTEGER")
    _ensure_column(db, "assets", "thumbnail_height", "INTEGER")


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
