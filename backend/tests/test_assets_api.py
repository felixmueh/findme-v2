from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, OutputProfile, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search_corpus import rebuild_search_corpus

from helpers import write_image, write_video


def test_asset_detail_includes_derived_actions_and_serves_file(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "Goblin Warrior.png"
    write_image(asset_path)
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
                    OutputProfile(id="foundry", label="FoundryVTT", prefix="library/Tokens"),
                ),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
        row = db.execute("SELECT asset_id, size_bytes, mtime_ns FROM assets").fetchone()
        asset_id = row["asset_id"]
        thumbnail_url = f"/api/assets/{asset_id}/thumb?v={row['size_bytes']}-{row['mtime_ns']}"
    rebuild_search_corpus(config, db_path)

    client = TestClient(create_app(config, db_path))
    detail = client.get(f"/api/assets/{asset_id}").json()

    assert detail["asset"]["relative_path"] == "Goblin Warrior.png"
    assert detail["asset"]["thumbnail_url"] == thumbnail_url
    assert detail["actions"] == {
        "open": {
            "id": "open",
            "label": "Open",
            "url": f"/api/assets/{asset_id}/file",
        },
        "copy": {
            "id": "copy",
            "label": "Copy",
            "url": f"/api/assets/{asset_id}/file",
            "enabled": True,
        },
        "copy_thumbnail": {
            "id": "copy_thumbnail",
            "label": "Copy Thumbnail",
            "url": thumbnail_url,
            "enabled": True,
        },
        "download": {
            "id": "download",
            "label": "Download",
            "url": f"/api/assets/{asset_id}/file?download=1",
        },
        "copy_path": {
            "id": "copy_path",
            "label": "Copy path",
            "value": str(asset_path.resolve()),
        },
        "copy_foundryvtt_path": {
            "id": "copy_foundryvtt_path",
            "label": "Copy FoundryVTT path",
            "value": "library/Tokens/Goblin Warrior.png",
        },
    }
    assert client.get(detail["actions"]["open"]["url"]).content == asset_path.read_bytes()
    thumbnail_response = client.get(detail["asset"]["thumbnail_url"])
    assert thumbnail_response.status_code == 200
    assert thumbnail_response.headers["content-type"] == "image/webp"
    assert (
        thumbnail_response.headers["cache-control"]
        == "public, max-age=31536000, immutable"
    )


def test_missing_asset_file_returns_404(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "Goblin.png"
    write_image(asset_path)
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
    rebuild_search_corpus(config, db_path)
    asset_path.unlink()

    client = TestClient(create_app(config, db_path))

    assert client.get(f"/api/assets/{asset_id}/file").status_code == 404


def test_asset_metadata_includes_image_size_and_video_duration(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "maps" / "Castle.png", size=(2048, 1536))
    write_video(source_root / "animations" / "Portal.mp4", size=(640, 360))
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".mp4"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
        image_id = db.execute(
            "SELECT asset_id FROM assets WHERE relative_path = 'maps/Castle.png'"
        ).fetchone()["asset_id"]
        video_id = db.execute(
            "SELECT asset_id FROM assets WHERE relative_path = 'animations/Portal.mp4'"
        ).fetchone()["asset_id"]
    rebuild_search_corpus(config, db_path)

    client = TestClient(create_app(config, db_path))

    image_asset = client.get(f"/api/assets/{image_id}").json()["asset"]
    assert image_asset["media_width"] == 2048
    assert image_asset["media_height"] == 1536
    assert image_asset["duration_seconds"] is None

    video_asset = client.get(f"/api/assets/{video_id}").json()["asset"]
    assert video_asset["media_width"] == 640
    assert video_asset["media_height"] == 360
    assert video_asset["duration_seconds"] >= 0.9

    search_asset = client.get("/api/search", params={"q": "portal"}).json()["cards"][0][
        "asset"
    ]
    assert search_asset["media_width"] == 640
    assert search_asset["media_height"] == 360
    assert search_asset["duration_seconds"] >= 0.9
