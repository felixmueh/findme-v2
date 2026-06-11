from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def connect_metadata(path: Path) -> Iterator[sqlite3.Connection]:
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


def initialize_metadata_database(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_files (
          file_id TEXT PRIMARY KEY,
          hash_algorithm TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (hash_algorithm, content_hash)
        );

        CREATE TABLE IF NOT EXISTS metadata_file_locations (
          source_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          file_id TEXT NOT NULL REFERENCES metadata_files(file_id) ON DELETE CASCADE,
          asset_id TEXT,
          size_bytes INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          missing_at TEXT,
          PRIMARY KEY (source_id, relative_path)
        );

        CREATE INDEX IF NOT EXISTS idx_metadata_file_locations_file
          ON metadata_file_locations(file_id);

        CREATE TABLE IF NOT EXISTS tags (
          tag_id TEXT PRIMARY KEY,
          slug TEXT NOT NULL UNIQUE,
          label TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS file_tags (
          file_id TEXT NOT NULL REFERENCES metadata_files(file_id) ON DELETE CASCADE,
          tag_id TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
          sort_order INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (file_id, tag_id)
        );

        CREATE INDEX IF NOT EXISTS idx_file_tags_tag
          ON file_tags(tag_id);
        """
    )
