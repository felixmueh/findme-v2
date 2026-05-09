from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class CleanupSummary:
    stale_assets: int = 0
    asset_search_rows: int = 0
    unreferenced_cache_files: int = 0


def cleanup_index(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    dry_run: bool = False,
) -> CleanupSummary:
    stale_asset_ids = tuple(
        row["asset_id"] for row in db.execute("SELECT asset_id FROM assets WHERE stale = 1")
    )
    stale_assets = len(stale_asset_ids)
    stale_search_rows = _count_search_rows(db, stale_asset_ids)
    unreferenced_cache_files = tuple(_unreferenced_cache_files(db, data_root))

    if dry_run:
        return CleanupSummary(
            stale_assets=stale_assets,
            asset_search_rows=stale_search_rows,
            unreferenced_cache_files=len(unreferenced_cache_files),
        )

    _delete_search_rows(db, stale_asset_ids)
    if stale_asset_ids:
        db.executemany(
            "DELETE FROM assets WHERE asset_id = ?",
            [(asset_id,) for asset_id in stale_asset_ids],
        )
    for path in unreferenced_cache_files:
        path.unlink(missing_ok=True)
    _prune_empty_cache_dirs(data_root / "cache" / "thumbs")

    return CleanupSummary(
        stale_assets=stale_assets,
        asset_search_rows=stale_search_rows,
        unreferenced_cache_files=len(unreferenced_cache_files),
    )


def _count_search_rows(db: sqlite3.Connection, asset_ids: tuple[str, ...]) -> int:
    if not asset_ids:
        return 0
    return sum(
        db.execute(
            "SELECT COUNT(*) FROM asset_search WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()[0]
        for asset_id in asset_ids
    )


def _delete_search_rows(db: sqlite3.Connection, asset_ids: tuple[str, ...]) -> None:
    if not asset_ids:
        return
    db.executemany(
        "DELETE FROM asset_search WHERE asset_id = ?",
        [(asset_id,) for asset_id in asset_ids],
    )


def _unreferenced_cache_files(
    db: sqlite3.Connection,
    data_root: Path,
) -> list[Path]:
    thumbs_root = data_root / "cache" / "thumbs"
    if not thumbs_root.exists():
        return []
    referenced = {
        Path(row["thumbnail_path"]).resolve()
        for row in db.execute(
            """
            SELECT thumbnail_path
            FROM assets
            WHERE stale = 0
              AND thumbnail_path IS NOT NULL
            """
        )
    }
    return [
        path
        for path in thumbs_root.rglob("*.webp")
        if path.is_file() and path.resolve() not in referenced
    ]


def _prune_empty_cache_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            pass
