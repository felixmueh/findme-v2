from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, OutputProfile, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search_corpus import rebuild_search_corpus

from helpers import write_audio, write_image, write_video


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


def test_asset_tags_are_stored_in_metadata_database(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin Warrior.png")
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

    client = TestClient(create_app(config, db_path))
    response = client.put(
        f"/api/assets/{asset_id}/tags",
        json={"tags": ["NPC", "Goblin Warrior"]},
    )

    assert response.status_code == 200
    assert response.json()["tags"] == [
        {"slug": "goblin-warrior", "label": "Goblin Warrior"},
        {"slug": "npc", "label": "NPC"},
    ]
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["tags"] == [
        {"slug": "goblin-warrior", "label": "Goblin Warrior"},
        {"slug": "npc", "label": "NPC"},
    ]
    search_asset = client.get("/api/search", params={"q": "goblin"}).json()["cards"][0][
        "asset"
    ]
    assert search_asset["tags"] == [
        {"slug": "goblin-warrior", "label": "Goblin Warrior"},
        {"slug": "npc", "label": "NPC"},
    ]

    metadata_db = config.data_root / "metadata.sqlite3"
    assert metadata_db.is_file()
    with connect(metadata_db) as metadata:
        tag_rows = metadata.execute(
            "SELECT slug, label FROM tags ORDER BY slug"
        ).fetchall()
        file_rows = metadata.execute(
            "SELECT file_id, hash_algorithm, length(content_hash) FROM metadata_files"
        ).fetchall()
    assert [tuple(row) for row in tag_rows] == [
        ("goblin-warrior", "Goblin Warrior"),
        ("npc", "NPC"),
    ]
    assert len(file_rows) == 1
    assert tuple(file_rows[0])[1:] == ("sha256", 64)

    with connect(db_path) as db:
        index_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "tags" not in index_tables


def test_api_lists_existing_tags_from_metadata_database(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin.png")
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
    client = TestClient(create_app(config, db_path))
    client.put(f"/api/assets/{asset_id}/tags", json={"tags": ["NPC", "Goblin"]})

    response = client.get("/api/tags")

    assert response.status_code == 200
    assert response.json() == {
        "tags": [
            {"slug": "goblin", "label": "Goblin"},
            {"slug": "npc", "label": "NPC"},
        ]
    }


def test_api_prunes_unused_tag_definitions(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin.png")
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
    client = TestClient(create_app(config, db_path))
    client.put(f"/api/assets/{asset_id}/tags", json={"tags": ["NPC", "Goblin"]})

    response = client.put(f"/api/assets/{asset_id}/tags", json={"tags": ["NPC"]})

    assert response.status_code == 200
    assert client.get("/api/tags").json() == {
        "tags": [
            {"slug": "npc", "label": "NPC"},
        ]
    }


def test_asset_tags_sort_number_tags_before_other_tags(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin.png")
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
    client = TestClient(create_app(config, db_path))

    response = client.put(
        f"/api/assets/{asset_id}/tags",
        json={"tags": ["zeta", "2", "alpha", "10"]},
    )

    assert response.status_code == 200
    assert response.json()["tags"] == [
        {"slug": "2", "label": "2"},
        {"slug": "10", "label": "10"},
        {"slug": "alpha", "label": "alpha"},
        {"slug": "zeta", "label": "zeta"},
    ]


def test_batch_adds_tags_to_selected_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Goblins" / "Goblin A.png")
    write_image(source_root / "Goblins" / "Goblin B.png")
    write_image(source_root / "Orcs" / "Orc.png")
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
        rows = db.execute(
            "SELECT asset_id, relative_path FROM assets ORDER BY relative_path"
        ).fetchall()
    rebuild_search_corpus(config, db_path)
    ids_by_path = {row["relative_path"]: row["asset_id"] for row in rows}
    client = TestClient(create_app(config, db_path))

    response = client.post(
        "/api/tags/batch",
        json={
            "asset_ids": [
                ids_by_path["Goblins/Goblin A.png"],
                ids_by_path["Goblins/Goblin B.png"],
            ],
            "add_tags": ["Encounter"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"matched": 2, "tagged": 2, "failed": []}
    assert client.get(f"/api/assets/{ids_by_path['Goblins/Goblin A.png']}").json()[
        "asset"
    ]["tags"] == [{"slug": "encounter", "label": "Encounter"}]
    assert client.get(f"/api/assets/{ids_by_path['Goblins/Goblin B.png']}").json()[
        "asset"
    ]["tags"] == [{"slug": "encounter", "label": "Encounter"}]
    assert client.get(f"/api/assets/{ids_by_path['Orcs/Orc.png']}").json()["asset"][
        "tags"
    ] == []


def test_batch_tags_full_backend_search_result_scope(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Goblins" / "Goblin A.png")
    write_image(source_root / "Goblins" / "Goblin B.png")
    write_image(source_root / "Goblins" / "Goblin C.png")
    write_image(source_root / "Orcs" / "Orc.png")
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
        rows = db.execute(
            "SELECT asset_id, relative_path FROM assets ORDER BY relative_path"
        ).fetchall()
    rebuild_search_corpus(config, db_path)
    ids_by_path = {row["relative_path"]: row["asset_id"] for row in rows}
    client = TestClient(create_app(config, db_path))

    response = client.post(
        "/api/tags/batch",
        json={
            "scope": {"kind": "search", "q": "goblin", "mode": "strict"},
            "add_tags": ["Goblins"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"matched": 3, "tagged": 3, "failed": []}
    for path in (
        "Goblins/Goblin A.png",
        "Goblins/Goblin B.png",
        "Goblins/Goblin C.png",
    ):
        assert client.get(f"/api/assets/{ids_by_path[path]}").json()["asset"][
            "tags"
        ] == [{"slug": "goblins", "label": "Goblins"}]
    assert client.get(f"/api/assets/{ids_by_path['Orcs/Orc.png']}").json()["asset"][
        "tags"
    ] == []


def test_batch_tags_folder_subtree_scope(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Goblins" / "Goblin A.png")
    write_image(source_root / "Goblins" / "Nested" / "Goblin B.png")
    write_image(source_root / "Orcs" / "Orc.png")
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
        rows = db.execute(
            "SELECT asset_id, relative_path FROM assets ORDER BY relative_path"
        ).fetchall()
    rebuild_search_corpus(config, db_path)
    ids_by_path = {row["relative_path"]: row["asset_id"] for row in rows}
    client = TestClient(create_app(config, db_path))

    response = client.post(
        "/api/tags/batch",
        json={
            "scope": {"kind": "folder", "source_id": "dnd", "directory": "Goblins"},
            "add_tags": ["Goblins"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"matched": 2, "tagged": 2, "failed": []}
    assert client.get(f"/api/assets/{ids_by_path['Goblins/Goblin A.png']}").json()[
        "asset"
    ]["tags"] == [{"slug": "goblins", "label": "Goblins"}]
    assert client.get(
        f"/api/assets/{ids_by_path['Goblins/Nested/Goblin B.png']}"
    ).json()["asset"]["tags"] == [{"slug": "goblins", "label": "Goblins"}]
    assert client.get(f"/api/assets/{ids_by_path['Orcs/Orc.png']}").json()["asset"][
        "tags"
    ] == []


def test_batch_removes_tags_from_mixed_selection(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Goblin A.png")
    write_image(source_root / "Goblin B.png")
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
        rows = db.execute(
            "SELECT asset_id, relative_path FROM assets ORDER BY relative_path"
        ).fetchall()
    rebuild_search_corpus(config, db_path)
    ids_by_path = {row["relative_path"]: row["asset_id"] for row in rows}
    client = TestClient(create_app(config, db_path))
    first_id = ids_by_path["Goblin A.png"]
    second_id = ids_by_path["Goblin B.png"]
    client.put(f"/api/assets/{first_id}/tags", json={"tags": ["Encounter", "NPC"]})
    client.put(f"/api/assets/{second_id}/tags", json={"tags": ["NPC"]})

    response = client.post(
        "/api/tags/batch",
        json={
            "asset_ids": [first_id, second_id],
            "remove_tags": ["Encounter"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"matched": 2, "tagged": 2, "failed": []}
    assert client.get(f"/api/assets/{first_id}").json()["asset"]["tags"] == [
        {"slug": "npc", "label": "NPC"}
    ]
    assert client.get(f"/api/assets/{second_id}").json()["asset"]["tags"] == [
        {"slug": "npc", "label": "NPC"}
    ]


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


def test_asset_metadata_includes_audio_duration(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_audio(source_root / "sounds" / "Thunder.mp3", duration_seconds=2.5)
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp3"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
        audio_id = db.execute("SELECT asset_id FROM assets").fetchone()["asset_id"]
    rebuild_search_corpus(config, db_path)

    client = TestClient(create_app(config, db_path))

    audio_asset = client.get(f"/api/assets/{audio_id}").json()["asset"]
    assert audio_asset["media_type"] == "audio"
    assert audio_asset["media_width"] is None
    assert audio_asset["media_height"] is None
    assert audio_asset["duration_seconds"] >= 2.4

    search_asset = client.get("/api/search", params={"q": "thunder"}).json()["cards"][0][
        "asset"
    ]
    assert search_asset["duration_seconds"] >= 2.4
