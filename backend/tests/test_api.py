from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources

from helpers import write_image


def _card_asset(card: dict[str, object]) -> dict[str, object]:
    if card["kind"] == "asset":
        return card["asset"]  # type: ignore[return-value]
    return card["representative"]  # type: ignore[return-value]


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
    cards = body["cards"]
    assert len(cards) == 1
    asset = _card_asset(cards[0])
    assert asset["source_id"] == "dnd"
    assert asset["relative_path"] == "goblin.png"
    assert asset["media_type"] == "image"
    assert asset["thumbnail_url"].startswith(
        f"/api/assets/{asset['asset_id']}/thumb?v="
    )


def test_api_lists_numbered_variants_as_collection_cards(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / "Portraits").mkdir(parents=True)
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Tokens" / "Goblin2.png")
    write_image(source_root / "Tokens" / "Goblin [3].webp")
    write_image(source_root / "Portraits" / "Goblin4.png")
    write_image(source_root / "Tokens" / "Orc.png")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".webp"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)

    client = TestClient(create_app(config, db_path))

    body = client.get("/api/assets").json()

    assert body["total"] == 3
    cards = body["cards"]
    collection = next(card for card in cards if card["kind"] == "collection")
    assert collection["kind"] == "collection"
    assert collection["count"] == 3
    assert collection["representative"]["relative_path"] == "Tokens/Goblin.png"
    assert [asset["relative_path"] for asset in collection["variants"]] == [
        "Tokens/Goblin.png",
        "Tokens/Goblin2.png",
        "Tokens/Goblin [3].webp",
    ]
    asset_paths = [
        card["asset"]["relative_path"] for card in cards if card["kind"] == "asset"
    ]
    assert asset_paths == ["Portraits/Goblin4.png", "Tokens/Orc.png"]


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
    asset = _card_asset(body["cards"][0])
    assert asset["relative_path"] == "Goblin Warrior.png"
    assert asset["thumbnail_url"].startswith(
        f"/api/assets/{asset['asset_id']}/thumb?v="
    )
    assert asset["score"] > 0


def test_api_search_collection_includes_unmatched_variants(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    write_image(source_root / "Tokens" / "Human Mage.png")
    write_image(source_root / "Tokens" / "Human Mage2.png")
    write_image(source_root / "Tokens" / "Human Mage3.png")
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

    body = client.get("/api/search", params={"q": "mage2", "mode": "strict"}).json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert card["representative"]["relative_path"] == "Tokens/Human Mage2.png"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "Tokens/Human Mage.png",
        "Tokens/Human Mage2.png",
        "Tokens/Human Mage3.png",
    ]


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
    assert _card_asset(fuzzy_body["cards"][0])["relative_path"] == "Goblin Warrior.png"


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
    assert _card_asset(body["cards"][0])["relative_path"] == "Human Mage.png"


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
    assert len(assets_body["cards"]) == 1
    assert search_body["total"] == 2
    assert len(search_body["cards"]) == 1


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
    assert _card_asset(body["cards"][0])["relative_path"] == "Goblin.png"


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
