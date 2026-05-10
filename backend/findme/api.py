from __future__ import annotations

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
    SearchCorpusError,
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
    card_resolver = ResultCardResolver.from_assets(
        [_search_response(result) for result in search_index.scoped_assets()],
        card_metadata_by_asset_id=snapshot.card_metadata_by_asset_id(),
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
            matched_assets = _matched_assets(
                search_index,
                q,
                source_id=source_id,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
            result_assets = _filter_results(
                matched_assets,
                directory=directory,
                directory_prefix=directory_prefix,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        cards, total = card_resolver.page_for(
            [_search_response(result) for result in result_assets],
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
            matched_assets = _matched_assets(
                search_index,
                q,
                source_id=source_id,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )
            result_assets = _filter_results(
                matched_assets,
                directory=directory,
                directory_prefix=directory_prefix,
            )
        except SearchSuperseded:
            return Response(status_code=204)
        cards, total = card_resolver.page_for(
            [_search_response(result) for result in result_assets],
            offset=offset,
            limit=limit,
        )
        directories = search_index.directories_for_matches(
            matched_assets,
            source_id=source_id,
        )
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
    domain_vocabulary: DomainVocabulary | None,
    mode: SearchMode,
    control: SearchControl | None,
) -> list[SearchResult]:
    trimmed = query.strip() if query is not None else ""
    if not trimmed:
        return search_index.scoped_assets(source_id=source_id, control=control)
    return search_index.search(
        trimmed,
        source_id=source_id,
        domain_vocabulary=domain_vocabulary,
        mode=mode,
        limit=len(search_index.assets),
        offset=0,
        control=control,
    ).assets


def _filter_results(
    results: list[SearchResult],
    *,
    directory: str | None,
    directory_prefix: str | None,
) -> list[SearchResult]:
    if directory is None and not directory_prefix:
        return results
    filtered = []
    for result in results:
        if directory is not None and result.directory != directory:
            continue
        if directory_prefix and not (
            result.directory == directory_prefix
            or result.directory.startswith(f"{directory_prefix}/")
        ):
            continue
        filtered.append(result)
    return filtered


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
