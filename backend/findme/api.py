from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
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
        with connect(database_path) as db:
            rows = _fetch_assets(
                db,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                limit=limit,
                offset=offset,
            )
            total = _count_assets(
                db,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
            )
        return {"assets": [_asset_response(row) for row in rows], "total": total}

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
                limit=limit,
                offset=offset,
                control=control,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        return {
            "assets": [_search_response(result) for result in page.assets],
            "total": page.total,
        }

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


def _fetch_assets(
    db: sqlite3.Connection,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    where = ["assets.stale = 0"]
    params: list[object] = []
    if source_id:
        where.append("assets.source_id = ?")
        params.append(source_id)
    if directory is not None:
        where.append("assets.directory = ?")
        params.append(directory)
    if directory_prefix:
        where.append("(assets.directory = ? OR assets.directory LIKE ?)")
        params.extend([directory_prefix, f"{directory_prefix}/%"])

    params.extend([limit, offset])
    return db.execute(
        f"""
        SELECT
          assets.asset_id,
          assets.source_id,
          sources.display_name AS source_display_name,
          assets.relative_path,
          assets.filename,
          assets.stem,
          assets.extension,
          assets.directory,
          assets.size_bytes,
          assets.media_type,
          assets.thumbnail_path,
          assets.thumbnail_width,
          assets.thumbnail_height
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        WHERE {" AND ".join(where)}
        ORDER BY sources.priority DESC, assets.relative_path COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def _count_assets(
    db: sqlite3.Connection,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
) -> int:
    where = ["assets.stale = 0"]
    params: list[object] = []
    if source_id:
        where.append("assets.source_id = ?")
        params.append(source_id)
    if directory is not None:
        where.append("assets.directory = ?")
        params.append(directory)
    if directory_prefix:
        where.append("(assets.directory = ? OR assets.directory LIKE ?)")
        params.extend([directory_prefix, f"{directory_prefix}/%"])

    row = db.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM assets
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()
    return int(row["total"])


def _asset_response(row: sqlite3.Row) -> dict[str, object]:
    response = {
        "asset_id": row["asset_id"],
        "source_id": row["source_id"],
        "source_display_name": row["source_display_name"],
        "relative_path": row["relative_path"],
        "filename": row["filename"],
        "stem": row["stem"],
        "extension": row["extension"],
        "directory": row["directory"],
        "size_bytes": row["size_bytes"],
        "media_type": row["media_type"],
    }
    if row["thumbnail_path"] is not None:
        response["thumbnail_url"] = f"/api/assets/{row['asset_id']}/thumb"
        response["thumbnail_width"] = row["thumbnail_width"]
        response["thumbnail_height"] = row["thumbnail_height"]
    return response


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
