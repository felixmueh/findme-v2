from pathlib import Path

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search import search_assets

from helpers import write_image


def test_search_assets_matches_filename_and_path_tokens(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens" / "Goblins").mkdir(parents=True)
    (source_root / "Maps").mkdir()
    write_image(source_root / "Tokens" / "Goblins" / "Goblin Warrior.png")
    write_image(source_root / "Maps" / "Goblin Cave.jpg")
    write_image(source_root / "Tokens" / "Dragon.webp")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "goblin warrior")

    assert [result.relative_path for result in results] == [
        "Tokens/Goblins/Goblin Warrior.png",
        "Maps/Goblin Cave.jpg",
    ]
    assert results[0].score > results[1].score


def test_search_assets_applies_source_and_directory_filters(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / "Maps").mkdir()
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Maps" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "goblin", source_id="dnd", directory="Tokens")

    assert [result.relative_path for result in results] == ["Tokens/Goblin.png"]


def test_search_assets_hides_stale_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    stale_path = source_root / "Goblin.png"
    write_image(stale_path)
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        stale_path.unlink()
        index_sources(db, config)
        results = search_assets(db, "goblin")

    assert results == []


def _config(tmp_path: Path, source_root: Path) -> AppConfig:
    return AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".jpg", ".webp"}),
            ),
        ),
    )
