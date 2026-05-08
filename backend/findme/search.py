from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3


@dataclass(frozen=True)
class SearchResult:
    asset_id: str
    source_id: str
    source_display_name: str
    relative_path: str
    filename: str
    stem: str
    extension: str
    directory: str
    size_bytes: int
    media_type: str
    thumbnail_path: str | None
    thumbnail_width: int | None
    thumbnail_height: int | None
    score: float


def search_assets(
    db: sqlite3.Connection,
    query: str,
    *,
    source_id: str | None = None,
    directory: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SearchResult]:
    match_query = _match_query(query)
    if not match_query:
        return []

    where = ["assets.stale = 0", "asset_search MATCH ?"]
    params: list[object] = [match_query]
    if source_id:
        where.append("assets.source_id = ?")
        params.append(source_id)
    if directory is not None:
        where.append("assets.directory = ?")
        params.append(directory)

    params.extend([limit, offset])
    rows = db.execute(
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
          assets.thumbnail_height,
          bm25(asset_search, 6.0, 5.0, 2.0, 1.0) * -1.0
            + sources.priority * 0.0001 AS score
        FROM asset_search
        JOIN assets ON assets.asset_id = asset_search.asset_id
        JOIN sources ON sources.id = assets.source_id
        WHERE {" AND ".join(where)}
        ORDER BY score DESC, sources.priority DESC, assets.relative_path COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_row_to_result(row) for row in rows]


def _match_query(query: str) -> str:
    tokens = re.findall(r"[\w-]+", query.lower())
    return " OR ".join(f"{token}*" for token in tokens)


def _row_to_result(row: sqlite3.Row) -> SearchResult:
    return SearchResult(
        asset_id=row["asset_id"],
        source_id=row["source_id"],
        source_display_name=row["source_display_name"],
        relative_path=row["relative_path"],
        filename=row["filename"],
        stem=row["stem"],
        extension=row["extension"],
        directory=row["directory"],
        size_bytes=row["size_bytes"],
        media_type=row["media_type"],
        thumbnail_path=row["thumbnail_path"],
        thumbnail_width=row["thumbnail_width"],
        thumbnail_height=row["thumbnail_height"],
        score=row["score"],
    )
