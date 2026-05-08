from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


THUMBNAIL_MAX_SIZE = (256, 256)
THUMBNAIL_QUALITY = 82


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailResult:
    path: Path
    width: int
    height: int


def thumbnail_cache_path(
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
) -> Path:
    del source_id, relative_path
    return data_root / "cache" / "thumbs" / asset_id[:2] / f"{asset_id}-{size_bytes}-{mtime_ns}.webp"


def generate_thumbnail(
    source_path: Path,
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
) -> ThumbnailResult:
    thumb_path = thumbnail_cache_path(
        data_root,
        asset_id=asset_id,
        source_id=source_id,
        relative_path=relative_path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
    )
    if thumb_path.is_file():
        with Image.open(thumb_path) as existing:
            return ThumbnailResult(thumb_path, existing.width, existing.height)

    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source_path) as image:
            image.thumbnail(THUMBNAIL_MAX_SIZE)
            thumbnail = image.convert("RGB")
            thumbnail.save(thumb_path, "WEBP", quality=THUMBNAIL_QUALITY, method=4)
            return ThumbnailResult(thumb_path, thumbnail.width, thumbnail.height)
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailError(f"could not generate thumbnail for {source_path}: {exc}") from exc
