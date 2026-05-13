from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Callable


@dataclass(frozen=True)
class ThumbnailCleanupSummary:
    unreferenced_thumbnail_files: int = 0


CleanupProgress = Callable[[str, int, int | None], None]
CleanupLog = Callable[[str], None]


def cleanup_thumbnails(
    db: sqlite3.Connection,
    *,
    data_root: Path,
    dry_run: bool = False,
    progress: CleanupProgress | None = None,
    log: CleanupLog | None = None,
) -> ThumbnailCleanupSummary:
    if log is not None:
        log("thumbnail cleanup scan started")
    unreferenced = tuple(_unreferenced_thumbnail_files(db, data_root, progress=progress))
    if log is not None:
        log(f"thumbnail cleanup scan complete: unreferenced={len(unreferenced)}")

    if dry_run:
        return ThumbnailCleanupSummary(
            unreferenced_thumbnail_files=len(unreferenced),
        )

    for index, path in enumerate(unreferenced, start=1):
        path.unlink(missing_ok=True)
        if progress is not None:
            progress("thumbnails", index, len(unreferenced))
    if log is not None:
        log("thumbnail cleanup complete")

    return ThumbnailCleanupSummary(
        unreferenced_thumbnail_files=len(unreferenced),
    )


def _unreferenced_thumbnail_files(
    db: sqlite3.Connection,
    data_root: Path,
    *,
    progress: CleanupProgress | None = None,
) -> list[Path]:
    thumbs_root = data_root / "cache" / "thumbs"
    if not thumbs_root.exists():
        return []
    referenced = {
        Path(row["thumbnail_path"]).as_posix()
        for row in db.execute(
            """
            SELECT thumbnail_path
            FROM assets
            WHERE thumbnail_path IS NOT NULL
            """
        )
    }
    referenced.update(
        Path(row["cache_path"]).as_posix()
        for row in db.execute(
            """
            SELECT cache_path
            FROM asset_thumbnail_variants
            """
        )
    )
    unreferenced: list[Path] = []
    count = 0
    for count, path in enumerate(thumbs_root.rglob("*.webp"), start=1):
        if progress is not None:
            progress("thumbnail-scan", count, None)
        if path.is_file() and path.as_posix() not in referenced:
            unreferenced.append(path)
    if progress is not None and count > 0:
        progress("thumbnail-scan", count, count)
    return unreferenced
