from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Protocol

from findme.config import AppConfig, SourceConfig
from findme.identity import asset_id_for
from findme.thumbnails import (
    ThumbnailError,
    ThumbnailResult,
    generate_thumbnail,
    generate_video_poster,
)


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".webm", ".mp4"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".wav", ".aif", ".aiff", ".m4a", ".flac"})
IGNORED_DIRS = frozenset({".findme"})


@dataclass(frozen=True)
class IndexSummary:
    scanned_files: int = 0
    upserted_assets: int = 0
    marked_stale: int = 0
    generated_thumbnails: int = 0
    thumbnail_failures: tuple["ThumbnailFailure", ...] = ()


@dataclass(frozen=True)
class ThumbnailFailure:
    source_id: str
    relative_path: str
    absolute_path: Path
    message: str


class ProgressReporter(Protocol):
    def __call__(self, progress: "IndexProgress | IndexEvent") -> None: ...


@dataclass(frozen=True)
class IndexProgress:
    scanned_files: int
    upserted_assets: int
    generated_thumbnails: int
    failed_thumbnails: int
    marked_stale: int
    current_source_id: str
    elapsed_seconds: float
    current_relative_path: str = ""
    current_action: str = ""
    current_thumbnail: str = ""


@dataclass(frozen=True)
class IndexEvent:
    kind: str
    source_id: str
    relative_path: str = ""
    message: str = ""
    waiting_assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingAsset:
    source: SourceConfig
    file_path: Path
    relative_path: str
    size_bytes: int
    mtime_ns: int
    extension: str
    directory: str
    asset_id: str
    media_type: str


@dataclass(frozen=True)
class PreparedAsset:
    pending: PendingAsset
    thumbnail: ThumbnailResult | None
    thumbnail_failure: ThumbnailFailure | None = None
    upsert_required: bool = True


@dataclass(frozen=True)
class CurrentAsset:
    media_type: str
    thumbnail_path: Path | None
    thumbnail: ThumbnailResult | None


def index_sources(
    db: sqlite3.Connection,
    config: AppConfig,
    *,
    thumbnail_workers: int | None = None,
    retry_failed_thumbnails: bool = False,
    progress: ProgressReporter | None = None,
) -> IndexSummary:
    scanned = 0
    upserted = 0
    stale = 0
    thumbnails = 0
    thumbnail_failures: list[ThumbnailFailure] = []
    start = time.monotonic()
    worker_count = thumbnail_workers if thumbnail_workers is not None else config.thumbnail_workers

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
        for prepared in _prepare_assets(
            db,
            config,
            source,
            max_workers=worker_count,
            retry_failed_thumbnails=retry_failed_thumbnails,
            progress=progress,
        ):
            seen_paths.add(prepared.pending.relative_path)
            scanned += 1
            if prepared.upsert_required:
                upserted += _upsert_asset(db, prepared)
            if prepared.thumbnail is not None and prepared.thumbnail.generated:
                thumbnails += 1
            if prepared.thumbnail_failure is not None:
                thumbnail_failures.append(prepared.thumbnail_failure)
            if progress is not None:
                progress(
                    IndexProgress(
                        scanned_files=scanned,
                        upserted_assets=upserted,
                        generated_thumbnails=thumbnails,
                        failed_thumbnails=len(thumbnail_failures),
                        marked_stale=stale,
                        current_source_id=source.id,
                        elapsed_seconds=time.monotonic() - start,
                        current_relative_path=prepared.pending.relative_path,
                        current_action=(
                            "upserted" if prepared.upsert_required else "unchanged"
                        ),
                        current_thumbnail=_thumbnail_status(prepared),
                    )
                )

        stale += _mark_missing_stale(db, source.id, seen_paths)
        if progress is not None:
            progress(
                IndexProgress(
                    scanned_files=scanned,
                    upserted_assets=upserted,
                    generated_thumbnails=thumbnails,
                    failed_thumbnails=len(thumbnail_failures),
                    marked_stale=stale,
                    current_source_id=source.id,
                    elapsed_seconds=time.monotonic() - start,
                )
            )

    return IndexSummary(
        scanned_files=scanned,
        upserted_assets=upserted,
        marked_stale=stale,
        generated_thumbnails=thumbnails,
        thumbnail_failures=tuple(thumbnail_failures),
    )


def count_indexable_files(config: AppConfig) -> int:
    return sum(1 for source in config.sources for _ in _iter_source_files(source))


def _iter_source_files(source: SourceConfig) -> Iterator[Path]:
    for path in source.root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(source.root).parts[:-1]):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in source.media_extensions:
            continue
        yield path


def _prepare_assets(
    db: sqlite3.Connection,
    config: AppConfig,
    source: SourceConfig,
    *,
    max_workers: int,
    retry_failed_thumbnails: bool = False,
    progress: ProgressReporter | None = None,
) -> Iterator[PreparedAsset]:
    in_flight: set[Future[PreparedAsset]] = set()
    started_at: dict[Future[PreparedAsset], float] = {}
    started_pending: dict[Future[PreparedAsset], PendingAsset] = {}
    last_wait_log = 0.0
    limit = max(max_workers * 2, 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        def wait_for_completed() -> tuple[set[Future[PreparedAsset]], set[Future[PreparedAsset]]]:
            nonlocal last_wait_log
            while True:
                done, remaining = wait(in_flight, timeout=5.0, return_when=FIRST_COMPLETED)
                if done:
                    return done, remaining
                if progress is None:
                    continue
                now = time.monotonic()
                if now - last_wait_log < 30.0:
                    continue
                last_wait_log = now
                waiting_assets = tuple(
                    f"{pending.source.id}:{pending.relative_path}:{now - started_at[future]:.1f}s"
                    for future, pending in started_pending.items()
                    if future in in_flight
                )
                progress(
                    IndexEvent(
                        kind="thumbnail-wait",
                        source_id=source.id,
                        message=f"in_flight={len(in_flight)}",
                        waiting_assets=waiting_assets,
                    )
                )

        for file_path in _iter_source_files(source):
            pending = _pending_asset(source, file_path)
            current = _current_asset(db, pending)
            if current is not None and (
                current.media_type == pending.media_type
                and (
                    current.thumbnail is not None
                    or (current.thumbnail_path is None and not retry_failed_thumbnails)
                    or not _needs_thumbnail(pending)
                )
            ):
                yield PreparedAsset(
                    pending=pending,
                    thumbnail=current.thumbnail,
                    upsert_required=False,
                )
                continue
            if progress is not None:
                progress(
                    IndexEvent(
                        kind="file-queued",
                        source_id=source.id,
                        relative_path=pending.relative_path,
                        message=f"media={pending.media_type}",
                    )
                )
            future = executor.submit(_prepare_asset, config, pending)
            in_flight.add(future)
            started_at[future] = time.monotonic()
            started_pending[future] = pending
            if len(in_flight) >= limit:
                done, in_flight = wait_for_completed()
                for future in done:
                    started_at.pop(future, None)
                    started_pending.pop(future, None)
                    yield future.result()
        while in_flight:
            done, in_flight = wait_for_completed()
            for future in done:
                started_at.pop(future, None)
                started_pending.pop(future, None)
                yield future.result()


def _pending_asset(source: SourceConfig, file_path: Path) -> PendingAsset:
    stat = file_path.stat()
    extension = file_path.suffix.lower()
    relative_path = _relative_posix(file_path, source.root)
    directory = Path(relative_path).parent.as_posix()
    if directory == ".":
        directory = ""
    asset_id = asset_id_for(source.id, relative_path)
    media_type = _media_type(extension)
    return PendingAsset(
        source=source,
        file_path=file_path,
        relative_path=relative_path,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        extension=extension,
        directory=directory,
        asset_id=asset_id,
        media_type=media_type,
    )


def _prepare_asset(config: AppConfig, pending: PendingAsset) -> PreparedAsset:
    try:
        thumbnail = _thumbnail_for(
            pending.file_path,
            pending.source,
            data_root=config.data_root,
            asset_id=pending.asset_id,
            relative_path=pending.relative_path,
            size_bytes=pending.size_bytes,
            mtime_ns=pending.mtime_ns,
            media_type=pending.media_type,
        )
        return PreparedAsset(pending=pending, thumbnail=thumbnail)
    except ThumbnailError as exc:
        return PreparedAsset(
            pending=pending,
            thumbnail=None,
            thumbnail_failure=ThumbnailFailure(
                source_id=pending.source.id,
                relative_path=pending.relative_path,
                absolute_path=pending.file_path,
                message=str(exc),
            ),
        )


def _current_asset(
    db: sqlite3.Connection,
    pending: PendingAsset,
) -> CurrentAsset | None:
    row = db.execute(
        """
        SELECT media_type, thumbnail_path, thumbnail_width, thumbnail_height
        FROM assets
        WHERE source_id = ?
          AND relative_path = ?
          AND size_bytes = ?
          AND mtime_ns = ?
          AND stale = 0
        """,
        (
            pending.source.id,
            pending.relative_path,
            pending.size_bytes,
            pending.mtime_ns,
        ),
    ).fetchone()
    if row is None:
        return None
    thumbnail_path = Path(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
    thumbnail = None
    if thumbnail_path is not None:
        if thumbnail_path.is_file():
            thumbnail = ThumbnailResult(
                thumbnail_path,
                row["thumbnail_width"],
                row["thumbnail_height"],
                generated=False,
            )
    return CurrentAsset(
        media_type=row["media_type"],
        thumbnail_path=thumbnail_path,
        thumbnail=thumbnail,
    )


def _needs_thumbnail(pending: PendingAsset) -> bool:
    if pending.media_type == "video":
        return True
    if pending.media_type == "image":
        return True
    return False


def _thumbnail_status(prepared: PreparedAsset) -> str:
    if prepared.thumbnail_failure is not None:
        return "failed"
    if prepared.thumbnail is None:
        return "none"
    if prepared.thumbnail.generated:
        return "generated"
    return "cached"


def _upsert_asset(db: sqlite3.Connection, prepared: PreparedAsset) -> int:
    pending = prepared.pending
    thumbnail = prepared.thumbnail
    db.execute(
        """
        INSERT INTO assets(
          asset_id, source_id, relative_path, absolute_path, filename, stem,
          extension, directory, size_bytes, mtime_ns, media_type,
          thumbnail_path, thumbnail_width, thumbnail_height, stale, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
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
          thumbnail_path = excluded.thumbnail_path,
          thumbnail_width = excluded.thumbnail_width,
          thumbnail_height = excluded.thumbnail_height,
          stale = 0,
          indexed_at = CURRENT_TIMESTAMP
        """,
        (
            pending.asset_id,
            pending.source.id,
            pending.relative_path,
            str(pending.file_path),
            pending.file_path.name,
            pending.file_path.stem,
            pending.extension,
            pending.directory,
            pending.size_bytes,
            pending.mtime_ns,
            pending.media_type,
            str(thumbnail.path) if thumbnail else None,
            thumbnail.width if thumbnail else None,
            thumbnail.height if thumbnail else None,
        ),
    )
    db.execute("DELETE FROM asset_search WHERE asset_id = ?", (pending.asset_id,))
    db.execute(
        """
        INSERT INTO asset_search(asset_id, source_id, filename, stem, directory, relative_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            pending.asset_id,
            pending.source.id,
            pending.file_path.name,
            pending.file_path.stem,
            pending.directory,
            pending.relative_path,
        ),
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
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def _thumbnail_for(
    file_path: Path,
    source: SourceConfig,
    *,
    data_root: Path,
    asset_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
    media_type: str,
) -> ThumbnailResult | None:
    if media_type == "image":
        return generate_thumbnail(
            file_path,
            data_root,
            asset_id=asset_id,
            source_id=source.id,
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )
    if media_type == "video":
        return generate_video_poster(
            file_path,
            data_root,
            asset_id=asset_id,
            source_id=source.id,
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )
    return None
