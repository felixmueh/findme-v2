from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import numpy as np
from pydantic import BaseModel, Field

from findme.assets import (
    AssetNotFoundError,
    UnsafeAssetPathError,
    asset_actions,
    asset_response,
    get_asset,
    thumbnail_variant_response,
)
from findme.config import AppConfig
from findme.db import connect, initialize_database, search_corpus_is_stale
from findme.indexer import IndexProgress, index_sources
from findme.metadata import (
    MetadataValidationError,
    add_asset_tags,
    list_tags,
    remove_asset_tags,
    set_asset_tags,
    tag_response,
    tags_for_asset,
)
from findme.metadata_db import connect_metadata, initialize_metadata_database
from findme.result_cards import ResultCardResolver
from findme.search import (
    SearchIndex,
    SearchMode,
    SearchResult,
    SearchControl,
    SearchSuperseded,
    load_domain_vocabulary,
)
from findme.search_profiles import (
    SearchProfileConflict,
    SearchProfileSessionManager,
    SearchProfileValidationError,
)
from findme.search_corpus import (
    SEARCH_CORPUS_VERSION,
    load_search_corpus_snapshot,
    rebuild_search_corpus,
    search_corpus_lexical_path,
    search_corpus_snapshot_path,
)


class SearchProfileSelectRequest(BaseModel):
    search_session: str | None = None
    profile_id: str
    discard: bool = False


class SearchProfileSettingsRequest(BaseModel):
    search_session: str | None = None
    source_id: str
    directory: str = ""
    quality_override: int | None = Field(default=None, ge=0, le=10)
    selection_state: str | None = None


class SearchProfileSaveRequest(BaseModel):
    search_session: str | None = None
    name: str | None = None
    overwrite: bool = False


class SearchProfileSessionRequest(BaseModel):
    search_session: str | None = None


class SearchProfileDeleteRequest(BaseModel):
    discard: bool = False


class AssetTagsRequest(BaseModel):
    tags: list[str]


class BatchTagsScope(BaseModel):
    kind: Literal["search", "folder"]
    q: str | None = None
    mode: SearchMode = "strict"
    source_id: str | None = None
    directory: str = ""
    directory_prefix: str | None = None
    view_source_id: str | None = None
    view_directory: str | None = None
    search_session: str | None = None
    where: dict[str, Any] | None = None


class BatchTagsRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    scope: BatchTagsScope | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)


class SearchQueryRequest(BaseModel):
    q: str | None = None
    mode: SearchMode = "strict"
    source_id: str | None = None
    directory: str | None = None
    directory_prefix: str | None = None
    view_source_id: str | None = None
    view_directory: str | None = None
    search_session: str | None = None
    search_revision: int | None = None
    media_type: Literal["image", "video", "audio", "other"] | None = None
    where: dict[str, Any] | None = None
    result_shape: Literal["cards", "assets", "directories"] = "cards"
    include_directories: bool = False
    directory_state_only: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class IndexJobScopeRequest(BaseModel):
    kind: Literal["folder"]
    source_id: str
    directory: str = ""


class IndexJobRequest(BaseModel):
    scope: IndexJobScopeRequest


@dataclass(frozen=True)
class _RuntimeParts:
    search_index: SearchIndex
    profile_sessions: SearchProfileSessionManager
    card_resolver: ResultCardResolver
    card_counts_by_directory: dict[tuple[str, str], int]


class _IndexJobManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        database_path: Path,
        reload_runtime: Callable[[], None],
    ) -> None:
        self._config = config
        self._database_path = database_path
        self._reload_runtime = reload_runtime
        self._lock = threading.Lock()
        self._current: dict[str, object] | None = None

    def start(self, scope: dict[str, str]) -> dict[str, object]:
        with self._lock:
            if self._current is not None and self._current["status"] in {
                "queued",
                "running",
            }:
                raise HTTPException(status_code=409, detail="index job is already running")
            job = {
                "id": uuid.uuid4().hex,
                "status": "queued",
                "scope": scope,
                "phase": "queued",
                "scanned_files": 0,
                "new_assets": 0,
                "written_assets": 0,
                "generated_thumbnails": 0,
                "failed_thumbnails": 0,
                "detail": "Waiting to start",
                "current_source_id": None,
                "current_relative_path": None,
                "current_action": None,
                "elapsed_seconds": 0.0,
                "error": None,
                "started_at": time.time(),
                "finished_at": None,
            }
            self._current = job
        thread = threading.Thread(
            target=self._run,
            args=(str(job["id"]), scope),
            name="findme-index-job",
            daemon=True,
        )
        thread.start()
        return self._job_response(job)

    def current(self) -> dict[str, object] | None:
        with self._lock:
            return self._job_response(self._current) if self._current is not None else None

    def job(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            if self._current is None or self._current["id"] != job_id:
                return None
            return self._job_response(self._current)

    def _run(self, job_id: str, scope: dict[str, str]) -> None:
        self._update(
            job_id,
            status="running",
            phase="indexing",
            detail="Scanning files",
        )
        try:
            source_id = scope["source_id"]
            directory = scope["directory"]

            def progress(progress: IndexProgress | object) -> None:
                if isinstance(progress, IndexProgress):
                    self._update(
                        job_id,
                        scanned_files=progress.scanned_files,
                        new_assets=progress.new_assets,
                        written_assets=progress.written_assets,
                        generated_thumbnails=progress.generated_thumbnails,
                        failed_thumbnails=progress.failed_thumbnails,
                        detail=_index_progress_detail(progress),
                        current_source_id=progress.current_source_id,
                        current_relative_path=progress.current_relative_path or None,
                        current_action=progress.current_action or None,
                        elapsed_seconds=progress.elapsed_seconds,
                    )

            with connect(self._database_path) as db:
                initialize_database(db)
                summary = index_sources(
                    db,
                    self._config,
                    source_ids=frozenset({source_id}),
                    directory_prefixes={source_id: directory},
                    progress=progress,
                )
                corpus_stale = search_corpus_is_stale(db)
            self._update(
                job_id,
                scanned_files=summary.scanned_files,
                new_assets=summary.new_assets,
                written_assets=summary.written_assets,
                generated_thumbnails=summary.generated_thumbnails,
                failed_thumbnails=len(summary.thumbnail_failures),
            )
            if corpus_stale:
                self._update(
                    job_id,
                    phase="rebuilding-search",
                    detail="Preparing search index",
                )
                rebuild_search_corpus(
                    self._config,
                    self._database_path,
                    log=lambda message: self._update(
                        job_id,
                        detail=_search_corpus_progress_detail(message),
                    ),
                )
            self._update(
                job_id,
                phase="reloading-search",
                detail="Reloading search runtime",
            )
            self._reload_runtime()
            self._update(
                job_id,
                status="succeeded",
                phase="complete",
                detail="Search runtime reloaded",
                finished_at=time.time(),
            )
        except Exception as exc:
            self._update(
                job_id,
                status="failed",
                phase="failed",
                error=str(exc),
                finished_at=time.time(),
            )

    def _update(self, job_id: str, **fields: object) -> None:
        with self._lock:
            if self._current is None or self._current["id"] != job_id:
                return
            self._current.update(fields)

    def _job_response(self, job: dict[str, object]) -> dict[str, object]:
        response = dict(job)
        started_at = float(response["started_at"])
        finished_at = response["finished_at"]
        response["elapsed_seconds"] = max(
            0.0,
            (float(finished_at) if finished_at is not None else time.time()) - started_at,
        )
        return response


def _index_progress_detail(progress: IndexProgress) -> str:
    relative_path = progress.current_relative_path
    if not relative_path:
        return "Scanning files"
    if progress.current_action == "unchanged":
        return f"Checked {relative_path}"
    if progress.current_action == "indexed":
        return f"Indexed {relative_path}"
    if progress.current_action == "thumbnail":
        return f"Generated thumbnail for {relative_path}"
    return relative_path


def _search_corpus_progress_detail(message: str) -> str:
    if message == "search preprocessing loading assets":
        return "Loading assets for search"
    if message.startswith("search preprocessing loaded assets"):
        return "Loaded assets for search"
    if message == "search snapshot build started":
        return "Building search snapshot"
    if message == "search snapshot write started":
        return "Writing search snapshot"
    if message.startswith("search snapshot write finished"):
        return "Search snapshot written"
    if message.startswith("search lexical index build started"):
        return "Building lexical search index"
    if message == "search lexical index build finished":
        return "Lexical search index built"
    if message == "search preprocessing publishing artifacts":
        return "Publishing search artifacts"
    if message.startswith("search preprocessing finished"):
        return "Search artifacts published"
    return message


def create_app(
    config: AppConfig, db_path: Path | None = None, static_dir: Path | None = None
) -> FastAPI:
    database_path = db_path or config.data_root / "findme.sqlite3"
    metadata_database_path = config.data_root / "metadata.sqlite3"

    def load_runtime_parts() -> _RuntimeParts:
        snapshot_path = search_corpus_snapshot_path(
            config.data_root,
            config.search_corpus_path,
        )
        lexical_path = search_corpus_lexical_path(
            config.data_root,
            config.search_corpus_path,
        )
        snapshot = load_search_corpus_snapshot(snapshot_path)
        with connect(database_path) as db:
            loaded_search_index = SearchIndex.load_from_snapshot(
                db,
                snapshot,
                expected_version=SEARCH_CORPUS_VERSION,
                lexical_index_path=lexical_path,
            )
        loaded_profile_sessions = SearchProfileSessionManager(loaded_search_index)
        loaded_card_resolver = ResultCardResolver.from_assets(
            [
                _asset_result_response(asset, score=0.0)
                for asset in loaded_search_index.assets
            ],
            card_metadata_by_asset_id=snapshot.card_metadata_by_asset_id(),
        )
        return _RuntimeParts(
            search_index=loaded_search_index,
            profile_sessions=loaded_profile_sessions,
            card_resolver=loaded_card_resolver,
            card_counts_by_directory=_card_counts_by_directory_subtree(
                loaded_search_index,
                loaded_card_resolver,
            ),
        )

    runtime_parts = load_runtime_parts()
    search_index = runtime_parts.search_index
    profile_sessions = runtime_parts.profile_sessions
    card_resolver = runtime_parts.card_resolver
    card_counts_by_directory = runtime_parts.card_counts_by_directory

    def reload_runtime() -> None:
        nonlocal search_index, profile_sessions, card_resolver, card_counts_by_directory
        runtime_parts = load_runtime_parts()
        search_index = runtime_parts.search_index
        profile_sessions = runtime_parts.profile_sessions
        card_resolver = runtime_parts.card_resolver
        card_counts_by_directory = runtime_parts.card_counts_by_directory

    index_jobs = _IndexJobManager(
        config=config,
        database_path=database_path,
        reload_runtime=reload_runtime,
    )
    search_revisions = _SearchRevisionRegistry()

    def resolve_view_scope(
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        view_source_id: str | None,
        view_directory: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        has_view_scope = view_source_id is not None or view_directory is not None
        if not has_view_scope:
            return source_id, directory, directory_prefix
        if view_source_id is None or view_directory is None:
            raise HTTPException(
                status_code=400,
                detail="view_source_id and view_directory must be provided together",
            )
        if source_id is not None or directory is not None or directory_prefix is not None:
            raise HTTPException(
                status_code=400,
                detail="view scope cannot be combined with source_id, directory, or directory_prefix",
            )
        return view_source_id, None, view_directory or None

    def session_and_preferences(
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        view_source_id: str | None,
        view_directory: str | None,
    ):
        profile_session = profile_sessions.session(db, search_session)
        preferences = (
            profile_sessions.view_preferences(
                db,
                search_session,
                source_id=view_source_id,
                directory=view_directory or "",
            )
            if view_source_id is not None
            else profile_session.preferences
        )
        return profile_session, preferences

    def needs_scoped_scope(
        *,
        preferences,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
    ) -> bool:
        return (
            preferences.included_asset_mask is not None
            or source_id is not None
            or directory is not None
            or directory_prefix is not None
        )

    def card_page_for_browse_scope(
        *,
        preferences,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[dict[str, object]], int]:
        if not needs_scoped_scope(
            preferences=preferences,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
        ):
            return card_resolver.browse_page(offset=offset, limit=limit)
        if (
            source_id is None
            and directory is None
            and directory_prefix is None
            and preferences.included_asset_index_order is not None
        ):
            total = _single_whitelist_card_count(
                preferences,
                card_counts_by_directory,
            )
            if total is not None:
                return card_resolver.page_for_asset_index_array(
                    search_index.browse_asset_index_array(preferences=preferences),
                    offset=offset,
                    limit=limit,
                    total=total,
                )
        return card_resolver.page_for_asset_index_array(
            search_index.browse_asset_index_array(
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
            ),
            offset=offset,
            limit=limit,
        )

    def card_page_for_search(
        *,
        query: str,
        mode: SearchMode,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        preferences,
        domain_vocabulary,
        offset: int,
        limit: int,
        control: SearchControl | None,
    ) -> tuple[list[dict[str, object]], int, int, bool]:
        def asset_response_for_index(asset_index: int, score: float) -> dict[str, object]:
            return _asset_result_response(search_index.assets[asset_index], score=score)

        raw_limit = max(offset + limit, limit)
        while True:
            page = search_index.search_asset_index_page(
                query,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                limit=raw_limit,
                offset=0,
                control=control,
            )
            visible_cards = card_resolver.search_card_count_for_asset_index_array(
                page.asset_indexes,
                offset=offset,
                limit=limit,
            )
            raw_exhausted = raw_limit >= page.total
            if visible_cards >= limit or raw_exhausted:
                cards, _observed_total = (
                    card_resolver.page_for_search_asset_index_arrays(
                        page.asset_indexes,
                        page.scores,
                        offset=offset,
                        limit=limit,
                        asset_response_for_index=asset_response_for_index,
                    )
                )
                return cards, page.total, offset + len(cards), not raw_exhausted
            raw_limit = min(page.total, raw_limit + max(limit, raw_limit))

    def asset_page_for_query(
        *,
        query: str,
        mode: SearchMode,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        preferences,
        domain_vocabulary,
        offset: int,
        limit: int,
        control: SearchControl | None,
    ) -> tuple[list[dict[str, object]], int]:
        if not query:
            asset_indexes = search_index.browse_asset_index_array(
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
                control=control,
            )
            total = len(asset_indexes)
            page_asset_indexes = asset_indexes[offset : offset + limit]
        else:
            page = search_index.search_asset_index_page(
                query,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                limit=limit,
                offset=offset,
                control=control,
            )
            total = page.total
            page_asset_indexes = page.asset_indexes
        page_asset_ids = tuple(
            search_index.assets[int(asset_index)].asset_id
            for asset_index in page_asset_indexes
        )
        tags_by_asset_id = _tags_by_asset_id_for_page_asset_ids(
            metadata_database_path,
            page_asset_ids,
        )
        with connect(database_path) as db:
            return [
                _asset_query_response(
                    config,
                    _load_asset_or_404(db, config, asset_id),
                    tags_by_asset_id.get(asset_id, []),
                )
                for asset_id in page_asset_ids
            ], total

    def _asset_ids_for_batch_scope(scope: BatchTagsScope) -> tuple[str, ...]:
        if scope.kind == "folder":
            if scope.source_id is None:
                raise HTTPException(status_code=400, detail="folder scope requires source_id")
            source_id = scope.source_id
            directory = None
            directory_prefix = scope.directory
            query = ""
            mode = "strict"
        else:
            source_id, directory, directory_prefix = resolve_view_scope(
                source_id=scope.source_id,
                directory=None,
                directory_prefix=scope.directory_prefix,
                view_source_id=scope.view_source_id,
                view_directory=scope.view_directory,
            )
            query = _trimmed_query(scope.q)
            mode = scope.mode
        try:
            asset_mask = _asset_filter_mask(
                metadata_database_path,
                search_index,
                media_type=None,
                where=scope.where,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with connect(database_path) as db:
            try:
                _profile_session, preferences = session_and_preferences(
                    db,
                    search_session=scope.search_session,
                    view_source_id=scope.view_source_id if scope.kind == "search" else None,
                    view_directory=scope.view_directory if scope.kind == "search" else None,
                )
            except SearchProfileValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        preferences = _preferences_with_asset_mask(search_index, preferences, asset_mask)
        domain_vocabulary = _load_domain_vocabulary(config)
        asset_indexes = search_index.search_asset_index_array(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
        )
        return tuple(
            search_index.assets[int(asset_index)].asset_id
            for asset_index in asset_indexes
        )

    app = FastAPI(title="Findme V2")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sources")
    def list_sources() -> dict[str, list[dict[str, object]]]:
        sources = [
            {
                "id": source.id,
                "display_name": source.display_name,
                "root": str(source.root),
                "priority": source.priority,
            }
            for source in sorted(config.sources, key=lambda item: item.priority, reverse=True)
        ]
        return {"sources": sources}

    @app.post("/api/index-jobs", status_code=202)
    def start_index_job(request: IndexJobRequest) -> dict[str, object]:
        source = _source_config_or_404(config, request.scope.source_id)
        directory = _validated_source_directory(source, request.scope.directory)
        job = index_jobs.start(
            {
                "kind": request.scope.kind,
                "source_id": source.id,
                "directory": directory,
            }
        )
        return {"job": job}

    @app.get("/api/index-jobs/current")
    def current_index_job() -> dict[str, object | None]:
        return {"job": index_jobs.current()}

    @app.get("/api/index-jobs/{job_id}")
    def get_index_job(job_id: str) -> dict[str, object]:
        job = index_jobs.job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="index job was not found")
        return {"job": job}

    @app.get("/api/tags")
    def list_existing_tags() -> dict[str, list[dict[str, str]]]:
        if not metadata_database_path.is_file():
            return {"tags": []}
        with connect_metadata(metadata_database_path) as metadata_db:
            initialize_metadata_database(metadata_db)
            return {"tags": [tag_response(tag) for tag in list_tags(metadata_db)]}

    @app.post("/api/search/query")
    def query_search(request: SearchQueryRequest) -> object:
        try:
            asset_mask = _asset_filter_mask(
                metadata_database_path,
                search_index,
                media_type=request.media_type,
                where=request.where,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source_id, directory, directory_prefix = resolve_view_scope(
            source_id=request.source_id,
            directory=request.directory,
            directory_prefix=request.directory_prefix,
            view_source_id=request.view_source_id,
            view_directory=request.view_directory,
        )
        with connect(database_path) as db:
            try:
                profile_session, preferences = session_and_preferences(
                    db,
                    search_session=request.search_session,
                    view_source_id=request.view_source_id,
                    view_directory=request.view_directory,
                )
                dirty_folder_subtree_keys = profile_sessions.dirty_folder_subtree_keys(
                    profile_session
                )
                (
                    descendant_whitelist_keys,
                    descendant_blacklist_keys,
                ) = profile_sessions.selection_descendant_rule_keys(profile_session)
            except SearchProfileValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        preferences = _preferences_with_asset_mask(search_index, preferences, asset_mask)
        control = search_revisions.control(request.search_session, request.search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        trimmed_query = _trimmed_query(request.q)
        domain_vocabulary = _load_domain_vocabulary(config) if trimmed_query else None
        try:
            directories = None
            if request.include_directories or request.result_shape == "directories":
                if trimmed_query:
                    directories = search_index.directory_responses_for_search(
                        trimmed_query,
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        preferences=preferences,
                        match_asset_mask=asset_mask,
                        domain_vocabulary=domain_vocabulary,
                        mode=request.mode,
                        dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                        descendant_whitelist_keys=descendant_whitelist_keys,
                        descendant_blacklist_keys=descendant_blacklist_keys,
                        control=control,
                    )
                else:
                    directories = search_index.directory_responses_for_scope(
                        preferences=preferences,
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                        match_asset_mask=asset_mask,
                        descendant_whitelist_keys=descendant_whitelist_keys,
                        descendant_blacklist_keys=descendant_blacklist_keys,
                        control=control,
                    )
            if request.result_shape == "directories":
                payload: dict[str, object] = {}
            elif request.result_shape == "assets":
                assets, total = asset_page_for_query(
                    query=trimmed_query,
                    mode=request.mode,
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    preferences=preferences,
                    domain_vocabulary=domain_vocabulary,
                    offset=request.offset,
                    limit=request.limit,
                    control=control,
                )
                next_offset = request.offset + len(assets)
                payload: dict[str, object] = {
                    "assets": assets,
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": _has_more(next_offset, total),
                }
            elif not trimmed_query:
                cards, total = card_page_for_browse_scope(
                    preferences=preferences,
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    offset=request.offset,
                    limit=request.limit,
                )
                next_offset = request.offset + len(cards)
                payload = {
                    "cards": _cards_with_tags(metadata_database_path, cards),
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": _has_more(next_offset, total),
                }
            else:
                cards, total, next_offset, has_more = card_page_for_search(
                    query=trimmed_query,
                    mode=request.mode,
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    preferences=preferences,
                    domain_vocabulary=domain_vocabulary,
                    offset=request.offset,
                    limit=request.limit,
                    control=control,
                )
                payload = {
                    "cards": _cards_with_tags(metadata_database_path, cards),
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": has_more,
                }
        except SearchSuperseded:
            return Response(status_code=204)
        if directories is not None:
            payload.update(
                _directory_payload(
                    config,
                    directories,
                    state_only=request.directory_state_only,
                )
            )
        return JSONResponse(payload)

    @app.get("/api/assets")
    def list_assets(
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        view_source_id: str | None = None,
        view_directory: str | None = None,
        search_session: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        source_id, directory, directory_prefix = resolve_view_scope(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            view_source_id=view_source_id,
            view_directory=view_directory,
        )
        with connect(database_path) as db:
            try:
                _profile_session, preferences = session_and_preferences(
                    db,
                    search_session=search_session,
                    view_source_id=view_source_id,
                    view_directory=view_directory,
                )
            except SearchProfileValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        cards, total = card_page_for_browse_scope(
            preferences=preferences,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            offset=offset,
            limit=limit,
        )
        next_offset = offset + len(cards)
        return JSONResponse(
            {
                "cards": _cards_with_tags(metadata_database_path, cards),
                "total": total,
                "next_offset": next_offset,
                "has_more": _has_more(next_offset, total),
            }
        )

    @app.get("/api/search-profiles")
    def list_search_profiles(search_session: str | None = None) -> dict[str, object]:
        with connect(database_path) as db:
            return profile_sessions.profiles_response(db, search_session)

    @app.post("/api/search-profiles/select")
    def select_search_profile(request: SearchProfileSelectRequest) -> dict[str, object]:
        try:
            with connect(database_path) as db:
                session = profile_sessions.select_profile(
                    db,
                    search_session=request.search_session,
                    profile_id=request.profile_id,
                    discard=request.discard,
                )
                return {"session": profile_sessions.session_response(session)}
        except SearchProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="search profile was not found") from exc

    @app.post("/api/search-profiles")
    def create_search_profile(request: SearchProfileSaveRequest) -> dict[str, object]:
        try:
            with connect(database_path) as db:
                session = profile_sessions.create_profile(
                    db,
                    search_session=request.search_session,
                    name=request.name or "",
                )
                return profile_sessions.profiles_response(db, session.search_session)
        except SearchProfileValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/search-profiles/{profile_id}")
    def delete_search_profile(
        profile_id: str,
        search_session: str | None = None,
        discard: bool = False,
    ) -> dict[str, object]:
        try:
            with connect(database_path) as db:
                session = profile_sessions.delete_profile(
                    db,
                    search_session=search_session,
                    profile_id=profile_id,
                    discard=discard,
                )
                return {"session": profile_sessions.session_response(session)}
        except SearchProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SearchProfileValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="search profile was not found") from exc

    @app.patch("/api/search-profile/settings")
    def update_search_profile_settings(
        request: SearchProfileSettingsRequest,
    ) -> dict[str, object]:
        try:
            with connect(database_path) as db:
                update_kwargs = {}
                fields_set = request.model_fields_set
                if "quality_override" in fields_set:
                    update_kwargs["quality_override"] = request.quality_override
                if "selection_state" in fields_set:
                    update_kwargs["selection_state"] = request.selection_state
                session = profile_sessions.update_setting(
                    db,
                    search_session=request.search_session,
                    source_id=request.source_id,
                    directory=request.directory,
                    **update_kwargs,  # type: ignore[arg-type]
                )
                return {"session": profile_sessions.session_response(session)}
        except SearchProfileValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/search-profile/save")
    def save_search_profile(request: SearchProfileSaveRequest) -> dict[str, object]:
        try:
            with connect(database_path) as db:
                session = profile_sessions.save(
                    db,
                    search_session=request.search_session,
                    name=request.name,
                    overwrite=request.overwrite,
                )
                return {
                    "session": profile_sessions.session_response(session),
                    **profile_sessions.profiles_response(db, request.search_session),
                }
        except SearchProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SearchProfileValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/search-profile/reset-scoring")
    def reset_search_profile_scoring(
        request: SearchProfileSessionRequest,
    ) -> dict[str, object]:
        with connect(database_path) as db:
            session = profile_sessions.reset_scoring(
                db,
                search_session=request.search_session,
            )
            return {"session": profile_sessions.session_response(session)}

    @app.post("/api/search-profile/reset-selection")
    def reset_search_profile_selection(
        request: SearchProfileSessionRequest,
    ) -> dict[str, object]:
        with connect(database_path) as db:
            session = profile_sessions.reset_selection(
                db,
                search_session=request.search_session,
            )
            return {"session": profile_sessions.session_response(session)}

    @app.get("/api/assets/{asset_id}")
    def asset_detail(asset_id: str) -> dict[str, object]:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        response = asset_response(asset)
        response["tags"] = _tags_for_asset_response(metadata_database_path, asset)
        return {"asset": response, "actions": asset_actions(config, asset)}

    @app.put("/api/assets/{asset_id}/tags")
    def update_asset_tags(
        asset_id: str,
        request: AssetTagsRequest,
    ) -> dict[str, object]:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        if not asset.absolute_path.is_file():
            raise HTTPException(status_code=404, detail="asset file is missing")
        try:
            with connect_metadata(metadata_database_path) as metadata_db:
                initialize_metadata_database(metadata_db)
                tags = set_asset_tags(metadata_db, asset, request.tags)
        except MetadataValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"tags": [tag_response(tag) for tag in tags]}

    @app.post("/api/tags/batch")
    def batch_update_tags(request: BatchTagsRequest) -> dict[str, object]:
        if bool(request.asset_ids) == bool(request.scope):
            raise HTTPException(
                status_code=400,
                detail="provide exactly one of asset_ids or scope",
            )
        if not request.add_tags and not request.remove_tags:
            raise HTTPException(status_code=400, detail="add_tags or remove_tags is required")
        asset_ids = (
            tuple(dict.fromkeys(request.asset_ids))
            if request.asset_ids
            else _asset_ids_for_batch_scope(request.scope)  # type: ignore[arg-type]
        )
        tagged = 0
        failures: list[dict[str, str]] = []
        with connect_metadata(metadata_database_path) as metadata_db:
            initialize_metadata_database(metadata_db)
            for asset_id in asset_ids:
                try:
                    with connect(database_path) as db:
                        asset = get_asset(db, config, asset_id)
                    if not asset.absolute_path.is_file():
                        raise AssetNotFoundError("asset file is missing")
                    if request.add_tags:
                        add_asset_tags(metadata_db, asset, request.add_tags)
                    if request.remove_tags:
                        remove_asset_tags(metadata_db, asset, request.remove_tags)
                    tagged += 1
                except (
                    AssetNotFoundError,
                    MetadataValidationError,
                    UnsafeAssetPathError,
                ) as exc:
                    failures.append({"asset_id": asset_id, "message": str(exc)})
        return {
            "matched": len(asset_ids),
            "tagged": tagged,
            "failed": failures,
        }

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str, download: bool = False) -> FileResponse:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        if not asset.absolute_path.is_file():
            raise HTTPException(status_code=404, detail="asset file is missing")
        return FileResponse(
            asset.absolute_path,
            filename=asset.filename if download else None,
            media_type=None,
        )

    @app.get("/api/assets/{asset_id}/thumb")
    def asset_thumbnail(
        asset_id: str,
        max_edge: int | None = Query(default=None, ge=1),
    ) -> FileResponse:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        cache_path = next(
            (
                variant.cache_path
                for variant in asset.thumbnail_variants
                if max_edge is None or variant.max_edge == max_edge
            ),
            None,
        )
        if cache_path is None or not cache_path.is_file():
            raise HTTPException(status_code=404, detail="asset thumbnail is missing")
        return FileResponse(
            cache_path,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    return app


class _SearchRevisionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest_by_session: dict[str, tuple[int, float]] = {}

    def control(
        self,
        session: str | None,
        revision: int | None,
    ) -> SearchControl | None:
        if session is None or revision is None:
            return None
        with self._lock:
            current = self._latest_by_session.get(session)
            if current is None or revision > current[0]:
                self._latest_by_session[session] = (revision, time.monotonic())
            else:
                self._latest_by_session[session] = (current[0], time.monotonic())

        return SearchControl(lambda: not self._is_current(session, revision))

    def _is_current(self, session: str, revision: int) -> bool:
        with self._lock:
            current = self._latest_by_session.get(session)
            return current is not None and current[0] == revision


def _load_domain_vocabulary(config: AppConfig):
    if config.domain_vocabulary_path is None:
        return None
    return load_domain_vocabulary(config.domain_vocabulary_path)


def _trimmed_query(query: str | None) -> str:
    return query.strip() if query is not None else ""


def _has_more(next_offset: int, total: int) -> bool:
    return next_offset < total


TagPredicate = Callable[[set[str]], bool]

_MAX_TAG_WHERE_DEPTH = 8
_MAX_TAG_WHERE_LEAVES = 100


def _compile_tag_where(where: dict[str, Any] | None) -> TagPredicate:
    if where is None:
        return lambda _tags: True
    leaf_count = _validate_tag_where(where)
    if leaf_count > _MAX_TAG_WHERE_LEAVES:
        raise ValueError(f"where may contain at most {_MAX_TAG_WHERE_LEAVES} tag clauses")

    def predicate(tags: set[str]) -> bool:
        return _tag_where_matches(where, tags)

    return predicate


def _validate_tag_where(where: object, *, depth: int = 0) -> int:
    if depth > _MAX_TAG_WHERE_DEPTH:
        raise ValueError(f"where may be nested at most {_MAX_TAG_WHERE_DEPTH} levels")
    if not isinstance(where, dict):
        raise ValueError("where clause must be an object")
    if len(where) != 1:
        raise ValueError("where clause must contain exactly one operator")
    operator, value = next(iter(where.items()))
    if operator == "tag":
        if not isinstance(value, str) or not value:
            raise ValueError("tag must be a non-empty string")
        return 1
    if operator in {"and", "or"}:
        if not isinstance(value, list):
            raise ValueError(f"{operator} must contain a list of clauses")
        if not value:
            raise ValueError(f"{operator} must contain at least one clause")
        return sum(_validate_tag_where(item, depth=depth + 1) for item in value)
    if operator == "not":
        return _validate_tag_where(value, depth=depth + 1)
    raise ValueError(f"unsupported where operator: {operator}")


def _tag_where_matches(where: dict[str, Any], tags: set[str]) -> bool:
    operator, value = next(iter(where.items()))
    if operator == "tag":
        return value in tags
    if operator == "and":
        return all(_tag_where_matches(item, tags) for item in value)
    if operator == "or":
        return any(_tag_where_matches(item, tags) for item in value)
    if operator == "not":
        return not _tag_where_matches(value, tags)
    raise AssertionError(f"validated where operator became unsupported: {operator}")


def _tag_slugs_in_where(where: dict[str, Any]) -> tuple[str, ...]:
    operator, value = next(iter(where.items()))
    if operator == "tag":
        return (value,)
    if operator in {"and", "or"}:
        return tuple(
            dict.fromkeys(
                slug
                for item in value
                for slug in _tag_slugs_in_where(item)
            )
        )
    if operator == "not":
        return _tag_slugs_in_where(value)
    raise AssertionError(f"validated where operator became unsupported: {operator}")


def _asset_filter_mask(
    metadata_database_path: Path,
    search_index: SearchIndex,
    *,
    media_type: str | None,
    where: dict[str, Any] | None,
) -> np.ndarray | None:
    masks: list[np.ndarray] = []
    if media_type is not None:
        masks.append(
            np.fromiter(
                (asset.media_type == media_type for asset in search_index.assets),
                dtype=bool,
                count=len(search_index.assets),
            )
        )
    if where is not None:
        tag_predicate = _compile_tag_where(where)
        tags_by_asset_id = _tag_slugs_by_asset_id_for_where(
            metadata_database_path,
            where,
        )
        masks.append(
            np.fromiter(
                (
                    tag_predicate(tags_by_asset_id.get(asset.asset_id, set()))
                    for asset in search_index.assets
                ),
                dtype=bool,
                count=len(search_index.assets),
            )
        )
    if not masks:
        return None
    combined = masks[0]
    for mask in masks[1:]:
        combined = np.logical_and(combined, mask)
    return combined


def _tag_slugs_by_asset_id_for_where(
    metadata_database_path: Path,
    where: dict[str, Any],
) -> dict[str, set[str]]:
    slugs = _tag_slugs_in_where(where)
    if not slugs or not metadata_database_path.is_file():
        return {}
    placeholders = ", ".join("?" for _ in slugs)
    with connect_metadata(metadata_database_path) as metadata_db:
        initialize_metadata_database(metadata_db)
        rows = metadata_db.execute(
            f"""
            SELECT metadata_file_locations.asset_id, tags.slug
            FROM tags
            JOIN file_tags ON file_tags.tag_id = tags.tag_id
            JOIN metadata_file_locations
              ON metadata_file_locations.file_id = file_tags.file_id
            WHERE tags.slug IN ({placeholders})
              AND metadata_file_locations.asset_id IS NOT NULL
              AND metadata_file_locations.missing_at IS NULL
            """,
            slugs,
        ).fetchall()
    tags_by_asset_id: dict[str, set[str]] = {}
    for row in rows:
        tags_by_asset_id.setdefault(row["asset_id"], set()).add(row["slug"])
    return tags_by_asset_id


def _preferences_with_asset_mask(
    search_index: SearchIndex,
    preferences,
    mask: np.ndarray | None,
):
    if mask is None:
        return preferences
    combined_mask = (
        mask
        if preferences.included_asset_mask is None
        else np.logical_and(preferences.included_asset_mask, mask)
    )
    included_asset_indexes = np.flatnonzero(combined_mask).astype(np.int32, copy=False)
    included_directory_count_array = search_index._directory_count_array_for_asset_indexes(
        included_asset_indexes
    )
    included_directory_counts = {
        key: int(included_directory_count_array[directory_id])
        for directory_id, key in enumerate(search_index.directory_runtime.keys)
        if included_directory_count_array[directory_id] > 0
    }
    return replace(
        preferences,
        included_asset_mask=combined_mask,
        included_asset_index_order=None,
        included_directory_counts=included_directory_counts,
        included_directory_count_array=included_directory_count_array,
    )


def _load_asset_or_404(db: sqlite3.Connection, config: AppConfig, asset_id: str):
    try:
        return get_asset(db, config, asset_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail="asset not found") from exc
    except UnsafeAssetPathError as exc:
        raise HTTPException(status_code=500, detail="asset path is outside source root") from exc


def _source_config_or_404(config: AppConfig, source_id: str):
    source = next((source for source in config.sources if source.id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="source was not found")
    return source


def _validated_source_directory(source, directory: str) -> str:
    normalized = directory.strip("/")
    if "\\" in normalized:
        raise HTTPException(status_code=400, detail="directory must use forward slashes")
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="directory must be source-relative")
    target = source.root.joinpath(*parts).resolve()
    if target != source.root and not target.is_relative_to(source.root):
        raise HTTPException(status_code=400, detail="directory must be source-relative")
    return "/".join(parts)


def _tags_for_asset_response(metadata_database_path: Path, asset) -> list[dict[str, str]]:
    if not metadata_database_path.is_file():
        return []
    with connect_metadata(metadata_database_path) as metadata_db:
        initialize_metadata_database(metadata_db)
        return [tag_response(tag) for tag in tags_for_asset(metadata_db, asset)]


def _cards_with_tags(
    metadata_database_path: Path,
    cards: list[dict[str, object]],
) -> list[dict[str, object]]:
    tags_by_asset_id = _tags_by_asset_id_for_cards(metadata_database_path, cards)
    if not tags_by_asset_id:
        return cards
    return [_card_with_tags(card, tags_by_asset_id) for card in cards]


def _card_with_tags(
    card: dict[str, object],
    tags_by_asset_id: dict[str, list[dict[str, str]]],
) -> dict[str, object]:
    if card["kind"] == "asset":
        response = dict(card)
        response["asset"] = _asset_with_tags(
            card["asset"],  # type: ignore[arg-type]
            tags_by_asset_id,
        )
        return response
    response = dict(card)
    response["representative"] = _asset_with_tags(
        card["representative"],  # type: ignore[arg-type]
        tags_by_asset_id,
    )
    response["variants"] = [
        _asset_with_tags(asset, tags_by_asset_id)
        for asset in card["variants"]  # type: ignore[union-attr]
    ]
    return response


def _asset_with_tags(
    asset: dict[str, object],
    tags_by_asset_id: dict[str, list[dict[str, str]]],
) -> dict[str, object]:
    response = dict(asset)
    response["tags"] = tags_by_asset_id.get(str(asset["asset_id"]), [])
    return response


def _tags_by_asset_id_for_cards(
    metadata_database_path: Path,
    cards: list[dict[str, object]],
) -> dict[str, list[dict[str, str]]]:
    asset_ids = tuple(dict.fromkeys(_asset_ids_for_cards(cards)))
    return _tags_by_asset_id_for_page_asset_ids(metadata_database_path, asset_ids)


def _tags_by_asset_id_for_page_asset_ids(
    metadata_database_path: Path,
    asset_ids: tuple[str, ...],
) -> dict[str, list[dict[str, str]]]:
    if not asset_ids or not metadata_database_path.is_file():
        return {}
    placeholders = ", ".join("?" for _ in asset_ids)
    with connect_metadata(metadata_database_path) as metadata_db:
        initialize_metadata_database(metadata_db)
        rows = metadata_db.execute(
            f"""
            SELECT
              metadata_file_locations.asset_id,
              tags.slug,
              tags.label
            FROM metadata_file_locations
            JOIN file_tags ON file_tags.file_id = metadata_file_locations.file_id
            JOIN tags ON tags.tag_id = file_tags.tag_id
            WHERE metadata_file_locations.asset_id IN ({placeholders})
              AND metadata_file_locations.missing_at IS NULL
            ORDER BY
              metadata_file_locations.asset_id,
              CASE
                WHEN tags.slug GLOB '[0-9]*' AND tags.slug NOT GLOB '*[^0-9]*'
                THEN 0
                ELSE 1
              END,
              CASE
                WHEN tags.slug GLOB '[0-9]*' AND tags.slug NOT GLOB '*[^0-9]*'
                THEN CAST(tags.slug AS INTEGER)
                ELSE 0
              END,
              tags.label COLLATE NOCASE,
              tags.slug
            """,
            asset_ids,
        ).fetchall()
    tags_by_asset_id: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        tags_by_asset_id.setdefault(row["asset_id"], []).append(
            {"slug": row["slug"], "label": row["label"]}
        )
    return tags_by_asset_id


def _asset_ids_for_cards(cards: list[dict[str, object]]) -> list[str]:
    asset_ids: list[str] = []
    for card in cards:
        if card["kind"] == "asset":
            asset = card["asset"]  # type: ignore[assignment]
            asset_ids.append(str(asset["asset_id"]))
            continue
        representative = card["representative"]  # type: ignore[assignment]
        asset_ids.append(str(representative["asset_id"]))
        for asset in card["variants"]:  # type: ignore[union-attr]
            asset_ids.append(str(asset["asset_id"]))
    return asset_ids


def _asset_query_response(
    config: AppConfig,
    asset,
    tags: list[dict[str, str]],
) -> dict[str, object]:
    response = asset_response(asset)
    response["mtime_ns"] = asset.mtime_ns
    response["absolute_path"] = str(asset.absolute_path)
    response["tags"] = tags
    response["actions"] = asset_actions(config, asset)
    return response


def _search_response(result: SearchResult) -> dict[str, object]:
    return _asset_result_response(result, score=result.score)


def _directory_payload(
    config: AppConfig,
    directories: list[dict[str, object]],
    *,
    state_only: bool,
) -> dict[str, object]:
    if state_only:
        return {
            "directory_states": [
                _directory_state_response(directory) for directory in directories
            ]
        }
    return {"directories": [_directory_response(config, directory) for directory in directories]}


def _directory_response(
    config: AppConfig,
    directory: dict[str, object],
) -> dict[str, object]:
    response = dict(directory)
    file_browser_url = _directory_file_browser_url(
        config,
        source_id=str(directory["source_id"]),
        path=str(directory["path"]),
    )
    if file_browser_url is not None:
        response["file_browser_url"] = file_browser_url
    return response


def _directory_file_browser_url(
    config: AppConfig,
    *,
    source_id: str,
    path: str,
) -> str | None:
    source = next((source for source in config.sources if source.id == source_id), None)
    if source is None or source.file_browser_url_prefix is None:
        return None
    if not path:
        return source.file_browser_url_prefix
    encoded_path = "/".join(quote(segment) for segment in path.split("/"))
    return f"{source.file_browser_url_prefix}/{encoded_path}"


def _directory_state_response(directory: dict[str, object]) -> list[object]:
    return [
        directory["id"],
        directory["matched_asset_count"],
        directory["included_asset_count"],
        directory["quality"],
        directory["quality_override"],
        directory["selection_state"],
        directory["selection_effective"],
        directory["profile_dirty"],
        directory["has_descendant_whitelist"],
        directory["has_descendant_blacklist"],
    ]


def _asset_result_response(result, *, score: float) -> dict[str, object]:
    response = {
        "asset_id": result.asset_id,
        "source_id": result.source_id,
        "source_display_name": result.source_display_name,
        "relative_path": result.relative_path,
        "filename": result.filename,
        "stem": result.stem,
        "extension": result.extension,
        "directory": result.directory,
        "size_bytes": result.size_bytes,
        "media_type": result.media_type,
        "media_width": result.media_width,
        "media_height": result.media_height,
        "duration_seconds": result.duration_seconds,
        "score": score,
    }
    if result.thumbnail_variants:
        baseline = result.thumbnail_variants[0]
        baseline_response = thumbnail_variant_response(
            result.asset_id,
            baseline,
            size_bytes=result.size_bytes,
            mtime_ns=result.mtime_ns,
        )
        response["thumbnail_url"] = baseline_response["url"]
        response["thumbnail_width"] = baseline.width
        response["thumbnail_height"] = baseline.height
        response["thumbnail_variants"] = [
            thumbnail_variant_response(
                result.asset_id,
                variant,
                size_bytes=result.size_bytes,
                mtime_ns=result.mtime_ns,
            )
            for variant in result.thumbnail_variants
        ]
    return response


def _card_counts_by_directory_subtree(
    search_index: SearchIndex,
    card_resolver: ResultCardResolver,
) -> dict[tuple[str, str], int]:
    card_keys_by_directory: dict[tuple[str, str], set[str]] = defaultdict(set)
    for asset in search_index.assets:
        card_key = card_resolver.card_key_for_asset_index(asset.asset_index)
        for directory_key in search_index.asset_directory_keys[asset.asset_index]:
            card_keys_by_directory[directory_key].add(card_key)
    return {
        directory_key: len(card_keys)
        for directory_key, card_keys in card_keys_by_directory.items()
    }


def _single_whitelist_card_count(
    preferences,
    card_counts_by_directory: dict[tuple[str, str], int],
) -> int | None:
    effective_rules = [
        (source_id, directory, state)
        for (source_id, directory), state in preferences.effective_selection_states.items()
    ]
    if len(effective_rules) != 1:
        return None
    source_id, directory, state = effective_rules[0]
    if state != "whitelist":
        return None
    return card_counts_by_directory.get((source_id, directory), 0)
