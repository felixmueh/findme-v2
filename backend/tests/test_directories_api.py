from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources

from helpers import write_image


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

    client = TestClient(create_app(config, db_path))
    body = client.get("/api/directories", params={"source_id": "dnd"}).json()

    assert body["directories"] == [
        {"path": "", "name": "DND", "depth": 0, "asset_count": 3, "matched_asset_count": 3},
        {"path": "Maps", "name": "Maps", "depth": 1, "asset_count": 1, "matched_asset_count": 1},
        {"path": "Tokens", "name": "Tokens", "depth": 1, "asset_count": 2, "matched_asset_count": 2},
        {
            "path": "Tokens/Goblins",
            "name": "Goblins",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
        },
        {
            "path": "Tokens/Orcs",
            "name": "Orcs",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
        },
    ]


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

    client = TestClient(create_app(config, db_path))
    body = client.get("/api/directories", params={"source_id": "dnd", "q": "goblin"}).json()

    assert body["directories"] == [
        {"path": "", "name": "DND", "depth": 0, "asset_count": 3, "matched_asset_count": 2},
        {"path": "Maps", "name": "Maps", "depth": 1, "asset_count": 1, "matched_asset_count": 1},
        {"path": "Tokens", "name": "Tokens", "depth": 1, "asset_count": 2, "matched_asset_count": 1},
        {
            "path": "Tokens/Goblins",
            "name": "Goblins",
            "depth": 2,
            "asset_count": 1,
            "matched_asset_count": 1,
        },
    ]
