from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources


def test_api_lists_sources_and_current_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "goblin.png").write_bytes(b"png")
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

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/sources").json()["sources"] == [
        {
            "id": "dnd",
            "display_name": "DND",
            "priority": 10,
            "root": str(source_root),
        }
    ]
    assets = client.get("/api/assets").json()["assets"]
    assert len(assets) == 1
    assert assets[0]["source_id"] == "dnd"
    assert assets[0]["relative_path"] == "goblin.png"
    assert assets[0]["media_type"] == "image"
