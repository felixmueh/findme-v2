from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi import FastAPI, Query

from findme.config import AppConfig
from findme.db import connect, initialize_database


def create_app(config: AppConfig, db_path: Path | None = None) -> FastAPI:
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
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, list[dict[str, object]]]:
        with connect(database_path) as db:
            initialize_database(db)
            rows = _fetch_assets(db, source_id=source_id, directory=directory, limit=limit, offset=offset)
        return {"assets": [_asset_response(row) for row in rows]}

    return app


def _fetch_assets(
    db: sqlite3.Connection,
    *,
    source_id: str | None,
    directory: str | None,
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
          assets.media_type
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        WHERE {" AND ".join(where)}
        ORDER BY sources.priority DESC, assets.relative_path COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def _asset_response(row: sqlite3.Row) -> dict[str, object]:
    return {
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
