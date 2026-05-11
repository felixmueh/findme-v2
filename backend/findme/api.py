from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from findme.assets import (
    AssetNotFoundError,
    UnsafeAssetPathError,
    asset_actions,
    asset_response,
    get_asset,
    thumbnail_url,
)
from findme.config import AppConfig
from findme.db import connect
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


def create_app(
    config: AppConfig, db_path: Path | None = None, static_dir: Path | None = None
) -> FastAPI:
    database_path = db_path or config.data_root / "findme.sqlite3"
    snapshot_path = search_corpus_snapshot_path(config.data_root, config.search_corpus_path)
    lexical_path = search_corpus_lexical_path(config.data_root, config.search_corpus_path)
    snapshot = load_search_corpus_snapshot(snapshot_path)
    with connect(database_path) as db:
        search_index = SearchIndex.load_from_snapshot(
            db,
            snapshot,
            expected_version=SEARCH_CORPUS_VERSION,
            lexical_index_path=lexical_path,
    )
    card_metadata_by_asset_id = snapshot.card_metadata_by_asset_id()
    profile_sessions = SearchProfileSessionManager(search_index)
    card_resolver = ResultCardResolver.from_assets(
        [_asset_search_response(asset, score=0.0) for asset in search_index.assets],
        card_metadata_by_asset_id=card_metadata_by_asset_id,
    )
    card_counts_by_directory = _card_counts_by_directory_subtree(
        search_index,
        card_resolver,
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
        return JSONResponse({"cards": cards, "total": total})

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

    @app.get("/api/search")
    def search(
        q: str | None = None,
        mode: SearchMode = "strict",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        view_source_id: str | None = None,
        view_directory: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
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
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        trimmed_query = _trimmed_query(q)
        if not trimmed_query:
            cards, total = card_page_for_browse_scope(
                preferences=preferences,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                offset=offset,
                limit=limit,
            )
            return JSONResponse({"cards": cards, "total": total})
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            page = search_index.search(
                trimmed_query,
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
        except SearchSuperseded:
            return Response(status_code=204)
        cards, _card_total = card_resolver.page_for(
            [_search_response(result) for result in page.assets],
            offset=0,
            limit=limit,
        )
        return JSONResponse({"cards": cards, "total": page.total})

    @app.get("/api/search-with-directory-counts")
    def search_with_directory_counts(
        q: str | None = None,
        mode: SearchMode = "strict",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        view_source_id: str | None = None,
        view_directory: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        source_id, directory, directory_prefix = resolve_view_scope(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            view_source_id=view_source_id,
            view_directory=view_directory,
        )
        with connect(database_path) as db:
            try:
                profile_session, preferences = session_and_preferences(
                    db,
                    search_session=search_session,
                    view_source_id=view_source_id,
                    view_directory=view_directory,
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
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        trimmed_query = _trimmed_query(q)
        if not trimmed_query:
            cards, total = card_page_for_browse_scope(
                preferences=preferences,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                offset=offset,
                limit=limit,
            )
            try:
                directories = search_index.directory_responses_for_scope(
                    preferences=preferences,
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                    descendant_whitelist_keys=descendant_whitelist_keys,
                    descendant_blacklist_keys=descendant_blacklist_keys,
                    control=control,
                )
            except SearchSuperseded:
                return Response(status_code=204)
            return JSONResponse(
                {
                    "cards": cards,
                    "total": total,
                    "directories": directories,
                }
            )
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            search_response = search_index.search_with_directory_responses(
                trimmed_query,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                limit=limit,
                offset=offset,
                dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                descendant_whitelist_keys=descendant_whitelist_keys,
                descendant_blacklist_keys=descendant_blacklist_keys,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        cards, _card_total = card_resolver.page_for(
            [_search_response(result) for result in search_response.page.assets],
            offset=0,
            limit=limit,
        )
        return JSONResponse(
            {
                "cards": cards,
                "total": search_response.page.total,
                "directories": search_response.directories,
            }
        )

    @app.get("/api/assets/{asset_id}")
    def asset_detail(asset_id: str) -> dict[str, object]:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        return {"asset": asset_response(asset), "actions": asset_actions(config, asset)}

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
    def asset_thumbnail(asset_id: str) -> FileResponse:
        with connect(database_path) as db:
            asset = _load_asset_or_404(db, config, asset_id)
        if asset.thumbnail_path is None or not asset.thumbnail_path.is_file():
            raise HTTPException(status_code=404, detail="asset thumbnail is missing")
        return FileResponse(
            asset.thumbnail_path,
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


def _load_asset_or_404(db: sqlite3.Connection, config: AppConfig, asset_id: str):
    try:
        return get_asset(db, config, asset_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail="asset not found") from exc
    except UnsafeAssetPathError as exc:
        raise HTTPException(status_code=500, detail="asset path is outside source root") from exc


def _search_response(result: SearchResult) -> dict[str, object]:
    return _asset_search_response(result, score=result.score)


def _asset_search_response(result, *, score: float) -> dict[str, object]:
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
        "score": score,
    }
    if result.thumbnail_path is not None:
        response["thumbnail_url"] = thumbnail_url(
            result.asset_id,
            size_bytes=result.size_bytes,
            mtime_ns=result.mtime_ns,
        )
        response["thumbnail_width"] = result.thumbnail_width
        response["thumbnail_height"] = result.thumbnail_height
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
