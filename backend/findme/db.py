from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 2


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

        CREATE TABLE IF NOT EXISTS index_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS search_profiles (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('none', 'default', 'custom')),
          version INTEGER NOT NULL DEFAULT 1,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_search_profiles_kind_builtin
          ON search_profiles(kind)
          WHERE kind IN ('none', 'default');

        CREATE TABLE IF NOT EXISTS search_profile_folder_settings (
          profile_id TEXT NOT NULL REFERENCES search_profiles(id) ON DELETE CASCADE,
          source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          directory TEXT NOT NULL,
          quality_override INTEGER CHECK (quality_override BETWEEN 0 AND 10),
          selection_state TEXT CHECK (selection_state IN ('whitelist', 'blacklist')),
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (profile_id, source_id, directory),
          CHECK (quality_override IS NOT NULL OR selection_state IS NOT NULL)
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
    _ensure_builtin_search_profiles(db)
    _migrate_folder_quality_overrides(db)


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_index_metadata(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute(
        "SELECT value FROM index_metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return str(row["value"]) if row is not None else None


def set_index_metadata(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO index_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def compute_index_fingerprint(db: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    rows = db.execute(
        """
        SELECT
          assets.asset_id,
          assets.source_id,
          sources.display_name,
          sources.priority,
          assets.relative_path,
          assets.absolute_path,
          assets.filename,
          assets.stem,
          assets.extension,
          assets.directory,
          assets.size_bytes,
          assets.mtime_ns,
          assets.media_type,
          assets.thumbnail_path,
          assets.thumbnail_width,
          assets.thumbnail_height
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        ORDER BY assets.source_id, assets.relative_path COLLATE NOCASE
        """
    )
    for row in rows:
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def refresh_index_fingerprint(db: sqlite3.Connection) -> str:
    fingerprint = compute_index_fingerprint(db)
    set_index_metadata(db, "index_fingerprint", fingerprint)
    return fingerprint


def mark_search_corpus_current(db: sqlite3.Connection) -> None:
    fingerprint = get_index_metadata(db, "index_fingerprint")
    if fingerprint is None:
        fingerprint = refresh_index_fingerprint(db)
    set_index_metadata(db, "search_corpus_fingerprint", fingerprint)


def search_corpus_is_stale(db: sqlite3.Connection) -> bool:
    index_fingerprint = get_index_metadata(db, "index_fingerprint")
    search_fingerprint = get_index_metadata(db, "search_corpus_fingerprint")
    return index_fingerprint != search_fingerprint


def _ensure_builtin_search_profiles(db: sqlite3.Connection) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO search_profiles(id, name, kind, version, sort_order)
        VALUES ('none', 'None', 'none', 1, 0)
        """
    )
    db.execute(
        """
        INSERT OR IGNORE INTO search_profiles(id, name, kind, version, sort_order)
        VALUES ('default', 'Default', 'default', 1, 1)
        """
    )


def _migrate_folder_quality_overrides(db: sqlite3.Connection) -> None:
    legacy_exists = db.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'folder_quality_overrides'
        """
    ).fetchone()
    if legacy_exists is None:
        return
    db.execute(
        """
        INSERT INTO search_profile_folder_settings(
          profile_id,
          source_id,
          directory,
          quality_override,
          selection_state,
          updated_at
        )
        SELECT 'default', source_id, directory, quality, NULL, updated_at
        FROM folder_quality_overrides
        WHERE quality != 5
        ON CONFLICT(profile_id, source_id, directory) DO UPDATE SET
          quality_override = excluded.quality_override,
          updated_at = excluded.updated_at
        """
    )
    db.execute("DROP TABLE folder_quality_overrides")
