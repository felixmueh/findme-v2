from pathlib import Path

from PIL import Image

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.thumbnails import thumbnail_cache_path

from helpers import write_image


def test_thumbnail_cache_path_uses_data_root_and_fingerprint(tmp_path: Path) -> None:
    path = thumbnail_cache_path(
        tmp_path / "data",
        asset_id="abc123",
        source_id="dnd",
        relative_path="Tokens/Goblin.png",
        size_bytes=100,
        mtime_ns=200,
    )

    assert path == tmp_path / "data" / "cache" / "thumbs" / "ab" / "abc123-100-200.webp"


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
