from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search_corpus import rebuild_search_corpus

from helpers import directory_query, write_image


def _stable_directory(directory: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in directory.items()
        if key
        not in {
            "included_asset_count",
            "id",
            "selection_state",
            "selection_effective",
            "profile_dirty",
            "has_descendant_whitelist",
            "has_descendant_blacklist",
        }
    }


def test_api_lists_indexed_directory_tree(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Tokens" / "Goblins" / "Goblin.png")
    write_image(source_root / "Tokens" / "Orcs" / "Orc.png")
    write_image(source_root / "Maps" / "Cave.png")
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
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))
    body = directory_query(client, {"source_id": "dnd"}).json()

    assert [_stable_directory(directory) for directory in body["directories"]] == [
        {
            "source_id": "dnd",
            "path": "",
            "name": "DND",
            "depth": 0,
            "asset_count": 3,
            "matched_asset_count": 3,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Maps",
            "name": "Maps",
            "depth": 1,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens",
            "name": "Tokens",
            "depth": 1,
            "asset_count": 2,
            "matched_asset_count": 2,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens/Goblins",
            "name": "Goblins",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens/Orcs",
            "name": "Orcs",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
    ]


def test_api_directory_rows_include_configured_file_browser_urls(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Paper" / "Plain.png")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="maps",
                display_name="Maps",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
                file_browser_url_prefix="https://filebrowser.example/files/DND/Maps",
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    directories = directory_query(client).json()["directories"]
    urls_by_path = {
        directory["path"]: directory["file_browser_url"] for directory in directories
    }

    assert urls_by_path == {
        "": "https://filebrowser.example/files/DND/Maps",
        "Paper": "https://filebrowser.example/files/DND/Maps/Paper",
    }


def test_api_lists_only_directories_with_search_matches(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Tokens" / "Goblins" / "Goblin.png")
    write_image(source_root / "Tokens" / "Orcs" / "Orc.png")
    write_image(source_root / "Maps" / "Goblin Cave.png")
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
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))
    body = directory_query(client, {"source_id": "dnd", "q": "goblin"}).json()

    assert [_stable_directory(directory) for directory in body["directories"]] == [
        {
            "source_id": "dnd",
            "path": "",
            "name": "DND",
            "depth": 0,
            "asset_count": 3,
            "matched_asset_count": 2,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Maps",
            "name": "Maps",
            "depth": 1,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens",
            "name": "Tokens",
            "depth": 1,
            "asset_count": 2,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens/Goblins",
            "name": "Goblins",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
    ]


def test_api_directory_rows_are_source_scoped_for_all_sources(tmp_path: Path) -> None:
    dnd_root = tmp_path / "dnd"
    maps_root = tmp_path / "maps"
    write_image(dnd_root / "Tokens" / "Goblin.png")
    write_image(maps_root / "Tokens" / "Cave.png")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=dnd_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="maps",
                display_name="Maps",
                root=maps_root,
                priority=5,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))
    body = directory_query(client).json()

    assert [_stable_directory(directory) for directory in body["directories"]] == [
        {
            "source_id": "dnd",
            "path": "",
            "name": "DND",
            "depth": 0,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "dnd",
            "path": "Tokens",
            "name": "Tokens",
            "depth": 1,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "maps",
            "path": "",
            "name": "Maps",
            "depth": 0,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
        {
            "source_id": "maps",
            "path": "Tokens",
            "name": "Tokens",
            "depth": 1,
            "asset_count": 1,
            "matched_asset_count": 1,
            "quality": 5,
            "quality_override": None,
        },
    ]
