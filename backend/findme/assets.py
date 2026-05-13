from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol

from findme.config import AppConfig, OutputProfile, SourceConfig


class AssetNotFoundError(LookupError):
    pass


class UnsafeAssetPathError(ValueError):
    pass


@dataclass(frozen=True)
class ThumbnailVariantRecord:
    kind: str
    max_edge: int
    width: int
    height: int
    cache_path: Path


class ThumbnailVariantLike(Protocol):
    kind: str
    max_edge: int
    width: int
    height: int


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
    thumbnail_variants: tuple[ThumbnailVariantRecord, ...]


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
          assets.media_type
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        WHERE assets.asset_id = ?
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
        thumbnail_variants=_thumbnail_variants_for_asset(db, row["asset_id"]),
    )
    _validate_asset_path(config, asset)
    return asset


def _thumbnail_variants_for_asset(
    db: sqlite3.Connection,
    asset_id: str,
) -> tuple[ThumbnailVariantRecord, ...]:
    rows = db.execute(
        """
        SELECT kind, max_edge, width, height, cache_path
        FROM asset_thumbnail_variants
        WHERE asset_id = ?
        ORDER BY max_edge
        """,
        (asset_id,),
    ).fetchall()
    return tuple(
        ThumbnailVariantRecord(
            kind=row["kind"],
            max_edge=row["max_edge"],
            width=row["width"],
            height=row["height"],
            cache_path=Path(row["cache_path"]),
        )
        for row in rows
    )


def asset_actions(config: AppConfig, asset: AssetRecord) -> dict[str, object]:
    source = _source_for(config, asset.source_id)
    file_url = f"/api/assets/{asset.asset_id}/file"
    baseline_variant = asset.thumbnail_variants[0] if asset.thumbnail_variants else None
    thumb_url = None
    if baseline_variant is not None:
        thumb_url = thumbnail_variant_response(
            asset.asset_id,
            baseline_variant,
            size_bytes=asset.size_bytes,
            mtime_ns=asset.mtime_ns,
        )["url"]
    return {
        "open": {"id": "open", "label": "Open", "url": file_url},
        "copy": {
            "id": "copy",
            "label": "Copy",
            "url": file_url,
            "enabled": asset.media_type == "image",
        },
        "copy_thumbnail": {
            "id": "copy_thumbnail",
            "label": "Copy Thumbnail",
            "url": thumb_url,
            "enabled": thumb_url is not None,
        },
        "download": {"id": "download", "label": "Download", "url": f"{file_url}?download=1"},
        "copy_path": {
            "id": "copy_path",
            "label": "Copy path",
            "value": str(asset.absolute_path),
        },
        "copy_foundryvtt_path": {
            "id": "copy_foundryvtt_path",
            "label": "Copy FoundryVTT path",
            "value": _foundryvtt_path(source, asset),
        },
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
    if asset.thumbnail_variants:
        baseline_variant = asset.thumbnail_variants[0]
        baseline_response = thumbnail_variant_response(
            asset.asset_id,
            baseline_variant,
            size_bytes=asset.size_bytes,
            mtime_ns=asset.mtime_ns,
        )
        response["thumbnail_url"] = baseline_response["url"]
        response["thumbnail_width"] = baseline_variant.width
        response["thumbnail_height"] = baseline_variant.height
        response["thumbnail_variants"] = [
            thumbnail_variant_response(
                asset.asset_id,
                variant,
                size_bytes=asset.size_bytes,
                mtime_ns=asset.mtime_ns,
            )
            for variant in asset.thumbnail_variants
        ]
    return response


def thumbnail_url(
    asset_id: str,
    *,
    size_bytes: int,
    mtime_ns: int,
    max_edge: int | None = None,
) -> str:
    version = f"v={size_bytes}-{mtime_ns}"
    if max_edge is None:
        return f"/api/assets/{asset_id}/thumb?{version}"
    return f"/api/assets/{asset_id}/thumb?max_edge={max_edge}&{version}"


def thumbnail_variant_response(
    asset_id: str,
    variant: ThumbnailVariantLike,
    *,
    size_bytes: int,
    mtime_ns: int,
) -> dict[str, object]:
    return {
        "kind": variant.kind,
        "max_edge": variant.max_edge,
        "width": variant.width,
        "height": variant.height,
        "url": thumbnail_url(
            asset_id,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            max_edge=None if variant.max_edge == 256 else variant.max_edge,
        ),
    }


def _foundryvtt_path(source: SourceConfig, asset: AssetRecord) -> str:
    profile = _primary_output_profile(source)
    if profile is None:
        return asset.relative_path
    return _join_url_path(profile.prefix, asset.relative_path)


def _primary_output_profile(source: SourceConfig) -> OutputProfile | None:
    if source.primary_output is not None:
        for profile in source.output_profiles:
            if profile.id == source.primary_output:
                return profile
    if source.output_profiles:
        return source.output_profiles[0]
    return None


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
