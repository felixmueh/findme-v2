from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, UnidentifiedImageError


THUMBNAIL_MAX_SIZE = (256, 256)
THUMBNAIL_QUALITY = 82
THUMBNAIL_WEBP_METHOD = 2
VIDEO_POSTER_TIMEOUT_SECONDS = 30.0


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailResult:
    path: Path
    width: int
    height: int
    generated: bool = False


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
            width, height = _save_thumbnail_image(image, thumb_path)
        return ThumbnailResult(thumb_path, width, height, generated=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailError(f"could not generate thumbnail for {source_path}: {exc}") from exc


def generate_video_poster(
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

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ThumbnailError("ffmpeg is required to generate video poster thumbnails")

    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path = _extract_first_video_frame(ffmpeg, source_path, thumb_path.parent)
    try:
        with Image.open(frame_path) as frame:
            width, height = _save_thumbnail_image(frame, thumb_path)
        return ThumbnailResult(thumb_path, width, height, generated=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailError(
            f"could not generate video poster thumbnail for {source_path}: {exc}"
        ) from exc
    finally:
        frame_path.unlink(missing_ok=True)


def _extract_first_video_frame(ffmpeg: str, source_path: Path, cache_dir: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix="poster-",
        dir=cache_dir,
        delete=False,
    ) as frame_file:
        frame_path = Path(frame_file.name)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-an",
        "-frames:v",
        "1",
        str(frame_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VIDEO_POSTER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        frame_path.unlink(missing_ok=True)
        raise ThumbnailError(
            f"could not extract first video frame for {source_path}: "
            f"ffmpeg timed out after {VIDEO_POSTER_TIMEOUT_SECONDS:.1f}s"
        ) from exc
    except OSError as exc:
        frame_path.unlink(missing_ok=True)
        raise ThumbnailError(
            f"could not extract first video frame for {source_path}: {exc}"
        ) from exc

    if completed.returncode != 0:
        frame_path.unlink(missing_ok=True)
        detail = completed.stderr.strip() or f"ffmpeg exited with {completed.returncode}"
        raise ThumbnailError(
            f"could not extract first video frame for {source_path}: {detail}"
        )
    if not frame_path.is_file() or frame_path.stat().st_size == 0:
        frame_path.unlink(missing_ok=True)
        raise ThumbnailError(
            f"could not extract first video frame for {source_path}: no frame was written"
        )
    return frame_path


def _save_thumbnail_image(image: Image.Image, thumb_path: Path) -> tuple[int, int]:
    image.thumbnail(THUMBNAIL_MAX_SIZE)
    thumbnail = image.convert("RGBA" if _has_alpha(image) else "RGB")
    thumbnail.save(
        thumb_path,
        "WEBP",
        quality=THUMBNAIL_QUALITY,
        method=THUMBNAIL_WEBP_METHOD,
    )
    return thumbnail.width, thumbnail.height


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
