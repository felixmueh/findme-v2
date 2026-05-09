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


def test_api_serves_static_frontend_when_configured(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>Findme</main>", encoding="utf-8")
    config = AppConfig(data_root=tmp_path / "data", sources=())

    client = TestClient(create_app(config, tmp_path / "findme.sqlite3", static_dir=static_dir))

    assert client.get("/").text == "<main>Findme</main>"
