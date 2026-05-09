from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources

from helpers import write_image


def test_api_lists_sources_and_current_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "goblin.png")
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
    body = client.get("/api/assets").json()
    assert body["total"] == 1
    assets = body["assets"]
    assert len(assets) == 1
    assert assets[0]["source_id"] == "dnd"
    assert assets[0]["relative_path"] == "goblin.png"
    assert assets[0]["media_type"] == "image"
    assert assets[0]["thumbnail_url"].startswith(
        f"/api/assets/{assets[0]['asset_id']}/thumb?v="
    )


def test_api_search_returns_ranked_results(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin Warrior.png")
    write_image(source_root / "Dragon.png")
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

    response = client.get("/api/search", params={"q": "goblin"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["assets"][0]["relative_path"] == "Goblin Warrior.png"
    assert body["assets"][0]["thumbnail_url"].startswith(
        f"/api/assets/{body['assets'][0]['asset_id']}/thumb?v="
    )
    assert body["assets"][0]["score"] > 0


def test_api_search_accepts_search_mode(tmp_path: Path) -> None:
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

    client = TestClient(create_app(config, db_path))

    strict_body = client.get(
        "/api/search",
        params={"q": "goblin warror", "mode": "strict"},
    ).json()
    fuzzy_body = client.get(
        "/api/search",
        params={"q": "goblin warror", "mode": "fuzzy"},
    ).json()

    assert strict_body["total"] == 0
    assert fuzzy_body["total"] == 1
    assert fuzzy_body["assets"][0]["relative_path"] == "Goblin Warrior.png"


def test_api_domain_search_uses_configured_vocabulary(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Human Mage.png")
    vocabulary_path = tmp_path / "vocabulary.yaml"
    vocabulary_path.write_text(
        """
version: 1
expansions:
  hero:
    - mage
""",
        encoding="utf-8",
    )
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
        domain_vocabulary_path=vocabulary_path,
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)

    client = TestClient(create_app(config, db_path))

    body = client.get("/api/search", params={"q": "hero", "mode": "domain"}).json()

    assert body["total"] == 1
    assert body["assets"][0]["relative_path"] == "Human Mage.png"


def test_api_result_totals_ignore_limit_and_offset(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin Archer.png")
    write_image(source_root / "Goblin Warrior.png")
    write_image(source_root / "Orc.png")
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

    assets_body = client.get("/api/assets", params={"limit": 1, "offset": 1}).json()
    search_body = client.get("/api/search", params={"q": "goblin", "limit": 1, "offset": 1}).json()

    assert assets_body["total"] == 3
    assert len(assets_body["assets"]) == 1
    assert search_body["total"] == 2
    assert len(search_body["assets"]) == 1


def test_api_search_uses_startup_search_index_until_app_restart(tmp_path: Path) -> None:
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

    client = TestClient(create_app(config, db_path))

    with connect(db_path) as db:
        db.execute("UPDATE assets SET stale = 1 WHERE relative_path = ?", ("Goblin.png",))
        db.execute("DELETE FROM asset_search WHERE filename = ?", ("Goblin.png",))

    body = client.get("/api/search", params={"q": "goblin"}).json()

    assert body["total"] == 1
    assert body["assets"][0]["relative_path"] == "Goblin.png"


def test_api_directories_use_startup_search_index_until_app_restart(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Creatures").mkdir(parents=True)
    write_image(source_root / "Creatures" / "Goblin.png")
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

    with connect(db_path) as db:
        db.execute(
            "UPDATE assets SET stale = 1 WHERE relative_path = ?",
            ("Creatures/Goblin.png",),
        )
        db.execute("DELETE FROM asset_search WHERE filename = ?", ("Goblin.png",))

    body = client.get("/api/directories", params={"q": "goblin"}).json()

    directories = {(item["source_id"], item["path"]): item for item in body["directories"]}
    assert directories[("dnd", "")]["asset_count"] == 1
    assert directories[("dnd", "")]["matched_asset_count"] == 1
    assert directories[("dnd", "Creatures")]["asset_count"] == 1
    assert directories[("dnd", "Creatures")]["matched_asset_count"] == 1


def test_api_search_rejects_superseded_search_revision(tmp_path: Path) -> None:
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

    client = TestClient(create_app(config, db_path))

    current = client.get(
        "/api/search",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 2},
    )
    superseded = client.get(
        "/api/search",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 1},
    )

    assert current.status_code == 200
    assert superseded.status_code == 204


def test_api_directories_reject_superseded_search_revision(tmp_path: Path) -> None:
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

    client = TestClient(create_app(config, db_path))

    current = client.get(
        "/api/directories",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 2},
    )
    superseded = client.get(
        "/api/directories",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 1},
    )

    assert current.status_code == 200
    assert superseded.status_code == 204


def test_api_requests_do_not_initialize_database(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
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

    def fail_request_time_initialization(_db) -> None:
        raise AssertionError("request attempted schema initialization")

    monkeypatch.setattr(
        "findme.api.initialize_database", fail_request_time_initialization
    )

    response = client.get("/api/assets")

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_api_serves_static_frontend_when_configured(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>Findme</main>", encoding="utf-8")
    config = AppConfig(data_root=tmp_path / "data", sources=())

    client = TestClient(create_app(config, tmp_path / "findme.sqlite3", static_dir=static_dir))

    assert client.get("/").text == "<main>Findme</main>"
