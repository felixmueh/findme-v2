from pathlib import Path
import os

import pytest

from PIL import Image

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.thumbnails import ThumbnailError, generate_video_poster, thumbnail_cache_path

from helpers import write_image, write_video


def test_thumbnail_cache_path_uses_data_root_and_fingerprint(tmp_path: Path) -> None:
    path = thumbnail_cache_path(
        tmp_path / "data",
        asset_id="abc123",
        source_id="dnd",
        relative_path="Tokens/Goblin.png",
        size_bytes=100,
        mtime_ns=200,
        max_edge=256,
    )

    assert path == tmp_path / "data" / "cache" / "thumbs" / "ab" / "abc123-100-200-256.webp"


def test_index_sources_generates_static_image_thumbnail(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Tokens" / "Goblin.png"
    write_image(image_path, size=(800, 400))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        row = db.execute(
            "SELECT thumbnail_path, thumbnail_width, thumbnail_height FROM assets"
        ).fetchone()

    assert row["thumbnail_path"] is not None
    assert Path(row["thumbnail_path"]).is_file()
    assert row["thumbnail_width"] == 256
    assert row["thumbnail_height"] == 128
    with Image.open(row["thumbnail_path"]) as thumb:
        assert thumb.size == (256, 128)


def test_index_sources_generates_static_image_thumbnail_mipmaps(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Maps" / "City.png"
    write_image(image_path, size=(1200, 600))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        asset = db.execute(
            """
            SELECT asset_id, thumbnail_path, thumbnail_width, thumbnail_height
            FROM assets
            WHERE relative_path = 'Maps/City.png'
            """
        ).fetchone()
        variants = db.execute(
            """
            SELECT kind, max_edge, width, height, cache_path
            FROM asset_thumbnail_variants
            WHERE asset_id = ?
            ORDER BY max_edge
            """,
            (asset["asset_id"],),
        ).fetchall()

    assert summary.generated_thumbnails == 1
    assert asset["thumbnail_width"] == 256
    assert asset["thumbnail_height"] == 128
    assert Path(asset["thumbnail_path"]).name.endswith("-256.webp")
    assert [(row["kind"], row["max_edge"], row["width"], row["height"]) for row in variants] == [
        ("thumbnail", 256, 256, 128),
        ("thumbnail", 512, 512, 256),
        ("thumbnail", 1024, 1024, 512),
    ]
    assert all(Path(row["cache_path"]).is_file() for row in variants)


def test_index_sources_generates_portrait_thumbnail_mipmaps_by_max_edge(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Portraits" / "Banner.png"
    write_image(image_path, size=(600, 1200))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        asset = db.execute(
            """
            SELECT asset_id, thumbnail_path, thumbnail_width, thumbnail_height
            FROM assets
            WHERE relative_path = 'Portraits/Banner.png'
            """
        ).fetchone()
        variants = db.execute(
            """
            SELECT kind, max_edge, width, height, cache_path
            FROM asset_thumbnail_variants
            WHERE asset_id = ?
            ORDER BY max_edge
            """,
            (asset["asset_id"],),
        ).fetchall()

    assert summary.generated_thumbnails == 1
    assert asset["thumbnail_width"] == 128
    assert asset["thumbnail_height"] == 256
    assert Path(asset["thumbnail_path"]).name.endswith("-256.webp")
    assert [(row["kind"], row["max_edge"], row["width"], row["height"]) for row in variants] == [
        ("thumbnail", 256, 128, 256),
        ("thumbnail", 512, 256, 512),
        ("thumbnail", 1024, 512, 1024),
    ]
    assert all(Path(row["cache_path"]).is_file() for row in variants)


def test_index_sources_generates_extreme_wide_thumbnail_by_max_edge(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Maps" / "Panorama.png"
    write_image(image_path, size=(2048, 256))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        variants = db.execute(
            """
            SELECT kind, max_edge, width, height, cache_path
            FROM asset_thumbnail_variants
            ORDER BY max_edge
            """
        ).fetchall()

    assert summary.generated_thumbnails == 1
    assert [(row["kind"], row["max_edge"], row["width"], row["height"]) for row in variants] == [
        ("thumbnail", 256, 256, 32),
        ("thumbnail", 512, 512, 64),
        ("thumbnail", 1024, 1024, 128),
    ]
    assert all(Path(row["cache_path"]).is_file() for row in variants)


def test_index_sources_skips_thumbnail_mipmaps_that_would_upscale(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Tokens" / "Small.png"
    write_image(image_path, size=(400, 200))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
        variants = db.execute(
            """
            SELECT max_edge, width, height
            FROM asset_thumbnail_variants
            ORDER BY max_edge
            """
        ).fetchall()

    assert [(row["max_edge"], row["width"], row["height"]) for row in variants] == [
        (256, 256, 128),
    ]


def test_index_sources_preserves_thumbnail_alpha(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    image_path = source_root / "Tokens" / "Ghost.png"
    write_image(image_path, size=(32, 32), alpha=0)
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        thumbnail_path = db.execute("SELECT thumbnail_path FROM assets").fetchone()["thumbnail_path"]

    with Image.open(thumbnail_path) as thumb:
        assert thumb.mode == "RGBA"
        assert thumb.getpixel((0, 0))[3] == 0


def test_index_sources_generates_gif_thumbnail(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    gif_path = source_root / "Animations" / "Transparent.gif"
    write_image(gif_path, size=(320, 160), alpha=0)
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".gif"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        row = db.execute(
            """
            SELECT media_type, thumbnail_path, thumbnail_width, thumbnail_height
            FROM assets
            WHERE relative_path = 'Animations/Transparent.gif'
            """
        ).fetchone()

    assert summary.generated_thumbnails == 1
    assert row["media_type"] == "image"
    assert row["thumbnail_path"] is not None
    assert Path(row["thumbnail_path"]).is_file()
    assert row["thumbnail_width"] == 256
    assert row["thumbnail_height"] == 128
    with Image.open(row["thumbnail_path"]) as thumb:
        assert thumb.mode == "RGBA"
        assert thumb.size == (256, 128)


def test_index_sources_generates_video_poster_thumbnail(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    video_path = source_root / "Animations" / "Portal.mp4"
    write_video(video_path, size=(640, 360))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp4"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        row = db.execute(
            """
            SELECT media_type, thumbnail_path, thumbnail_width, thumbnail_height
            FROM assets
            WHERE relative_path = 'Animations/Portal.mp4'
            """
        ).fetchone()

    assert summary.generated_thumbnails == 1
    assert row["media_type"] == "video"
    assert row["thumbnail_path"] is not None
    assert Path(row["thumbnail_path"]).is_file()
    assert row["thumbnail_width"] == 256
    assert row["thumbnail_height"] == 144
    with Image.open(row["thumbnail_path"]) as thumb:
        assert thumb.size == (256, 144)
        red, green, blue = thumb.convert("RGB").getpixel((0, 0))
        assert red > 200
        assert green < 80
        assert blue < 80


def test_index_sources_generates_video_poster_mipmaps(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    video_path = source_root / "Animations" / "Portal.mp4"
    write_video(video_path, size=(1280, 720))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp4"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        asset = db.execute(
            "SELECT asset_id, thumbnail_width, thumbnail_height FROM assets"
        ).fetchone()
        variants = db.execute(
            """
            SELECT kind, max_edge, width, height, cache_path
            FROM asset_thumbnail_variants
            WHERE asset_id = ?
            ORDER BY max_edge
            """,
            (asset["asset_id"],),
        ).fetchall()

    assert summary.generated_thumbnails == 1
    assert asset["thumbnail_width"] == 256
    assert asset["thumbnail_height"] == 144
    assert [(row["kind"], row["max_edge"], row["width"], row["height"]) for row in variants] == [
        ("poster", 256, 256, 144),
        ("poster", 512, 512, 288),
        ("poster", 1024, 1024, 576),
    ]
    assert all(Path(row["cache_path"]).is_file() for row in variants)


def test_index_sources_generates_portrait_video_poster_mipmaps_by_max_edge(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    video_path = source_root / "Animations" / "Portal.mp4"
    write_video(video_path, size=(720, 1280))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp4"}),
            ),
        ),
        additional_thumbnail_mipmaps=(512, 1024),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, thumbnail_workers=1)
        asset = db.execute(
            "SELECT asset_id, thumbnail_width, thumbnail_height FROM assets"
        ).fetchone()
        variants = db.execute(
            """
            SELECT kind, max_edge, width, height, cache_path
            FROM asset_thumbnail_variants
            WHERE asset_id = ?
            ORDER BY max_edge
            """,
            (asset["asset_id"],),
        ).fetchall()

    assert summary.generated_thumbnails == 1
    assert asset["thumbnail_width"] == 144
    assert asset["thumbnail_height"] == 256
    assert [(row["kind"], row["max_edge"], row["width"], row["height"]) for row in variants] == [
        ("poster", 256, 144, 256),
        ("poster", 512, 288, 512),
        ("poster", 1024, 576, 1024),
    ]
    assert all(Path(row["cache_path"]).is_file() for row in variants)


def test_video_poster_generation_times_out_stuck_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ffmpeg = fake_bin / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_ffmpeg.chmod(0o755)
    fake_ffprobe = fake_bin / "ffprobe"
    fake_ffprobe.write_text(
        '#!/bin/sh\nprintf \'{"streams":[{"width":640,"height":360}]}\\n\'\n',
        encoding="utf-8",
    )
    fake_ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("findme.thumbnails.VIDEO_POSTER_TIMEOUT_SECONDS", 0.1)
    video_path = tmp_path / "slow.webm"
    video_path.write_bytes(b"not important")

    with pytest.raises(ThumbnailError, match="timed out"):
        generate_video_poster(
            video_path,
            tmp_path / "data",
            asset_id="slow-video",
            source_id="dnd",
            relative_path="slow.webm",
            size_bytes=video_path.stat().st_size,
            mtime_ns=video_path.stat().st_mtime_ns,
        )

    assert not list((tmp_path / "data" / "cache" / "thumbs").rglob("poster-*.png"))
