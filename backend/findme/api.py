from __future__ import annotations

import json
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
    DirectoryFilter,
    DirectoryFilterRule,
    DirectoryMatch,
    DomainVocabulary,
    SearchIndex,
    SearchMode,
    SearchResult,
    SearchControl,
    SearchSuperseded,
    load_domain_vocabulary,
    upsert_folder_quality,
)
from findme.search_corpus import (
    SEARCH_CORPUS_VERSION,
    load_search_corpus_snapshot,
    search_corpus_lexical_path,
    search_corpus_snapshot_path,
)


class FolderQualityRequest(BaseModel):
    source_id: str
    directory: str = ""
    quality: int = Field(ge=0, le=10)


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
    card_resolver = ResultCardResolver.from_assets(
        [_search_response(result) for result in search_index.scoped_assets()],
        card_metadata_by_asset_id=card_metadata_by_asset_id,
    )
    search_revisions = _SearchRevisionRegistry()

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
        filter_rule: list[str] = Query(default_factory=list),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        directory_filter = _parse_directory_filter(filter_rule, config, search_index)
        scoped_assets = search_index.scoped_assets(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            directory_filter=directory_filter,
        )
        scoped_card_resolver = (
            ResultCardResolver.from_assets(
                [_search_response(result) for result in scoped_assets],
                card_metadata_by_asset_id=card_metadata_by_asset_id,
            )
            if directory_filter or source_id or directory is not None or directory_prefix
            else card_resolver
        )
        cards, total = scoped_card_resolver.page_for(
            [_search_response(result) for result in scoped_assets],
            offset=offset,
            limit=limit,
        )
        return JSONResponse({"cards": cards, "total": total})

    @app.get("/api/directories")
    def list_directories(
        source_id: str | None = None,
        q: str | None = None,
        mode: SearchMode = "combined",
        filter_rule: list[str] = Query(default_factory=list),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        directory_filter = _parse_directory_filter(filter_rule, config, search_index)
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            directories = search_index.directory_matches(
                q,
                source_id=source_id,
                directory_filter=directory_filter,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        return {"directories": [_directory_response(directory) for directory in directories]}

    @app.put("/api/folder-quality")
    def set_folder_quality(request: FolderQualityRequest) -> dict[str, object]:
        if (request.source_id, request.directory) not in search_index.directory_counts:
            raise HTTPException(status_code=404, detail="indexed folder was not found") from None
        with connect(database_path) as db:
            upsert_folder_quality(
                db,
                source_id=request.source_id,
                directory=request.directory,
                quality=request.quality,
            )
        search_index.set_folder_quality(
            request.source_id,
            request.directory,
            request.quality,
        )
        quality, quality_override = search_index.folder_quality(
            request.source_id,
            request.directory,
        )
        return {
            "source_id": request.source_id,
            "directory": request.directory,
            "quality": quality,
            "quality_override": quality_override,
        }

    @app.get("/api/search")
    def search(
        q: str | None = None,
        mode: SearchMode = "combined",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        filter_rule: list[str] = Query(default_factory=list),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        directory_filter = _parse_directory_filter(filter_rule, config, search_index)
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            matched_assets = _matched_assets(
                search_index,
                q,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                directory_filter=directory_filter,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        scoped_card_resolver = (
            ResultCardResolver.from_assets(
                [
                    _search_response(result)
                    for result in search_index.scoped_assets(
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        directory_filter=directory_filter,
                    )
                ],
                card_metadata_by_asset_id=card_metadata_by_asset_id,
            )
            if directory_filter or source_id or directory is not None or directory_prefix
            else card_resolver
        )
        cards, total = scoped_card_resolver.page_for(
            [_search_response(result) for result in matched_assets],
            offset=offset,
            limit=limit,
        )
        return JSONResponse({"cards": cards, "total": total})

    @app.get("/api/search-with-directory-counts")
    def search_with_directory_counts(
        q: str | None = None,
        mode: SearchMode = "combined",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        filter_rule: list[str] = Query(default_factory=list),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        directory_filter = _parse_directory_filter(filter_rule, config, search_index)
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            matched_assets = _matched_assets(
                search_index,
                q,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                directory_filter=directory_filter,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        scoped_card_resolver = (
            ResultCardResolver.from_assets(
                [
                    _search_response(result)
                    for result in search_index.scoped_assets(
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        directory_filter=directory_filter,
                    )
                ],
                card_metadata_by_asset_id=card_metadata_by_asset_id,
            )
            if directory_filter or source_id or directory is not None or directory_prefix
            else card_resolver
        )
        cards, total = scoped_card_resolver.page_for(
            [_search_response(result) for result in matched_assets],
            offset=offset,
            limit=limit,
        )
        try:
            directories = search_index.directory_matches(
                q,
                source_id=source_id,
                directory_filter=directory_filter,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        return JSONResponse(
            {
                "cards": cards,
                "total": total,
                "directories": [
                    _directory_response(directory) for directory in directories
                ],
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


def _matched_assets(
    search_index: SearchIndex,
    query: str | None,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    directory_filter: DirectoryFilter | None,
    domain_vocabulary: DomainVocabulary | None,
    mode: SearchMode,
    control: SearchControl | None,
) -> list[SearchResult]:
    trimmed = query.strip() if query is not None else ""
    if not trimmed:
        return search_index.scoped_assets(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            directory_filter=directory_filter,
            control=control,
        )
    return search_index.search(
        trimmed,
        source_id=source_id,
        directory=directory,
        directory_prefix=directory_prefix,
        directory_filter=directory_filter,
        domain_vocabulary=domain_vocabulary,
        mode=mode,
        limit=len(search_index.assets),
        offset=0,
        control=control,
    ).assets


def _parse_directory_filter(
    raw_rules: list[str],
    config: AppConfig,
    search_index: SearchIndex,
) -> DirectoryFilter | None:
    if not raw_rules:
        return None
    source_ids = {source.id for source in config.sources}
    rules = []
    seen_nodes: set[tuple[str, str]] = set()
    whitelist_nodes: set[tuple[str, str]] = set()
    for raw_rule in raw_rules:
        try:
            payload = json.loads(raw_rule)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="filter_rule must be a JSON object",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="filter_rule must be a JSON object")
        source_id = payload.get("source_id")
        path = payload.get("path")
        state = payload.get("state")
        if not isinstance(source_id, str) or not source_id:
            raise HTTPException(
                status_code=400,
                detail="filter_rule.source_id must be a non-empty string",
            )
        if source_id not in source_ids:
            raise HTTPException(status_code=400, detail=f"unknown filter_rule source: {source_id}")
        if not isinstance(path, str):
            raise HTTPException(status_code=400, detail="filter_rule.path must be a string")
        if not _is_normalized_directory_path(path):
            raise HTTPException(
                status_code=400,
                detail="filter_rule.path must be a normalized source-relative directory path",
            )
        if state not in {"whitelist", "blacklist"}:
            raise HTTPException(
                status_code=400,
                detail="filter_rule.state must be whitelist or blacklist",
            )
        node = (source_id, path)
        if node in seen_nodes:
            raise HTTPException(status_code=400, detail="duplicate filter_rule node")
        seen_nodes.add(node)
        if node not in search_index.directory_counts:
            raise HTTPException(status_code=400, detail="filter_rule directory was not found")
        if state == "whitelist":
            for whitelist_source_id, whitelist_path in whitelist_nodes:
                if source_id == whitelist_source_id and _paths_share_subtree(
                    whitelist_path,
                    path,
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="whitelist filter_rules must not overlap in the same subtree",
                    )
            whitelist_nodes.add(node)
        rules.append(DirectoryFilterRule(source_id=source_id, path=path, state=state))
    return DirectoryFilter(tuple(rules))


def _paths_share_subtree(first: str, second: str) -> bool:
    return first == second or _is_path_ancestor(first, second) or _is_path_ancestor(second, first)


def _is_path_ancestor(ancestor: str, path: str) -> bool:
    return path != "" if ancestor == "" else path.startswith(f"{ancestor}/")


def _is_normalized_directory_path(path: str) -> bool:
    if path == "":
        return True
    if path.startswith("/") or path.endswith("/") or "\\" in path or "//" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _load_asset_or_404(db: sqlite3.Connection, config: AppConfig, asset_id: str):
    try:
        return get_asset(db, config, asset_id)
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail="asset not found") from exc
    except UnsafeAssetPathError as exc:
        raise HTTPException(status_code=500, detail="asset path is outside source root") from exc


def _directory_response(directory: DirectoryMatch) -> dict[str, object]:
    return {
        "source_id": directory.source_id,
        "path": directory.path,
        "name": directory.name,
        "depth": directory.depth,
        "asset_count": directory.asset_count,
        "matched_asset_count": directory.matched_asset_count,
        "quality": directory.quality,
        "quality_override": directory.quality_override,
    }


def _search_response(result: SearchResult) -> dict[str, object]:
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
        "score": result.score,
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
