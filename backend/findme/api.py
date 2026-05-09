from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from findme.assets import (
    AssetNotFoundError,
    UnsafeAssetPathError,
    asset_actions,
    asset_response,
    get_asset,
)
from findme.config import AppConfig
from findme.db import connect, initialize_database
from findme.result_cards import ResultCardResolver
from findme.search import (
    DirectoryMatch,
    SearchIndex,
    SearchMode,
    SearchResult,
    SearchControl,
    SearchSuperseded,
    load_domain_vocabulary,
)


def create_app(
    config: AppConfig, db_path: Path | None = None, static_dir: Path | None = None
) -> FastAPI:
    database_path = db_path or config.data_root / "findme.sqlite3"
    with connect(database_path) as db:
        initialize_database(db)
        search_index = SearchIndex.load(db)
    card_resolver = ResultCardResolver.from_assets(
        [_search_response(result) for result in search_index.scoped_assets()]
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
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        scoped_assets = search_index.scoped_assets(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
        )
        cards, total = card_resolver.page_for(
            [_search_response(result) for result in scoped_assets],
            offset=offset,
            limit=limit,
        )
        return JSONResponse({"cards": cards, "total": total})

    @app.get("/api/directories")
    def list_directories(
        source_id: str | None = None,
        q: str | None = None,
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            directories = search_index.directory_matches(
                q,
                source_id=source_id,
                domain_vocabulary=domain_vocabulary,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        return {"directories": [_directory_response(directory) for directory in directories]}

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1),
        mode: SearchMode = "combined",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search_session: str | None = None,
        search_revision: int | None = None,
    ) -> object:
        control = search_revisions.control(search_session, search_revision)
        if control and control.is_cancelled():
            return Response(status_code=204)
        domain_vocabulary = _load_domain_vocabulary(config)
        try:
            page = search_index.search(
                q,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                limit=len(search_index.assets),
                offset=0,
                control=control,
            )
            scoped_assets = search_index.scoped_assets(
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        cards, total = card_resolver.page_for(
            [_search_response(result) for result in page.assets],
            offset=offset,
            limit=limit,
        )
        return JSONResponse({"cards": cards, "total": total})

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
        return FileResponse(asset.thumbnail_path, media_type="image/webp")

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
        response["thumbnail_url"] = f"/api/assets/{result.asset_id}/thumb"
        response["thumbnail_width"] = result.thumbnail_width
        response["thumbnail_height"] = result.thumbnail_height
    return response
