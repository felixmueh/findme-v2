from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
    SearchMode,
    SearchResult,
    count_search_assets,
    load_domain_vocabulary,
    search_assets,
)


def create_app(
    config: AppConfig, db_path: Path | None = None, static_dir: Path | None = None
) -> FastAPI:
    app = FastAPI(title="Findme V2")
    database_path = db_path or config.data_root / "findme.sqlite3"

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
            initialize_database(db)
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
        source_id: str | None = None, q: str | None = None
    ) -> dict[str, list[dict[str, object]]]:
        with connect(database_path) as db:
            initialize_database(db)
            directories = _fetch_directories(db, config, source_id=source_id, query=q)
        return {"directories": directories}

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1),
        mode: SearchMode = "combined",
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        domain_vocabulary = _load_domain_vocabulary(config)
        with connect(database_path) as db:
            initialize_database(db)
            results = search_assets(
                db,
                q,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                limit=limit,
                offset=offset,
            )
            total = count_search_assets(
                db,
                q,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
            )
        return {"assets": [_search_response(result) for result in results], "total": total}

    @app.get("/api/assets/{asset_id}")
    def asset_detail(asset_id: str) -> dict[str, object]:
        with connect(database_path) as db:
            initialize_database(db)
            asset = _load_asset_or_404(db, config, asset_id)
        return {"asset": asset_response(asset), "actions": asset_actions(config, asset)}

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str, download: bool = False) -> FileResponse:
        with connect(database_path) as db:
            initialize_database(db)
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
            initialize_database(db)
            asset = _load_asset_or_404(db, config, asset_id)
        if asset.thumbnail_path is None or not asset.thumbnail_path.is_file():
            raise HTTPException(status_code=404, detail="asset thumbnail is missing")
        return FileResponse(asset.thumbnail_path, media_type="image/webp")

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    return app


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


def _fetch_directories(
    db: sqlite3.Connection, config: AppConfig, *, source_id: str | None, query: str | None
) -> list[dict[str, object]]:
    source_lookup = {source.id: source.display_name for source in config.sources}
    total_counts = _directory_counts(db, source_id=source_id)
    match_counts = total_counts
    if query and query.strip():
        matched_assets = search_assets(db, query, source_id=source_id, limit=10_000)
        match_counts = _counts_from_asset_directories(
            [(asset.source_id, asset.directory) for asset in matched_assets]
        )
    directories = []
    for (source, path), total_count in total_counts.items():
        matched_count = match_counts.get((source, path), 0)
        if matched_count == 0:
            continue
        name = source_lookup.get(source, source) if path == "" else path.rsplit("/", 1)[-1]
        depth = 0 if path == "" else len(path.split("/"))
        directories.append(
            {
                "source_id": source,
                "path": path,
                "name": name,
                "depth": depth,
                "asset_count": total_count,
                "matched_asset_count": matched_count,
            }
        )
    return sorted(directories, key=lambda item: (item["path"].lower() != "", item["path"].lower()))


def _directory_counts(db: sqlite3.Connection, *, source_id: str | None) -> dict[tuple[str, str], int]:
    where = ["stale = 0"]
    params: list[object] = []
    if source_id:
        where.append("source_id = ?")
        params.append(source_id)

    rows = db.execute(
        f"""
        SELECT source_id, directory, COUNT(*) AS direct_count
        FROM assets
        WHERE {" AND ".join(where)}
        GROUP BY source_id, directory
        """,
        params,
    ).fetchall()
    return _counts_from_asset_directories(
        [(row["source_id"], row["directory"], row["direct_count"]) for row in rows]
    )


def _counts_from_asset_directories(
    directories: list[tuple[str, str] | tuple[str, str, int]],
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for item in directories:
        source = item[0]
        directory = item[1]
        direct_count = item[2] if len(item) == 3 else 1
        counts[(source, "")] = counts.get((source, ""), 0) + direct_count
        if not directory:
            continue
        parts = directory.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            counts[(source, path)] = counts.get((source, path), 0) + direct_count
    return counts


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
