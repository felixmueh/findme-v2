from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from findme.config import AppConfig, SourceConfig
from findme.identity import asset_id_for


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".webm", ".mp4"})
IGNORED_DIRS = frozenset({".findme"})


@dataclass(frozen=True)
class IndexSummary:
    scanned_files: int = 0
    upserted_assets: int = 0
    marked_stale: int = 0


def index_sources(db: sqlite3.Connection, config: AppConfig) -> IndexSummary:
    scanned = 0
    upserted = 0
    stale = 0

    for source in config.sources:
        db.execute(
            """
            INSERT INTO sources(id, display_name, root, priority)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              display_name = excluded.display_name,
              root = excluded.root,
              priority = excluded.priority
            """,
            (source.id, source.display_name, str(source.root), source.priority),
        )

        seen_paths = set()
        for file_path in _iter_source_files(source):
            relative_path = _relative_posix(file_path, source.root)
            seen_paths.add(relative_path)
            scanned += 1
            upserted += _upsert_asset(db, source, file_path, relative_path)

        stale += _mark_missing_stale(db, source.id, seen_paths)

    return IndexSummary(scanned_files=scanned, upserted_assets=upserted, marked_stale=stale)


def _iter_source_files(source: SourceConfig) -> list[Path]:
    files = []
    for path in source.root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(source.root).parts[:-1]):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in source.media_extensions:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(source.root).as_posix().lower())


def _upsert_asset(
    db: sqlite3.Connection, source: SourceConfig, file_path: Path, relative_path: str
) -> int:
    stat = file_path.stat()
    extension = file_path.suffix.lower()
    directory = Path(relative_path).parent.as_posix()
    if directory == ".":
        directory = ""
    asset_id = asset_id_for(source.id, relative_path)
    db.execute(
        """
        INSERT INTO assets(
          asset_id, source_id, relative_path, absolute_path, filename, stem,
          extension, directory, size_bytes, mtime_ns, media_type, stale, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        ON CONFLICT(source_id, relative_path) DO UPDATE SET
          asset_id = excluded.asset_id,
          absolute_path = excluded.absolute_path,
          filename = excluded.filename,
          stem = excluded.stem,
          extension = excluded.extension,
          directory = excluded.directory,
          size_bytes = excluded.size_bytes,
          mtime_ns = excluded.mtime_ns,
          media_type = excluded.media_type,
          stale = 0,
          indexed_at = CURRENT_TIMESTAMP
        """,
        (
            asset_id,
            source.id,
            relative_path,
            str(file_path),
            file_path.name,
            file_path.stem,
            extension,
            directory,
            stat.st_size,
            stat.st_mtime_ns,
            _media_type(extension),
        ),
    )
    db.execute("DELETE FROM asset_search WHERE asset_id = ?", (asset_id,))
    db.execute(
        """
        INSERT INTO asset_search(asset_id, source_id, filename, stem, directory, relative_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asset_id, source.id, file_path.name, file_path.stem, directory, relative_path),
    )
    return 1


def _mark_missing_stale(db: sqlite3.Connection, source_id: str, seen_paths: set[str]) -> int:
    existing = db.execute(
        "SELECT relative_path FROM assets WHERE source_id = ? AND stale = 0",
        (source_id,),
    ).fetchall()
    missing = [row["relative_path"] for row in existing if row["relative_path"] not in seen_paths]
    if not missing:
        return 0

    db.executemany(
        "UPDATE assets SET stale = 1, indexed_at = CURRENT_TIMESTAMP WHERE source_id = ? AND relative_path = ?",
        [(source_id, relative_path) for relative_path in missing],
    )
    db.executemany(
        """
        DELETE FROM asset_search
        WHERE asset_id = (
          SELECT asset_id FROM assets WHERE source_id = ? AND relative_path = ?
        )
        """,
        [(source_id, relative_path) for relative_path in missing],
    )
    return len(missing)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _media_type(extension: str) -> str:
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "other"
