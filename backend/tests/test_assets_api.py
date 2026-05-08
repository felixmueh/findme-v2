from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, OutputProfile, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources


def test_asset_detail_includes_derived_actions_and_serves_file(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "Goblin Warrior.png").write_bytes(b"png-bytes")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png"}),
                primary_output="foundry",
                output_profiles=(
                    OutputProfile(id="foundry", label="Foundry", prefix="assets/dnd"),
                ),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
        asset_id = db.execute("SELECT asset_id FROM assets").fetchone()["asset_id"]

    client = TestClient(create_app(config, db_path))
    detail = client.get(f"/api/assets/{asset_id}").json()

    assert detail["asset"]["relative_path"] == "Goblin Warrior.png"
    assert detail["actions"]["primary"]["value"] == "assets/dnd/Goblin Warrior.png"
    assert detail["actions"]["profiles"] == [
        {
            "id": "foundry",
            "label": "Foundry",
            "value": "assets/dnd/Goblin Warrior.png",
        }
    ]
    assert detail["actions"]["download_url"] == f"/api/assets/{asset_id}/file?download=1"
    assert client.get(detail["actions"]["open_url"]).content == b"png-bytes"


def test_missing_asset_file_returns_404(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "Goblin.png"
    asset_path.write_bytes(b"png")
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
        asset_id = db.execute("SELECT asset_id FROM assets").fetchone()["asset_id"]
    asset_path.unlink()

    client = TestClient(create_app(config, db_path))

    assert client.get(f"/api/assets/{asset_id}/file").status_code == 404
