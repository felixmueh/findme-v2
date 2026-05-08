from pathlib import Path

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import count_indexable_files, index_sources

from helpers import write_image


def test_index_sources_adds_media_files_and_ignores_legacy_findme_data(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / ".findme").mkdir()
    write_image(source_root / "Tokens" / "Goblin Warrior.PNG")
    (source_root / "Tokens" / "notes.txt").write_text("ignore", encoding="utf-8")
    (source_root / ".findme" / "old.png").write_bytes(b"ignore")

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
    db_path = tmp_path / "findme.sqlite3"

    with connect(db_path) as db:
        initialize_database(db)
        summary = index_sources(db, config)
        rows = db.execute(
            "SELECT source_id, relative_path, filename, stem, extension, stale FROM assets"
        ).fetchall()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert [tuple(row) for row in rows] == [
        ("dnd", "Tokens/Goblin Warrior.PNG", "Goblin Warrior.PNG", "Goblin Warrior", ".png", 0)
    ]


def test_count_indexable_files_matches_indexed_media_scope(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / ".findme").mkdir()
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Tokens" / "Dragon.jpg")
    write_image(source_root / ".findme" / "old.png")
    (source_root / "Tokens" / "notes.txt").write_text("ignore", encoding="utf-8")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".jpg"}),
            ),
        ),
    )

    assert count_indexable_files(config) == 2


def test_index_sources_marks_missing_assets_stale(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "dragon.webp"
    write_image(asset_path)
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".webp"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        asset_path.unlink()
        summary = index_sources(db, config)
        stale = db.execute("SELECT stale FROM assets WHERE relative_path = 'dragon.webp'").fetchone()[0]

    assert summary.marked_stale == 1
    assert stale == 1


def test_index_sources_skips_unchanged_thumbnail_generation(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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
        first = index_sources(db, config)
        second = index_sources(db, config)

    assert first.generated_thumbnails == 1
    assert second.generated_thumbnails == 0
