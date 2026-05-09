from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from findme.config import AppConfig, SourceConfig


class AssetNotFoundError(LookupError):
    pass


class UnsafeAssetPathError(ValueError):
    pass


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    source_id: str
    source_display_name: str
    relative_path: str
    absolute_path: Path
    filename: str
    stem: str
    extension: str
    directory: str
    size_bytes: int
    mtime_ns: int
    media_type: str
    thumbnail_path: Path | None
    thumbnail_width: int | None
    thumbnail_height: int | None


def get_asset(db: sqlite3.Connection, config: AppConfig, asset_id: str) -> AssetRecord:
    row = db.execute(
        """
        SELECT
          assets.asset_id,
          assets.source_id,
          sources.display_name AS source_display_name,
          assets.relative_path,
          assets.absolute_path,
          assets.filename,
          assets.stem,
          assets.extension,
          assets.directory,
          assets.size_bytes,
          assets.mtime_ns,
          assets.media_type,
          assets.thumbnail_path,
          assets.thumbnail_width,
          assets.thumbnail_height
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        WHERE assets.asset_id = ? AND assets.stale = 0
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise AssetNotFoundError(asset_id)

    asset = AssetRecord(
        asset_id=row["asset_id"],
        source_id=row["source_id"],
        source_display_name=row["source_display_name"],
        relative_path=row["relative_path"],
        absolute_path=Path(row["absolute_path"]),
        filename=row["filename"],
        stem=row["stem"],
        extension=row["extension"],
        directory=row["directory"],
        size_bytes=row["size_bytes"],
        mtime_ns=row["mtime_ns"],
        media_type=row["media_type"],
        thumbnail_path=Path(row["thumbnail_path"]) if row["thumbnail_path"] else None,
        thumbnail_width=row["thumbnail_width"],
        thumbnail_height=row["thumbnail_height"],
    )
    _validate_asset_path(config, asset)
    return asset


def asset_actions(config: AppConfig, asset: AssetRecord) -> dict[str, object]:
    source = _source_for(config, asset.source_id)
    profiles = [
        {
            "id": profile.id,
            "label": profile.label,
            "value": _join_url_path(profile.prefix, asset.relative_path),
        }
        for profile in source.output_profiles
    ]
    primary = _primary_action(source, asset, profiles)
    return {
        "primary": primary,
        "profiles": profiles,
        "source_relative": asset.relative_path,
        "absolute_path": str(asset.absolute_path),
        "open_url": f"/api/assets/{asset.asset_id}/file",
        "download_url": f"/api/assets/{asset.asset_id}/file?download=1",
    }


def asset_response(asset: AssetRecord) -> dict[str, object]:
    response = {
        "asset_id": asset.asset_id,
        "source_id": asset.source_id,
        "source_display_name": asset.source_display_name,
        "relative_path": asset.relative_path,
        "filename": asset.filename,
        "stem": asset.stem,
        "extension": asset.extension,
        "directory": asset.directory,
        "size_bytes": asset.size_bytes,
        "media_type": asset.media_type,
    }
    if asset.thumbnail_path is not None:
        response["thumbnail_url"] = thumbnail_url(
            asset.asset_id,
            size_bytes=asset.size_bytes,
            mtime_ns=asset.mtime_ns,
        )
        response["thumbnail_width"] = asset.thumbnail_width
        response["thumbnail_height"] = asset.thumbnail_height
    return response


def thumbnail_url(asset_id: str, *, size_bytes: int, mtime_ns: int) -> str:
    return f"/api/assets/{asset_id}/thumb?v={size_bytes}-{mtime_ns}"


def _primary_action(
    source: SourceConfig, asset: AssetRecord, profiles: list[dict[str, str]]
) -> dict[str, str]:
    if source.primary_output is not None:
        for profile in profiles:
            if profile["id"] == source.primary_output:
                return profile
    if profiles:
        return profiles[0]
    return {"id": "source_relative", "label": "Source Relative", "value": asset.relative_path}


def _source_for(config: AppConfig, source_id: str) -> SourceConfig:
    for source in config.sources:
        if source.id == source_id:
            return source
    raise AssetNotFoundError(f"unknown source for asset: {source_id}")


def _validate_asset_path(config: AppConfig, asset: AssetRecord) -> None:
    source = _source_for(config, asset.source_id)
    try:
        asset.absolute_path.resolve().relative_to(source.root.resolve())
    except ValueError as exc:
        raise UnsafeAssetPathError(str(asset.absolute_path)) from exc


def _join_url_path(prefix: str, relative_path: str) -> str:
    return f"{prefix.rstrip('/')}/{relative_path.lstrip('/')}"
