from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
import threading
from pathlib import Path
import sqlite3
import time
from typing import Protocol

import powerwalk

from findme.config import AppConfig, SourceConfig
from findme.db import refresh_index_fingerprint
from findme.identity import asset_id_for
from findme.thumbnails import (
    MediaMetadata,
    ThumbnailError,
    ThumbnailResult,
    generate_thumbnail,
    generate_video_poster,
    read_media_metadata,
)

INSERT_BATCH_SIZE = 512
POWERWALK_THREADS = 32

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".webm", ".mp4"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".wav", ".aif", ".aiff", ".m4a", ".flac"})
IGNORED_DIRS = frozenset({".findme"})


@dataclass(frozen=True)
class IndexSummary:
    scanned_files: int = 0
    written_assets: int = 0
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
    written_assets: int
    generated_thumbnails: int
    failed_thumbnails: int
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
    worker_id: str = ""
    worker_elapsed_seconds: float | None = None
    wait_seconds: float | None = None
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
    media_metadata: MediaMetadata | None = None
    thumbnail_failure: ThumbnailFailure | None = None
    processing_required: bool = True
    worker_id: str = ""
    worker_elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class CurrentAsset:
    absolute_path: str
    media_type: str
    size_bytes: int
    mtime_ns: int
    media_width: int | None
    media_height: int | None
    duration_seconds: float | None
    thumbnail_path: Path | None
    thumbnail_width: int | None
    thumbnail_height: int | None


@dataclass(frozen=True)
class DiscoveredAsset:
    file_path: Path
    relative_path: str
    extension: str
    directory: str
    media_type: str


def index_sources(
    db: sqlite3.Connection,
    config: AppConfig,
    *,
    source_ids: frozenset[str] | None = None,
    thumbnail_workers: int | None = None,
    retry_failed_thumbnails: bool = False,
    rebuild_thumbnails: bool = False,
    thumbnail_worker_timeout_seconds: float | None = None,
    progress: ProgressReporter | None = None,
) -> IndexSummary:
    scanned = 0
    written = 0
    thumbnails = 0
    thumbnail_failures: list[ThumbnailFailure] = []
    start = time.monotonic()
    worker_count = thumbnail_workers if thumbnail_workers is not None else config.thumbnail_workers
    if (
        thumbnail_worker_timeout_seconds is not None
        and thumbnail_worker_timeout_seconds <= 0.0
    ):
        thumbnail_worker_timeout_seconds = None

    selected_sources = _filter_sources(config.sources, source_ids)
    thumbnail_work: list[PendingAsset] = []
    index_changed = False
    needs_existing_metadata = retry_failed_thumbnails or rebuild_thumbnails

    for source in selected_sources:
        index_changed = _upsert_source(db, source) or index_changed
        source_assets = (
            _load_source_assets(db, source, progress=progress)
            if needs_existing_metadata
            else {}
        )
        source_asset_paths = (
            set(source_assets)
            if needs_existing_metadata
            else _load_source_asset_paths(db, source, progress=progress)
        )
        missing_media_metadata_paths = (
            _source_paths_missing_media_metadata(source_assets)
            if needs_existing_metadata
            else _load_source_asset_paths_missing_media_metadata(
                db,
                source,
                progress=progress,
            )
        )
        if missing_media_metadata_paths and not source_assets:
            source_assets = _load_source_assets(db, source, progress=progress)
            source_asset_paths = set(source_assets)
        source_scan_started = time.monotonic()

        seen_paths: set[str] = set()
        new_assets: list[PreparedAsset] = []
        updated_assets: list[PreparedAsset] = []
        for discovered in _iter_source_file_entries(source):
            seen_paths.add(discovered.relative_path)
            if discovered.relative_path not in source_asset_paths:
                pending = _pending_asset(source, discovered.file_path)
                current = None
            elif needs_existing_metadata or discovered.relative_path in missing_media_metadata_paths:
                current = source_assets[discovered.relative_path]
                pending = _pending_asset_from_current(source, discovered, current)
            else:
                pending = None
                current = None
            scanned += 1
            if pending is None:
                if progress is not None:
                    progress(
                        IndexProgress(
                            scanned_files=scanned,
                            written_assets=written,
                            generated_thumbnails=thumbnails,
                            failed_thumbnails=len(thumbnail_failures),
                            current_source_id=source.id,
                            elapsed_seconds=time.monotonic() - start,
                            current_relative_path=discovered.relative_path,
                            current_action="unchanged",
                            current_thumbnail="unchanged",
                        )
                    )
                continue
            prepared = _prepare_index_asset(
                pending,
                current,
                retry_failed_thumbnails=retry_failed_thumbnails,
                rebuild_thumbnails=rebuild_thumbnails,
            )
            if current is None:
                new_assets.append(prepared)
            elif _existing_asset_needs_update(discovered, current):
                updated_assets.append(prepared)
            if _should_generate_thumbnail(prepared):
                thumbnail_work.append(pending)
            if progress is not None:
                progress(
                    IndexProgress(
                        scanned_files=scanned,
                        written_assets=written,
                        generated_thumbnails=thumbnails,
                        failed_thumbnails=len(thumbnail_failures),
                        current_source_id=source.id,
                        elapsed_seconds=time.monotonic() - start,
                        current_relative_path=prepared.pending.relative_path,
                        current_action=(
                            "indexed" if prepared.processing_required else "unchanged"
                        ),
                        current_thumbnail=_thumbnail_status(prepared),
                    )
                )

        written += _insert_assets(
            db,
            new_assets,
            progress=progress,
        )
        written += _update_assets(
            db,
            updated_assets,
            progress=progress,
        )
        deleted = _delete_missing_assets(db, source.id, source_asset_paths - seen_paths)
        if new_assets or updated_assets or deleted:
            index_changed = True
        if progress is not None:
            progress(
                IndexEvent(
                    kind="source-scan-complete",
                    source_id=source.id,
                    message=(
                        f"seen={len(seen_paths)} "
                        f"elapsed={time.monotonic()-source_scan_started:.2f}s"
                    ),
                )
            )

    if source_ids is None:
        index_changed = _delete_unconfigured_sources(db, config.sources) or index_changed
    if index_changed:
        refresh_index_fingerprint(db)
        db.commit()

    pending_thumbnail_commits = 0
    for prepared in _generate_thumbnails(
        config,
        thumbnail_work,
        max_workers=worker_count,
        thumbnail_worker_timeout_seconds=thumbnail_worker_timeout_seconds,
        progress=progress,
    ):
        if prepared.thumbnail is not None or prepared.media_metadata is not None:
            updated = _update_asset_processing_result(db, prepared)
            if prepared.thumbnail is not None and prepared.thumbnail.generated:
                thumbnails += updated
            pending_thumbnail_commits += 1
            if pending_thumbnail_commits >= INSERT_BATCH_SIZE:
                db.commit()
                pending_thumbnail_commits = 0
        if prepared.thumbnail_failure is not None:
            thumbnail_failures.append(prepared.thumbnail_failure)
        if progress is not None:
            progress(
                IndexProgress(
                    scanned_files=scanned,
                    written_assets=written,
                    generated_thumbnails=thumbnails,
                    failed_thumbnails=len(thumbnail_failures),
                    current_source_id=prepared.pending.source.id,
                    elapsed_seconds=time.monotonic() - start,
                    current_relative_path=prepared.pending.relative_path,
                    current_action="thumbnail",
                    current_thumbnail=_thumbnail_status(prepared),
                )
            )
    if thumbnail_work:
        if pending_thumbnail_commits:
            db.commit()
        refresh_index_fingerprint(db)
        db.commit()

    return IndexSummary(
        scanned_files=scanned,
        written_assets=written,
        generated_thumbnails=thumbnails,
        thumbnail_failures=tuple(thumbnail_failures),
    )


def count_indexable_files(
    config: AppConfig,
    *,
    source_ids: frozenset[str] | None = None,
    progress: ProgressReporter | None = None,
    log_interval: int = 50_000,
) -> int:
    total = 0
    for source in _filter_sources(config.sources, source_ids):
        if progress is not None:
            progress(
                IndexEvent(
                    kind="index-count-start",
                    source_id=source.id,
                    message="started",
                )
        )
        source_count = 0
        for _ in _iter_source_file_entries(source):
            total += 1
            source_count += 1
            if progress is not None and source_count % log_interval == 0:
                progress(
                    IndexEvent(
                        kind="index-count-checkpoint",
                        source_id=source.id,
                        message=f"seen={source_count}",
                    )
                )
        if progress is not None:
            progress(
                IndexEvent(
                    kind="index-count-complete",
                    source_id=source.id,
                    message=f"count={source_count}",
                )
            )
    return total


def _filter_sources(
    sources: tuple[SourceConfig, ...],
    source_ids: frozenset[str] | None,
) -> tuple[SourceConfig, ...]:
    if source_ids is None:
        return sources
    selected_sources = [
        source for source in sources if source.id in source_ids
    ]
    return tuple(selected_sources)


def _iter_source_files(source: SourceConfig) -> Iterator[Path]:
    for discovered in _iter_source_file_entries(source):
        yield discovered.file_path


def _iter_source_file_entries(source: SourceConfig) -> Iterator[DiscoveredAsset]:
    extensions = source.media_extensions
    root = os.fspath(source.root)
    root_prefix = root.rstrip(os.sep) + os.sep
    root_prefix_len = len(root_prefix)
    for entry in powerwalk.walk(
        source.root,
        ignore_hidden=False,
        respect_git_ignore=False,
        respect_global_git_ignore=False,
        respect_git_exclude=False,
        respect_ignore=False,
        follow_symlinks=False,
        exclude=["**/.findme/**", "**/.findme/"],
        threads=POWERWALK_THREADS,
        on_error="raise",
    ):
        if entry.is_dir:
            continue
        path = entry.path_str
        name = path.rsplit(os.sep, 1)[-1]
        dot = name.rfind(".")
        if dot < 0:
            continue
        extension = name[dot:].lower()
        if extension not in extensions:
            continue
        relative_path = path[root_prefix_len:].replace(os.sep, "/")
        slash = relative_path.rfind("/")
        directory_posix = "" if slash < 0 else relative_path[:slash]
        yield DiscoveredAsset(
            file_path=Path(path),
            relative_path=relative_path,
            extension=extension,
            directory=directory_posix,
            media_type=_media_type(extension),
        )


def _pending_asset_from_current(
    source: SourceConfig,
    discovered: DiscoveredAsset,
    current: CurrentAsset,
) -> PendingAsset:
    return PendingAsset(
        source=source,
        file_path=discovered.file_path,
        relative_path=discovered.relative_path,
        size_bytes=current.size_bytes,
        mtime_ns=current.mtime_ns,
        extension=discovered.extension,
        directory=discovered.directory,
        asset_id=asset_id_for(source.id, discovered.relative_path),
        media_type=discovered.media_type,
    )


def _existing_asset_needs_update(
    discovered: DiscoveredAsset,
    current: CurrentAsset,
) -> bool:
    return (
        current.absolute_path != str(discovered.file_path)
        or current.media_type != discovered.media_type
    )


def _upsert_source(db: sqlite3.Connection, source: SourceConfig) -> bool:
    cursor = db.execute(
        """
        INSERT INTO sources(id, display_name, root, priority)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          display_name = excluded.display_name,
          root = excluded.root,
          priority = excluded.priority
        WHERE sources.display_name IS NOT excluded.display_name
           OR sources.root IS NOT excluded.root
           OR sources.priority IS NOT excluded.priority
        """,
        (source.id, source.display_name, str(source.root), source.priority),
    )
    return cursor.rowcount > 0


def _delete_missing_assets(
    db: sqlite3.Connection,
    source_id: str,
    missing_paths: set[str],
) -> int:
    deleted = 0
    missing = tuple(missing_paths)
    for start in range(0, len(missing), INSERT_BATCH_SIZE):
        batch = missing[start : start + INSERT_BATCH_SIZE]
        placeholders = ", ".join("?" for _ in batch)
        cursor = db.execute(
            f"DELETE FROM assets WHERE source_id = ? AND relative_path IN ({placeholders})",
            (source_id, *batch),
        )
        deleted += cursor.rowcount
    return deleted


def _update_assets(
    db: sqlite3.Connection,
    prepared_assets: list[PreparedAsset],
    *,
    progress: ProgressReporter | None = None,
) -> int:
    if not prepared_assets:
        return 0
    started = time.monotonic()
    db.executemany(
        """
        UPDATE assets
        SET absolute_path = ?,
            filename = ?,
            stem = ?,
            extension = ?,
            directory = ?,
            media_type = ?,
            media_width = NULL,
            media_height = NULL,
            duration_seconds = NULL,
            indexed_at = CURRENT_TIMESTAMP
        WHERE source_id = ?
          AND relative_path = ?
        """,
        [
            (
                str(prepared.pending.file_path),
                prepared.pending.file_path.name,
                prepared.pending.file_path.stem,
                prepared.pending.extension,
                prepared.pending.directory,
                prepared.pending.media_type,
                prepared.pending.source.id,
                prepared.pending.relative_path,
            )
            for prepared in prepared_assets
        ],
    )
    if progress is not None:
        progress(
            IndexEvent(
                kind="update-assets",
                source_id=prepared_assets[0].pending.source.id,
                message=f"count={len(prepared_assets)} elapsed={time.monotonic()-started:.3f}s",
            )
        )
    return len(prepared_assets)


def _delete_unconfigured_sources(
    db: sqlite3.Connection,
    configured_sources: tuple[SourceConfig, ...],
) -> bool:
    before = db.total_changes
    configured_ids = tuple(source.id for source in configured_sources)
    if not configured_ids:
        db.execute("DELETE FROM sources")
        return db.total_changes != before
    placeholders = ", ".join("?" for _ in configured_ids)
    db.execute(
        f"DELETE FROM sources WHERE id NOT IN ({placeholders})",
        configured_ids,
    )
    return db.total_changes != before


def _prepare_index_asset(
    pending: PendingAsset,
    current: CurrentAsset | None,
    *,
    retry_failed_thumbnails: bool,
    rebuild_thumbnails: bool,
) -> PreparedAsset:
    if current is not None and (
        current.media_type == pending.media_type
        and current.size_bytes == pending.size_bytes
        and current.mtime_ns == pending.mtime_ns
    ):
        needs_thumbnail = _needs_thumbnail(pending)
        needs_media_metadata = _needs_media_metadata(pending, current)
        if needs_thumbnail and rebuild_thumbnails:
            return PreparedAsset(pending=pending, thumbnail=None, processing_required=True)
        has_cached_thumbnail = (
            current.thumbnail_path is not None
        )
        if needs_thumbnail and has_cached_thumbnail and not needs_media_metadata:
            return PreparedAsset(
                pending=pending,
                thumbnail=ThumbnailResult(
                    path=current.thumbnail_path,
                    width=current.thumbnail_width,
                    height=current.thumbnail_height,
                    generated=False,
                ),
                processing_required=False,
            )
        if needs_thumbnail and has_cached_thumbnail and needs_media_metadata:
            return PreparedAsset(pending=pending, thumbnail=None, processing_required=True)
        if needs_thumbnail and current.thumbnail_path is None and not retry_failed_thumbnails:
            return PreparedAsset(
                pending=pending,
                thumbnail=None,
                processing_required=needs_media_metadata,
            )
        if not needs_thumbnail and not needs_media_metadata:
            return PreparedAsset(pending=pending, thumbnail=None, processing_required=False)

    return PreparedAsset(pending=pending, thumbnail=None, processing_required=True)


def _should_generate_thumbnail(prepared: PreparedAsset) -> bool:
    return (
        prepared.thumbnail is None
        and prepared.processing_required
        and _needs_thumbnail(prepared.pending)
    )


def _generate_thumbnails(
    config: AppConfig,
    pending_assets: list[PendingAsset],
    *,
    max_workers: int,
    thumbnail_worker_timeout_seconds: float | None = None,
    progress: ProgressReporter | None = None,
) -> Iterator[PreparedAsset]:
    if not pending_assets:
        return

    in_flight: set[Future[PreparedAsset]] = set()
    started_at: dict[Future[PreparedAsset], float] = {}
    started_pending: dict[Future[PreparedAsset], PendingAsset] = {}
    last_wait_log = 0.0
    limit = max(max_workers * 2, 1)
    executor = ThreadPoolExecutor(max_workers=max_workers)

    def _failure_asset(pending: PendingAsset, message: str) -> PreparedAsset:
        return PreparedAsset(
            pending=pending,
            thumbnail=None,
            thumbnail_failure=ThumbnailFailure(
                source_id=pending.source.id,
                relative_path=pending.relative_path,
                absolute_path=pending.file_path,
                message=message,
            ),
            processing_required=True,
        )

    def _drain_done(
        done: set[Future[PreparedAsset]],
        timed_out: set[Future[PreparedAsset]],
    ) -> Iterator[PreparedAsset]:
        timeout_msg = (
            f"thumbnail worker timed out after {thumbnail_worker_timeout_seconds:.1f}s"
            if thumbnail_worker_timeout_seconds is not None
            else "thumbnail worker timed out"
        )

        for future in timed_out:
            start_at = started_at.pop(future, None)
            pending = started_pending.pop(future, None)
            if pending is None:
                continue
            elapsed = time.monotonic() - start_at if start_at is not None else 0.0
            if progress is not None:
                progress(
                    IndexEvent(
                        kind="file-failed",
                        source_id=pending.source.id,
                        relative_path=pending.relative_path,
                        message=timeout_msg,
                        wait_seconds=elapsed,
                    )
                )
            yield _failure_asset(pending, timeout_msg)

        for future in done:
            start_at = started_at.pop(future, None)
            pending = started_pending.pop(future, None)
            path = pending.relative_path if pending is not None else ""
            source_id = pending.source.id if pending is not None else ""
            try:
                prepared = future.result()
            except Exception as exc:
                if progress is not None:
                    progress(
                        IndexEvent(
                            kind="file-failed",
                            source_id=source_id,
                            relative_path=path,
                            message=f"exception={exc!r}",
                            wait_seconds=(
                                time.monotonic() - start_at
                                if start_at is not None
                                else None
                            ),
                        )
                    )
                if pending is None:
                    continue
                prepared = _failure_asset(pending, f"thumbnail worker error: {exc!r}")
            wait_seconds = (
                time.monotonic() - start_at if start_at is not None else 0.0
            )
            if progress is not None:
                progress(
                    IndexEvent(
                        kind="file-completed",
                        source_id=source_id,
                        relative_path=path,
                        message=f"status={_thumbnail_status(prepared)}",
                        worker_id=prepared.worker_id,
                        worker_elapsed_seconds=prepared.worker_elapsed_seconds,
                        wait_seconds=wait_seconds,
                    )
                )
            yield prepared

    def wait_for_completed() -> tuple[
        set[Future[PreparedAsset]],
        set[Future[PreparedAsset]],
        set[Future[PreparedAsset]],
    ]:
        nonlocal last_wait_log
        wait_timeout = 5.0
        if thumbnail_worker_timeout_seconds is not None:
            wait_timeout = max(0.05, min(wait_timeout, thumbnail_worker_timeout_seconds))
        while True:
            now = time.monotonic()
            done, remaining = wait(
                in_flight,
                timeout=wait_timeout,
                return_when=FIRST_COMPLETED,
            )
            now = time.monotonic()
            timed_out: set[Future[PreparedAsset]] = set()
            if thumbnail_worker_timeout_seconds is not None and in_flight:
                timed_out = {
                    future
                    for future in remaining
                    if future not in done
                    and now - started_at.get(future, now)
                    >= thumbnail_worker_timeout_seconds
                }
                if timed_out and progress is not None:
                    pending = started_pending[next(iter(timed_out))]
                    progress(
                        IndexEvent(
                            kind="thumbnail-timeout",
                            source_id=pending.source.id,
                            message=(
                                "thumbnail worker timeout reached "
                                f"threshold={thumbnail_worker_timeout_seconds:.1f}s "
                                f"timed_out={len(timed_out)}"
                            ),
                        )
                    )
                if timed_out:
                    return done, timed_out, set(remaining - timed_out)

            if done:
                return done, set(), set(remaining)

            waiting_assets = tuple(
                (
                    f"{pending.source.id}:{pending.relative_path}:"
                    f"{now - started_at[future]:.1f}s"
                    for future, pending in started_pending.items()
                    if future in in_flight
                )
            )
            oldest_wait = 0.0
            if waiting_assets:
                oldest_wait = max(
                    now - started_at[future]
                    for future in started_pending
                    if future in in_flight
                )

            if (
                thumbnail_worker_timeout_seconds is not None
                and waiting_assets
                and oldest_wait >= thumbnail_worker_timeout_seconds
            ):
                timed_out = {
                    future
                    for future in in_flight
                    if now - started_at.get(future, now) >= thumbnail_worker_timeout_seconds
                }
                if timed_out:
                    return set(), timed_out, set(in_flight - timed_out)

            if now - last_wait_log < 10.0:
                continue

            last_wait_log = now
            if progress is not None:
                first_pending = next(iter(started_pending.values()))
                progress(
                    IndexEvent(
                        kind="thumbnail-wait",
                        source_id=first_pending.source.id,
                        message=f"in_flight={len(in_flight)} oldest_wait={oldest_wait:.1f}s",
                        waiting_assets=waiting_assets,
                    )
                )

    try:
        for pending in pending_assets:
            if progress is not None:
                progress(
                    IndexEvent(
                        kind="file-queued",
                        source_id=pending.source.id,
                        relative_path=pending.relative_path,
                        message=f"media={pending.media_type} in_flight={len(in_flight)}",
                    )
                )
                progress(
                    IndexEvent(
                        kind="file-submitted",
                        source_id=pending.source.id,
                        relative_path=pending.relative_path,
                        message=f"media={pending.media_type} queue={len(in_flight)}",
                    )
                )

            future = executor.submit(_prepare_asset, config, pending, progress=progress)
            in_flight.add(future)
            started_at[future] = time.monotonic()
            started_pending[future] = pending
            if len(in_flight) >= limit:
                done, timed_out, remaining = wait_for_completed()
                in_flight = remaining
                if timed_out:
                    executor.shutdown(wait=False, cancel_futures=True)
                    executor = ThreadPoolExecutor(max_workers=max_workers)
                for prepared in _drain_done(done, timed_out):
                    yield prepared

        while in_flight:
            done, timed_out, remaining = wait_for_completed()
            in_flight = remaining
            if timed_out:
                executor.shutdown(wait=False, cancel_futures=True)
                executor = ThreadPoolExecutor(max_workers=max_workers)
            for prepared in _drain_done(done, timed_out):
                yield prepared

    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _update_asset_processing_result(db: sqlite3.Connection, prepared: PreparedAsset) -> int:
    if prepared.thumbnail is None and prepared.media_metadata is None:
        return 0
    pending = prepared.pending
    baseline = prepared.thumbnail
    metadata = prepared.media_metadata
    cursor = db.execute(
        """
        UPDATE assets
        SET thumbnail_path = COALESCE(?, thumbnail_path),
            thumbnail_width = COALESCE(?, thumbnail_width),
            thumbnail_height = COALESCE(?, thumbnail_height),
            media_width = COALESCE(?, media_width),
            media_height = COALESCE(?, media_height),
            duration_seconds = ?,
            indexed_at = CURRENT_TIMESTAMP
        WHERE asset_id = ?
          AND source_id = ?
          AND relative_path = ?
          AND size_bytes = ?
          AND mtime_ns = ?
          AND media_type = ?
        """,
        (
            str(baseline.path) if baseline is not None else None,
            baseline.width if baseline is not None else None,
            baseline.height if baseline is not None else None,
            metadata.width if metadata is not None else None,
            metadata.height if metadata is not None else None,
            metadata.duration_seconds if metadata is not None else None,
            pending.asset_id,
            pending.source.id,
            pending.relative_path,
            pending.size_bytes,
            pending.mtime_ns,
            pending.media_type,
        ),
    )
    if cursor.rowcount > 0 and prepared.thumbnail is not None:
        _replace_asset_thumbnail_variants(db, prepared)
    return cursor.rowcount


def _replace_asset_thumbnail_variants(
    db: sqlite3.Connection,
    prepared: PreparedAsset,
) -> None:
    if prepared.thumbnail is None:
        return
    pending = prepared.pending
    variants = prepared.thumbnail.variants
    if not variants:
        return
    kind = variants[0].kind
    db.execute(
        """
        DELETE FROM asset_thumbnail_variants
        WHERE asset_id = ?
          AND kind = ?
        """,
        (pending.asset_id, kind),
    )
    db.executemany(
        """
        INSERT INTO asset_thumbnail_variants(
          asset_id,
          source_id,
          relative_path,
          kind,
          max_edge,
          width,
          height,
          cache_path,
          generated_from_size_bytes,
          generated_from_mtime_ns
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                pending.asset_id,
                pending.source.id,
                pending.relative_path,
                variant.kind,
                variant.max_edge,
                variant.width,
                variant.height,
                str(variant.path),
                pending.size_bytes,
                pending.mtime_ns,
            )
            for variant in variants
        ],
    )


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


def _prepare_asset(
    config: AppConfig,
    pending: PendingAsset,
    *,
    progress: ProgressReporter | None = None,
) -> PreparedAsset:
    worker_name = threading.current_thread().name
    started = time.monotonic()
    if progress is not None:
        progress(
            IndexEvent(
                kind="thumbnail-worker-started",
                source_id=pending.source.id,
                relative_path=pending.relative_path,
                message=f"worker={worker_name}",
            )
        )
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
            additional_thumbnail_mipmaps=config.additional_thumbnail_mipmaps,
        )
        media_metadata = read_media_metadata(pending.file_path, pending.media_type)
        worker_elapsed_seconds = time.monotonic() - started
        return PreparedAsset(
            pending=pending,
            thumbnail=thumbnail,
            media_metadata=media_metadata,
            worker_id=worker_name,
            worker_elapsed_seconds=worker_elapsed_seconds,
        )
    except ThumbnailError as exc:
        elapsed = time.monotonic() - started
        return PreparedAsset(
            pending=pending,
            thumbnail=None,
            thumbnail_failure=ThumbnailFailure(
                source_id=pending.source.id,
                relative_path=pending.relative_path,
                absolute_path=pending.file_path,
                message=str(exc),
            ),
            worker_id=worker_name,
            worker_elapsed_seconds=elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return PreparedAsset(
            pending=pending,
            thumbnail=None,
            thumbnail_failure=ThumbnailFailure(
                source_id=pending.source.id,
                relative_path=pending.relative_path,
                absolute_path=pending.file_path,
                message=str(exc),
            ),
            worker_id=worker_name,
            worker_elapsed_seconds=elapsed,
        )


def _load_source_assets(
    db: sqlite3.Connection,
    source: SourceConfig,
    *,
    progress: ProgressReporter | None = None,
) -> dict[str, CurrentAsset]:
    started = time.monotonic()
    rows = db.execute(
        """
        SELECT
          relative_path,
          absolute_path,
          size_bytes,
          mtime_ns,
          media_type,
          media_width,
          media_height,
          duration_seconds,
          thumbnail_path,
          thumbnail_width,
          thumbnail_height
        FROM assets
        WHERE source_id = ?
        """,
        (source.id,),
    ).fetchall()
    source_assets: dict[str, CurrentAsset] = {}
    for row in rows:
        thumbnail_path = (
            Path(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
        )
        source_assets[row["relative_path"]] = CurrentAsset(
            absolute_path=row["absolute_path"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            mtime_ns=row["mtime_ns"],
            media_width=row["media_width"],
            media_height=row["media_height"],
            duration_seconds=row["duration_seconds"],
            thumbnail_path=thumbnail_path,
            thumbnail_width=row["thumbnail_width"],
            thumbnail_height=row["thumbnail_height"],
        )
    if progress is not None:
        progress(
            IndexEvent(
                kind="source-assets-loaded",
                source_id=source.id,
                message=(
                    f"count={len(source_assets)} "
                    f"elapsed={time.monotonic() - started:.3f}s"
                ),
            )
        )
    return source_assets


def _load_source_asset_paths(
    db: sqlite3.Connection,
    source: SourceConfig,
    *,
    progress: ProgressReporter | None = None,
) -> set[str]:
    started = time.monotonic()
    rows = db.execute(
        """
        SELECT relative_path
        FROM assets
        WHERE source_id = ?
        """,
        (source.id,),
    ).fetchall()
    paths = {row["relative_path"] for row in rows}
    if progress is not None:
        progress(
            IndexEvent(
                kind="source-assets-loaded",
                source_id=source.id,
                message=(
                    f"count={len(paths)} "
                    f"elapsed={time.monotonic() - started:.3f}s"
                ),
            )
        )
    return paths


def _load_source_asset_paths_missing_media_metadata(
    db: sqlite3.Connection,
    source: SourceConfig,
    *,
    progress: ProgressReporter | None = None,
) -> set[str]:
    started = time.monotonic()
    rows = db.execute(
        """
        SELECT relative_path
        FROM assets
        WHERE source_id = ?
          AND media_type IN ('image', 'video')
          AND thumbnail_path IS NOT NULL
          AND (media_width IS NULL OR media_height IS NULL)
        """,
        (source.id,),
    ).fetchall()
    paths = {row["relative_path"] for row in rows}
    if progress is not None and paths:
        progress(
            IndexEvent(
                kind="source-media-metadata-missing-loaded",
                source_id=source.id,
                message=(
                    f"count={len(paths)} "
                    f"elapsed={time.monotonic() - started:.3f}s"
                ),
            )
        )
    return paths


def _source_paths_missing_media_metadata(
    source_assets: dict[str, CurrentAsset],
) -> set[str]:
    return {
        relative_path
        for relative_path, current in source_assets.items()
        if _needs_media_metadata_for_current(current)
    }


def _insert_assets(
    db: sqlite3.Connection,
    prepared_assets: list[PreparedAsset],
    *,
    progress: ProgressReporter | None = None,
) -> int:
    if not prepared_assets:
        return 0
    started = time.monotonic()
    if progress is not None:
        progress(
            IndexEvent(
                kind="insert-batch-start",
                source_id=prepared_assets[0].pending.source.id,
                message=f"batch={len(prepared_assets)}",
            )
        )
    db.executemany(
        """
        INSERT INTO assets(
          asset_id, source_id, relative_path, absolute_path, filename, stem,
          extension, directory, size_bytes, mtime_ns, media_type,
          thumbnail_path, thumbnail_width, thumbnail_height, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            (
                prepared.pending.asset_id,
                prepared.pending.source.id,
                prepared.pending.relative_path,
                str(prepared.pending.file_path),
                prepared.pending.file_path.name,
                prepared.pending.file_path.stem,
                prepared.pending.extension,
                prepared.pending.directory,
                prepared.pending.size_bytes,
                prepared.pending.mtime_ns,
                prepared.pending.media_type,
                str(prepared.thumbnail.path) if prepared.thumbnail else None,
                prepared.thumbnail.width if prepared.thumbnail else None,
                prepared.thumbnail.height if prepared.thumbnail else None,
            )
            for prepared in prepared_assets
        ],
    )
    if progress is not None:
        progress(
            IndexEvent(
                kind="insert-batch",
                source_id=prepared_assets[0].pending.source.id,
                message=f"batch={len(prepared_assets)} elapsed={time.monotonic()-started:.3f}s",
            )
        )
    return len(prepared_assets)


def _needs_thumbnail(pending: PendingAsset) -> bool:
    if pending.media_type == "video":
        return True
    if pending.media_type == "image":
        return True
    return False


def _needs_media_metadata(pending: PendingAsset, current: CurrentAsset) -> bool:
    if pending.media_type not in {"image", "video"}:
        return False
    return _needs_media_metadata_for_current(current)


def _needs_media_metadata_for_current(current: CurrentAsset) -> bool:
    return (
        current.media_type in {"image", "video"}
        and (current.media_width is None or current.media_height is None)
    )


def _thumbnail_status(prepared: PreparedAsset) -> str:
    if prepared.thumbnail_failure is not None:
        return "failed"
    if prepared.thumbnail is None:
        return "none"
    if prepared.thumbnail.generated:
        return "generated"
    return "cached"


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
    additional_thumbnail_mipmaps: tuple[int, ...],
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
            additional_max_edges=additional_thumbnail_mipmaps,
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
            additional_max_edges=additional_thumbnail_mipmaps,
        )
    return None
