from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

from PIL import Image, UnidentifiedImageError


THUMBNAIL_MAX_SIZE = (256, 256)
BASE_THUMBNAIL_SIZE = 256
THUMBNAIL_QUALITY = 82
THUMBNAIL_WEBP_METHOD = 2
VIDEO_POSTER_TIMEOUT_SECONDS = 30.0


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailVariant:
    path: Path
    width: int
    height: int
    max_edge: int
    kind: str
    generated: bool = False


@dataclass(frozen=True)
class ThumbnailResult:
    path: Path
    width: int
    height: int
    generated: bool = False
    variants: tuple[ThumbnailVariant, ...] = ()


@dataclass(frozen=True)
class MediaMetadata:
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


def thumbnail_cache_path(
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
    max_edge: int = BASE_THUMBNAIL_SIZE,
) -> Path:
    del source_id, relative_path
    return (
        data_root
        / "cache"
        / "thumbs"
        / asset_id[:2]
        / f"{asset_id}-{size_bytes}-{mtime_ns}-{max_edge}.webp"
    )


def generate_thumbnail(
    source_path: Path,
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
    additional_max_edges: tuple[int, ...] = (),
) -> ThumbnailResult:
    try:
        with Image.open(source_path) as image:
            variants = _save_thumbnail_variants(
                image,
                data_root,
                asset_id=asset_id,
                source_id=source_id,
                relative_path=relative_path,
                size_bytes=size_bytes,
                mtime_ns=mtime_ns,
                additional_max_edges=additional_max_edges,
                kind="thumbnail",
            )
        baseline = _baseline_variant(variants)
        return ThumbnailResult(
            baseline.path,
            baseline.width,
            baseline.height,
            generated=any(variant.generated for variant in variants),
            variants=variants,
        )
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailError(f"could not generate thumbnail for {source_path}: {exc}") from exc


def read_media_metadata(source_path: Path, media_type: str) -> MediaMetadata | None:
    if media_type == "image":
        try:
            with Image.open(source_path) as image:
                return MediaMetadata(width=image.width, height=image.height)
        except (OSError, UnidentifiedImageError) as exc:
            raise ThumbnailError(f"could not read image metadata for {source_path}: {exc}") from exc
    if media_type == "video":
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise ThumbnailError("ffprobe is required to read video metadata")
        width, height, duration_seconds = _video_metadata(ffprobe, source_path)
        return MediaMetadata(
            width=width,
            height=height,
            duration_seconds=duration_seconds,
        )
    if media_type == "audio":
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise ThumbnailError("ffprobe is required to read audio metadata")
        duration_seconds = _audio_duration(ffprobe, source_path)
        return MediaMetadata(duration_seconds=duration_seconds)
    return None


def generate_video_poster(
    source_path: Path,
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
    additional_max_edges: tuple[int, ...] = (),
) -> ThumbnailResult:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ThumbnailError("ffmpeg is required to generate video poster thumbnails")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ThumbnailError("ffprobe is required to generate video poster thumbnails")

    width, height, _duration_seconds = _video_metadata(ffprobe, source_path)
    sizes = _eligible_thumbnail_sizes(width, height, additional_max_edges)
    paths = {
        size: thumbnail_cache_path(
            data_root,
            asset_id=asset_id,
            source_id=source_id,
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            max_edge=size,
        )
        for size in sizes
    }
    if all(path.is_file() for path in paths.values()):
        variants = tuple(
            _read_existing_variant(paths[size], size, "poster")
            for size in sorted(sizes)
        )
        baseline = _baseline_variant(variants)
        return ThumbnailResult(
            baseline.path,
            baseline.width,
            baseline.height,
            generated=False,
            variants=variants,
        )

    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    try:
        variants = _extract_video_poster_variants(ffmpeg, source_path, paths)
        baseline = _baseline_variant(variants)
        return ThumbnailResult(
            baseline.path,
            baseline.width,
            baseline.height,
            generated=True,
            variants=variants,
        )
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailError(
            f"could not generate video poster thumbnail for {source_path}: {exc}"
        ) from exc


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


def _video_metadata(ffprobe: str, source_path: Path) -> tuple[int, int, float | None]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VIDEO_POSTER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailError(
            f"could not read video dimensions for {source_path}: "
            f"ffprobe timed out after {VIDEO_POSTER_TIMEOUT_SECONDS:.1f}s"
        ) from exc
    except OSError as exc:
        raise ThumbnailError(f"could not read video dimensions for {source_path}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"ffprobe exited with {completed.returncode}"
        raise ThumbnailError(f"could not read video dimensions for {source_path}: {detail}")
    try:
        data = json.loads(completed.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration_seconds = _positive_float_or_none(
            stream.get("duration")
            or data.get("format", {}).get("duration")
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ThumbnailError(
            f"could not read video dimensions for {source_path}: invalid ffprobe output"
        ) from exc
    if width < 1 or height < 1:
        raise ThumbnailError(f"could not read video dimensions for {source_path}: invalid size")
    return width, height, duration_seconds


def _positive_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _audio_duration(ffprobe: str, source_path: Path) -> float | None:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VIDEO_POSTER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailError(
            f"could not read audio duration for {source_path}: "
            f"ffprobe timed out after {VIDEO_POSTER_TIMEOUT_SECONDS:.1f}s"
        ) from exc
    except OSError as exc:
        raise ThumbnailError(f"could not read audio duration for {source_path}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"ffprobe exited with {completed.returncode}"
        raise ThumbnailError(f"could not read audio duration for {source_path}: {detail}")
    try:
        data = json.loads(completed.stdout)
        return _positive_float_or_none(data.get("format", {}).get("duration"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ThumbnailError(
            f"could not read audio duration for {source_path}: invalid ffprobe output"
        ) from exc


def _extract_video_poster_variants(
    ffmpeg: str,
    source_path: Path,
    paths: dict[int, Path],
) -> tuple[ThumbnailVariant, ...]:
    sizes = tuple(sorted(paths, reverse=True))
    split_labels = "".join(f"[v{index}]" for index in range(len(sizes)))
    filters = [f"[0:v]split={len(sizes)}{split_labels}"]
    for index, size in enumerate(sizes):
        filters.append(
            f"[v{index}]scale=w='min({size},iw)':"
            f"h='min({size},ih)':force_original_aspect_ratio=decrease[o{index}]"
        )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-an",
        "-sn",
        "-dn",
        "-filter_complex",
        ";".join(filters),
    ]
    for index, size in enumerate(sizes):
        command.extend(
            [
                "-map",
                f"[o{index}]",
                "-frames:v",
                "1",
                "-c:v",
                "libwebp",
                "-quality",
                str(THUMBNAIL_QUALITY),
                "-compression_level",
                str(THUMBNAIL_WEBP_METHOD),
                str(paths[size]),
            ]
        )
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
        for path in paths.values():
            path.unlink(missing_ok=True)
        raise ThumbnailError(
            f"could not extract video poster thumbnails for {source_path}: "
            f"ffmpeg timed out after {VIDEO_POSTER_TIMEOUT_SECONDS:.1f}s"
        ) from exc
    except OSError as exc:
        for path in paths.values():
            path.unlink(missing_ok=True)
        raise ThumbnailError(f"could not extract video poster thumbnails for {source_path}: {exc}") from exc

    if completed.returncode != 0:
        for path in paths.values():
            path.unlink(missing_ok=True)
        detail = completed.stderr.strip() or f"ffmpeg exited with {completed.returncode}"
        raise ThumbnailError(f"could not extract video poster thumbnails for {source_path}: {detail}")

    variants: list[ThumbnailVariant] = []
    for size in sorted(paths):
        path = paths[size]
        if not path.is_file() or path.stat().st_size == 0:
            for cleanup_path in paths.values():
                cleanup_path.unlink(missing_ok=True)
            raise ThumbnailError(
                f"could not extract video poster thumbnails for {source_path}: "
                f"no frame was written for {size}"
            )
        variants.append(_read_existing_variant(path, size, "poster"))
    return tuple(
        ThumbnailVariant(
            path=variant.path,
            width=variant.width,
            height=variant.height,
            max_edge=variant.max_edge,
            kind=variant.kind,
            generated=True,
        )
        for variant in variants
    )


def _save_thumbnail_image(image: Image.Image, thumb_path: Path) -> tuple[int, int]:
    image.thumbnail(THUMBNAIL_MAX_SIZE, resample=Image.Resampling.BICUBIC)
    thumbnail = image.convert("RGBA" if _has_alpha(image) else "RGB")
    thumbnail.save(
        thumb_path,
        "WEBP",
        quality=THUMBNAIL_QUALITY,
        method=THUMBNAIL_WEBP_METHOD,
    )
    return thumbnail.width, thumbnail.height


def _save_thumbnail_variants(
    image: Image.Image,
    data_root: Path,
    *,
    asset_id: str,
    source_id: str,
    relative_path: str,
    size_bytes: int,
    mtime_ns: int,
    additional_max_edges: tuple[int, ...],
    kind: str,
) -> tuple[ThumbnailVariant, ...]:
    sizes = _eligible_thumbnail_sizes(image.width, image.height, additional_max_edges)
    paths = {
        size: thumbnail_cache_path(
            data_root,
            asset_id=asset_id,
            source_id=source_id,
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            max_edge=size,
        )
        for size in sizes
    }
    if all(path.is_file() for path in paths.values()):
        return tuple(
            _read_existing_variant(paths[size], size, kind)
            for size in sorted(sizes)
        )

    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    generated: dict[int, ThumbnailVariant] = {}
    largest = max(sizes)
    working = image.copy()
    working.thumbnail((largest, largest), resample=Image.Resampling.BICUBIC)
    current = working
    for size in sorted(sizes, reverse=True):
        if max(current.width, current.height) > size:
            current = current.copy()
            current.thumbnail((size, size), resample=Image.Resampling.BICUBIC)
        path = paths[size]
        thumbnail = current.convert("RGBA" if _has_alpha(current) else "RGB")
        thumbnail.save(
            path,
            "WEBP",
            quality=THUMBNAIL_QUALITY,
            method=THUMBNAIL_WEBP_METHOD,
        )
        generated[size] = ThumbnailVariant(
            path=path,
            width=thumbnail.width,
            height=thumbnail.height,
            max_edge=size,
            kind=kind,
            generated=True,
        )
    return tuple(generated[size] for size in sorted(generated))


def _eligible_thumbnail_sizes(
    width: int,
    height: int,
    additional_max_edges: tuple[int, ...],
) -> tuple[int, ...]:
    original_max_edge = max(width, height)
    sizes = {BASE_THUMBNAIL_SIZE}
    sizes.update(size for size in additional_max_edges if original_max_edge > size)
    return tuple(sorted(sizes))


def _read_existing_variant(path: Path, max_edge: int, kind: str) -> ThumbnailVariant:
    with Image.open(path) as existing:
        return ThumbnailVariant(
            path=path,
            width=existing.width,
            height=existing.height,
            max_edge=max_edge,
            kind=kind,
            generated=False,
        )


def _baseline_variant(variants: tuple[ThumbnailVariant, ...]) -> ThumbnailVariant:
    for variant in variants:
        if variant.max_edge == BASE_THUMBNAIL_SIZE:
            return variant
    raise ThumbnailError("thumbnail generation did not produce baseline 256 variant")


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
