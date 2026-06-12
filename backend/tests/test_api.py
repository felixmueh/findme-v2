from pathlib import Path

from fastapi.testclient import TestClient

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.result_cards import ResultCardResolver
from findme.search import SearchIndex, SearchSuperseded
from findme.search_corpus import SearchCorpus, rebuild_search_corpus

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
    rebuild_search_corpus(config, db_path)

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
    body = client.get("/api/search").json()
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


def test_api_search_card_includes_thumbnail_mipmap_variants(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "map.png", size=(1200, 600))
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
        additional_thumbnail_mipmaps=(512, 1024),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
    rebuild_search_corpus(config, db_path)

    client = TestClient(create_app(config, db_path))
    body = client.get("/api/search").json()
    asset = _card_asset(body["cards"][0])

    assert [
        (variant["max_edge"], variant["width"], variant["height"])
        for variant in asset["thumbnail_variants"]
    ] == [
        (256, 256, 128),
        (512, 512, 256),
        (1024, 1024, 512),
    ]
    assert asset["thumbnail_variants"][0]["url"] == asset["thumbnail_url"]
    variant_url = asset["thumbnail_variants"][1]["url"]
    assert f"/api/assets/{asset['asset_id']}/thumb?max_edge=512&v=" in variant_url
    variant_response = client.get(variant_url)
    assert variant_response.status_code == 200
    assert variant_response.headers["content-type"] == "image/webp"


def test_api_requires_prebuilt_search_corpus(tmp_path: Path) -> None:
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

    try:
        create_app(config, db_path)
    except RuntimeError as exc:
        assert "rebuild-search-corpus" in str(exc)
    else:
        raise AssertionError("create_app should require a prebuilt search corpus")


def test_api_opens_prebuilt_lexical_index_without_rebuilding(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    rebuild_search_corpus(config, db_path)

    def fail_lexical_rebuild(*_args, **_kwargs):
        raise AssertionError("serve should open the prebuilt lexical index")

    monkeypatch.setattr("findme.search_corpus.SearchLexicalIndex.build", fail_lexical_rebuild)

    client = TestClient(create_app(config, db_path))
    body = client.get("/api/search", params={"q": "goblin", "mode": "strict"}).json()

    assert body["total"] == 1


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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    body = client.get("/api/search").json()

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


def test_api_updates_profile_quality_and_applies_it_to_current_ranking(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Bad" / "Goblin.png")
    write_image(source_root / "Good" / "Goblin.png")
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

    before = client.get("/api/search", params={"q": "goblin"}).json()
    assert [
        _card_asset(card)["relative_path"] for card in before["cards"]
    ] == [
        "Bad/Goblin.png",
        "Good/Goblin.png",
    ]

    response = client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Good",
            "quality_override": 10,
        },
    )

    assert response.status_code == 200
    assert response.json()["session"]["dirty_scoring"] is True
    after = client.get(
        "/api/search",
        params={"q": "goblin", "search_session": "tab-1"},
    ).json()
    strict_after = client.get(
        "/api/search",
        params={"q": "goblin", "mode": "strict", "search_session": "tab-1"},
    ).json()

    assert [
        _card_asset(card)["relative_path"] for card in after["cards"]
    ] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]
    assert [
        _card_asset(card)["relative_path"] for card in strict_after["cards"]
    ] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]

    save_response = client.post(
        "/api/search-profile/save",
        json={"search_session": "tab-1"},
    )
    assert save_response.status_code == 200

    persisted_client = TestClient(create_app(config, db_path))
    directories = persisted_client.get(
        "/api/search-with-directory-counts", params={"source_id": "dnd"}
    ).json()["directories"]

    assert next(directory for directory in directories if directory["path"] == "Good")[
        "quality"
    ] == 10


def test_api_marks_directory_subtrees_with_unsaved_profile_changes(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Nested" / "Castle.png")
    write_image(source_root / "Maps" / "Other" / "Cave.png")
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

    response = client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Nested",
            "quality_override": 8,
        },
    )
    assert response.status_code == 200

    dirty_body = client.get(
        "/api/search-with-directory-counts",
        params={"source_id": "dnd", "search_session": "tab-1"},
    ).json()
    dirty_directories = {
        directory["path"]: directory["profile_dirty"]
        for directory in dirty_body["directories"]
    }

    assert dirty_directories == {
        "": True,
        "Maps": True,
        "Maps/Nested": True,
        "Maps/Other": False,
    }

    save_response = client.post(
        "/api/search-profile/save",
        json={"search_session": "tab-1"},
    )
    assert save_response.status_code == 200

    saved_body = client.get(
        "/api/search-with-directory-counts",
        params={"source_id": "dnd", "search_session": "tab-1"},
    ).json()
    assert {
        directory["path"]: directory["profile_dirty"]
        for directory in saved_body["directories"]
    } == {
        "": False,
        "Maps": False,
        "Maps/Nested": False,
        "Maps/Other": False,
    }


def test_api_marks_directory_subtrees_with_descendant_selection_rules(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Nested" / "Castle.png")
    write_image(source_root / "Maps" / "Other" / "Cave.png")
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

    whitelist_response = client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Nested",
            "selection_state": "whitelist",
        },
    )
    blacklist_response = client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Other",
            "selection_state": "blacklist",
        },
    )
    save_response = client.post(
        "/api/search-profile/save",
        json={"search_session": "tab-1"},
    )

    assert whitelist_response.status_code == 200
    assert blacklist_response.status_code == 200
    assert save_response.status_code == 200

    body = client.get(
        "/api/search-with-directory-counts",
        params={"source_id": "dnd", "search_session": "tab-1"},
    ).json()

    assert {
        directory["path"]: (
            directory["has_descendant_whitelist"],
            directory["has_descendant_blacklist"],
            directory["profile_dirty"],
        )
        for directory in body["directories"]
    } == {
        "": (True, True, False),
        "Maps": (True, True, False),
        "Maps/Nested": (False, False, False),
        "Maps/Other": (False, False, False),
    }


def test_api_groups_numbered_variants_with_accidental_embedded_image_extension(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Map_1.png.jpg")
    write_image(source_root / "Map_2.png.jpg")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".jpg"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    body = client.get("/api/search").json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "Map_1.png.jpg",
        "Map_2.png.jpg",
    ]


def test_api_groups_numeric_letter_variants_as_collection_cards(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Dragon_5a.png")
    write_image(source_root / "Dragon_5b.png")
    write_image(source_root / "Dragon_6a.png")
    write_image(source_root / "Dragon_6b.png")
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

    body = client.get("/api/search").json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "Dragon_5a.png",
        "Dragon_5b.png",
        "Dragon_6a.png",
        "Dragon_6b.png",
    ]


def test_api_groups_color_variants_inside_filename_stems(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Portal_Blue_Glow.png")
    write_image(source_root / "Portal_green_Glow.png")
    write_image(source_root / "Portal_MAGENTA_Glow.png")
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

    body = client.get("/api/search").json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "Portal_Blue_Glow.png",
        "Portal_green_Glow.png",
        "Portal_MAGENTA_Glow.png",
    ]


def test_api_groups_letter_variants_inside_filename_stems(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Sigil_A_Active.png")
    write_image(source_root / "Sigil_B_Active.png")
    write_image(source_root / "Sigil_BC_Active.png")
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

    body = client.get("/api/search").json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "Sigil_A_Active.png",
        "Sigil_B_Active.png",
        "Sigil_BC_Active.png",
    ]


def test_api_groups_multiple_variant_positions_inside_filename_stems(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    spirits = source_root / "FA_Tokens" / "Spirits" / "Spirits_NPCs" / ".ts"
    write_image(spirits / "Human_Male_Blacksmith_A2_Spirit_02.png.jpg")
    write_image(spirits / "Human_Male_Blacksmith_B3_Spirit_03.png.jpg")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".jpg"}),
            ),
        ),
    )
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config)
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    body = client.get("/api/search").json()

    assert body["total"] == 1
    card = body["cards"][0]
    assert card["kind"] == "collection"
    assert [asset["relative_path"] for asset in card["variants"]] == [
        "FA_Tokens/Spirits/Spirits_NPCs/.ts/Human_Male_Blacksmith_A2_Spirit_02.png.jpg",
        "FA_Tokens/Spirits/Spirits_NPCs/.ts/Human_Male_Blacksmith_B3_Spirit_03.png.jpg",
    ]


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
    rebuild_search_corpus(config, db_path)
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
    rebuild_search_corpus(config, db_path)
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
    rebuild_search_corpus(config, db_path)
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


def test_api_search_with_directory_counts_uses_one_search_scope(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Tokens" / "Goblin Warrior.png")
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

    strict_body = client.get(
        "/api/search-with-directory-counts",
        params={"q": "goblin warror", "mode": "strict"},
    ).json()
    fuzzy_body = client.get(
        "/api/search-with-directory-counts",
        params={
            "q": "goblin warror",
            "mode": "fuzzy",
            "directory_prefix": "Tokens",
        },
    ).json()

    assert strict_body["total"] == 0
    assert strict_body["directories"] == []
    assert fuzzy_body["total"] == 1
    assert [
        _card_asset(card)["relative_path"] for card in fuzzy_body["cards"]
    ] == ["Tokens/Goblin Warrior.png"]
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in fuzzy_body["directories"]
    }
    assert directories[("dnd", "")]["matched_asset_count"] == 1
    assert directories[("dnd", "Tokens")]["matched_asset_count"] == 1


def test_api_query_modes_use_paged_numeric_path(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Goblin Cave.png")
    write_image(source_root / "Maps" / "Goblin Camp.png")
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Tokens" / "Goblin Warrior.png")
    write_image(source_root / "Characters" / "Human Mage.png")
    vocabulary_path = tmp_path / "vocabulary.yaml"
    vocabulary_path.write_text(
        """
version: 1
expansions:
  hero:
    - mage
""".strip(),
        encoding="utf-8",
    )
    config = AppConfig(
        data_root=tmp_path / "data",
        domain_vocabulary_path=vocabulary_path,
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

    search_body = client.get(
        "/api/search",
        params={"q": "goblin", "limit": 1},
    ).json()
    directory_body = client.get(
        "/api/search-with-directory-counts",
        params={"q": "goblin", "limit": 1},
    ).json()

    assert search_body["total"] == 4
    assert len(search_body["cards"]) == 1
    assert directory_body["total"] == 4
    assert len(directory_body["cards"]) == 1
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in directory_body["directories"]
    }
    assert directories[("dnd", "")]["matched_asset_count"] == 4
    assert directories[("dnd", "Maps")]["matched_asset_count"] == 2
    assert directories[("dnd", "Tokens")]["matched_asset_count"] == 2

    fuzzy_body = client.get(
        "/api/search",
        params={"q": "goblin warror", "mode": "fuzzy", "limit": 1},
    ).json()
    domain_body = client.get(
        "/api/search-with-directory-counts",
        params={"q": "hero", "mode": "domain", "limit": 1},
    ).json()

    assert fuzzy_body["total"] == 1
    assert _card_asset(fuzzy_body["cards"][0])["relative_path"] == (
        "Tokens/Goblin Warrior.png"
    )
    assert domain_body["total"] == 1
    assert _card_asset(domain_body["cards"][0])["relative_path"] == (
        "Characters/Human Mage.png"
    )


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
    rebuild_search_corpus(config, db_path)
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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    assets_api_body = client.get("/api/assets", params={"limit": 1, "offset": 1}).json()
    assets_body = client.get("/api/search", params={"limit": 1, "offset": 1}).json()
    search_body = client.get("/api/search", params={"q": "goblin", "limit": 1, "offset": 1}).json()
    directories_body = client.get(
        "/api/search-with-directory-counts",
        params={"limit": 1, "offset": 1},
    ).json()

    assert assets_api_body["total"] == 3
    assert len(assets_api_body["cards"]) == 1
    assert assets_api_body["next_offset"] == 2
    assert assets_api_body["has_more"] is True
    assert assets_body["total"] == 3
    assert len(assets_body["cards"]) == 1
    assert assets_body["next_offset"] == 2
    assert assets_body["has_more"] is True
    assert search_body["total"] == 2
    assert len(search_body["cards"]) == 1
    assert search_body["next_offset"] == 2
    assert search_body["has_more"] is False
    assert directories_body["total"] == 3
    assert len(directories_body["cards"]) == 1
    assert directories_body["next_offset"] == 2
    assert directories_body["has_more"] is True


def test_api_search_paginates_grouped_cards_without_repeating_collection_keys(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin.png")
    write_image(source_root / "Goblin2.png")
    write_image(source_root / "Goblin Archer.png")
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

    first = client.get("/api/search", params={"q": "goblin", "limit": 1}).json()
    second = client.get(
        "/api/search",
        params={"q": "goblin", "limit": 1, "offset": first["next_offset"]},
    ).json()

    assert first["cards"][0]["kind"] == "collection"
    assert second["cards"][0]["kind"] == "asset"
    assert first["next_offset"] == 1
    assert second["next_offset"] == 2


def test_api_applies_profile_selection_before_pagination(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Unused" / "Skip.png")
    write_image(source_root / "Tokens" / "Goblin.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps",
            "selection_state": "whitelist",
        },
    ).status_code == 200
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Unused",
            "selection_state": "blacklist",
        },
    ).status_code == 200

    body = client.get(
        "/api/assets",
        params={"limit": 1, "search_session": "tab-1"},
    ).json()

    assert body["total"] == 1
    assert [_card_asset(card)["relative_path"] for card in body["cards"]] == [
        "Maps/Keep.png"
    ]


def test_api_profile_blacklisted_folder_remains_visible_for_unblocking(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Unused" / "Skip.png")
    write_image(source_root / "Tokens" / "Goblin.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Unused",
            "selection_state": "blacklist",
        },
    ).status_code == 200

    body = client.get(
        "/api/search-with-directory-counts",
        params={"search_session": "tab-1"},
    ).json()
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in body["directories"]
    }

    assert body["total"] == 2
    blocked = directories[("dnd", "Maps/Unused")]
    assert blocked["asset_count"] == 1
    assert blocked["matched_asset_count"] == 1
    assert blocked["included_asset_count"] == 0
    assert blocked["selection_state"] == "blacklist"


def test_api_profile_blacklisted_search_match_keeps_directory_match_count(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Unused" / "Skip.png")
    write_image(source_root / "Tokens" / "Goblin.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Unused",
            "selection_state": "blacklist",
        },
    ).status_code == 200

    body = client.get(
        "/api/search-with-directory-counts",
        params={"q": "skip", "search_session": "tab-1"},
    ).json()
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in body["directories"]
    }

    assert body["total"] == 0
    blocked = directories[("dnd", "Maps/Unused")]
    assert blocked["matched_asset_count"] == 1
    assert blocked["included_asset_count"] == 0
    assert blocked["selection_state"] == "blacklist"


def test_api_profile_selection_browse_does_not_rebuild_search_corpus(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
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
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps",
            "selection_state": "whitelist",
        },
    ).status_code == 200

    def fail_from_assets(*_args, **_kwargs):
        raise AssertionError("profile-scoped browse rebuilt a SearchCorpus")

    monkeypatch.setattr(SearchCorpus, "from_assets", fail_from_assets)

    body = client.get(
        "/api/assets",
        params={"search_session": "tab-1"},
    ).json()

    assert body["total"] == 1
    assert [_card_asset(card)["relative_path"] for card in body["cards"]] == [
        "Maps/Keep.png"
    ]


def test_api_profile_selection_reuses_startup_card_resolver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Human Mage.png")
    write_image(source_root / "Maps" / "Human Mage2.png")
    write_image(source_root / "Tokens" / "Skip.png")
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
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps",
            "selection_state": "whitelist",
        },
    ).status_code == 200

    def fail_from_assets(*_args, **_kwargs):
        raise AssertionError("profile-scoped cards rebuilt ResultCardResolver")

    monkeypatch.setattr(ResultCardResolver, "from_assets", fail_from_assets)

    browse_body = client.get(
        "/api/assets",
        params={"search_session": "tab-1"},
    ).json()
    search_body = client.get(
        "/api/search",
        params={"q": "mage2", "mode": "strict", "search_session": "tab-1"},
    ).json()

    assert browse_body["total"] == 1
    assert browse_body["cards"][0]["kind"] == "collection"
    assert search_body["total"] == 1
    assert search_body["cards"][0]["kind"] == "collection"
    assert search_body["cards"][0]["representative"]["relative_path"] == (
        "Maps/Human Mage2.png"
    )


def test_api_rejects_malformed_profile_selection_rules(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Nested" / "Keep.png")
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

    invalid_requests = [
        {"search_session": "tab-1", "source_id": "missing", "directory": "Maps", "selection_state": "whitelist"},
        {"search_session": "tab-1", "source_id": "dnd", "directory": "../Maps", "selection_state": "whitelist"},
        {"search_session": "tab-1", "source_id": "dnd", "directory": "Maps", "selection_state": "include"},
    ]

    for payload in invalid_requests:
        response = client.patch("/api/search-profile/settings", json=payload)

        assert response.status_code == 400
        assert response.json()["detail"]


def test_api_profile_nested_whitelist_supersedes_parent_for_inclusion(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Nested" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps",
            "quality_override": 8,
            "selection_state": "whitelist",
        },
    ).status_code == 200
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Nested",
            "selection_state": "whitelist",
        },
    ).status_code == 200

    profile_body = client.get(
        "/api/search-profiles",
        params={"search_session": "tab-1"},
    ).json()
    settings = {
        (setting["source_id"], setting["directory"]): setting
        for setting in profile_body["settings"]
    }
    browse_body = client.get(
        "/api/search-with-directory-counts",
        params={"search_session": "tab-1"},
    ).json()
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in browse_body["directories"]
    }

    assert settings[("dnd", "Maps")]["quality_override"] == 8
    assert settings[("dnd", "Maps")]["selection_state"] == "whitelist"
    assert settings[("dnd", "Maps/Nested")]["selection_state"] == "whitelist"
    assert directories[("dnd", "Maps")]["selection_state"] == "whitelist"
    assert directories[("dnd", "Maps")]["selection_effective"] is False
    assert directories[("dnd", "Maps")]["included_asset_count"] == 1
    assert directories[("dnd", "Maps/Nested")]["selection_state"] == "whitelist"
    assert directories[("dnd", "Maps/Nested")]["selection_effective"] is True
    assert browse_body["total"] == 1


def test_api_profile_parent_whitelist_is_superseded_by_existing_child_whitelist(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Nested" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Nested",
            "selection_state": "whitelist",
        },
    ).status_code == 200
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps",
            "selection_state": "whitelist",
        },
    ).status_code == 200

    profile_body = client.get(
        "/api/search-profiles",
        params={"search_session": "tab-1"},
    ).json()
    settings = {
        (setting["source_id"], setting["directory"]): setting
        for setting in profile_body["settings"]
    }
    browse_body = client.get(
        "/api/search-with-directory-counts",
        params={"search_session": "tab-1"},
    ).json()
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in browse_body["directories"]
    }

    assert settings[("dnd", "Maps")]["selection_state"] == "whitelist"
    assert settings[("dnd", "Maps/Nested")]["selection_state"] == "whitelist"
    assert directories[("dnd", "Maps")]["selection_effective"] is False
    assert directories[("dnd", "Maps/Nested")]["selection_effective"] is True
    assert browse_body["total"] == 1


def test_api_view_scope_ignores_sibling_whitelist_and_keeps_descendant_rules(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Maps" / "Unused" / "Skip.png")
    write_image(source_root / "Tokens" / "Skip.png")
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

    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Tokens",
            "selection_state": "whitelist",
        },
    ).status_code == 200
    assert client.patch(
        "/api/search-profile/settings",
        json={
            "search_session": "tab-1",
            "source_id": "dnd",
            "directory": "Maps/Unused",
            "selection_state": "blacklist",
        },
    ).status_code == 200

    unscoped_body = client.get(
        "/api/search-with-directory-counts",
        params={"search_session": "tab-1"},
    ).json()
    scoped_body = client.get(
        "/api/search-with-directory-counts",
        params={
            "search_session": "tab-1",
            "view_source_id": "dnd",
            "view_directory": "Maps",
        },
    ).json()
    scoped_search_body = client.get(
        "/api/search",
        params={
            "q": "skip",
            "mode": "strict",
            "search_session": "tab-1",
            "view_source_id": "dnd",
            "view_directory": "Maps",
        },
    ).json()
    directories = {
        (directory["source_id"], directory["path"]): directory
        for directory in scoped_body["directories"]
    }

    assert unscoped_body["total"] == 1
    assert [_card_asset(card)["relative_path"] for card in scoped_body["cards"]] == [
        "Maps/Keep.png"
    ]
    assert scoped_body["total"] == 1
    assert directories[("dnd", "Maps")]["included_asset_count"] == 1
    assert directories[("dnd", "Maps/Unused")]["selection_state"] == "blacklist"
    assert directories[("dnd", "Maps/Unused")]["included_asset_count"] == 0
    assert ("dnd", "Tokens") not in directories
    assert scoped_search_body["total"] == 0


def test_api_view_scope_browse_uses_numeric_asset_indexes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Goblin.png")
    write_image(source_root / "Maps" / "Goblin2.png")
    write_image(source_root / "Maps" / "Orc.png")
    write_image(source_root / "Tokens" / "Goblin.png")
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

    def fail_browse_asset_indexes(*_args, **_kwargs):
        raise AssertionError("folder view browse should use numeric asset indexes")

    monkeypatch.setattr(SearchIndex, "browse_asset_indexes", fail_browse_asset_indexes)
    client = TestClient(create_app(config, db_path))

    body = client.get(
        "/api/search-with-directory-counts",
        params={
            "view_source_id": "dnd",
            "view_directory": "Maps",
        },
    ).json()

    assert body["total"] == 2
    assert [_card_asset(card)["relative_path"] for card in body["cards"]] == [
        "Maps/Goblin.png",
        "Maps/Orc.png",
    ]


def test_api_can_return_compact_directory_state_updates(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
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

    full_body = client.get("/api/search-with-directory-counts").json()
    compact_body = client.get(
        "/api/search-with-directory-counts",
        params={
            "view_source_id": "dnd",
            "view_directory": "Maps",
            "directory_state_only": "true",
        },
    ).json()
    full_by_path = {directory["path"]: directory for directory in full_body["directories"]}

    assert "directories" not in compact_body
    assert len(compact_body["cards"]) == 1
    assert compact_body["total"] == 1
    assert compact_body["directory_states"] == [
        [
            full_by_path[""]["id"],
            1,
            1,
            5,
            None,
            None,
            True,
            False,
            False,
            False,
        ],
        [
            full_by_path["Maps"]["id"],
            1,
            1,
            5,
            None,
            None,
            True,
            False,
            False,
            False,
        ],
    ]


def test_api_can_return_directory_state_without_cards(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
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

    body = client.get(
        "/api/search-with-directory-counts",
        params={
            "q": "keep",
            "directory_only": "true",
            "directory_state_only": "true",
        },
    ).json()

    assert "cards" not in body
    assert "total" not in body
    assert "directories" not in body
    assert len(body["directory_states"]) == 2


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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    with connect(db_path) as db:
        db.execute("DELETE FROM assets WHERE relative_path = ?", ("Goblin.png",))

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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    with connect(db_path) as db:
        db.execute(
            "DELETE FROM assets WHERE relative_path = ?",
            ("Creatures/Goblin.png",),
        )

    body = client.get("/api/search-with-directory-counts", params={"q": "goblin"}).json()

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
    rebuild_search_corpus(config, db_path)
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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    current = client.get(
        "/api/search-with-directory-counts",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 2},
    )
    superseded = client.get(
        "/api/search-with-directory-counts",
        params={"q": "goblin", "search_session": "tab-1", "search_revision": 1},
    )

    assert current.status_code == 200
    assert superseded.status_code == 204


def test_api_search_with_directory_counts_rejects_superseded_directory_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "assets"
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

    def supersede_directory_counts(*_args, **_kwargs):
        raise SearchSuperseded

    monkeypatch.setattr(
        SearchIndex,
        "directory_responses_for_search",
        supersede_directory_counts,
    )

    response = client.get(
        "/api/search-with-directory-counts",
        params=[
            ("q", "goblin"),
            ("search_session", "tab-1"),
            ("search_revision", "1"),
        ],
    )

    assert response.status_code == 204


def test_api_uses_preinitialized_database(tmp_path: Path) -> None:
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
    rebuild_search_corpus(config, db_path)
    client = TestClient(create_app(config, db_path))

    response = client.get("/api/search")

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_api_serves_static_frontend_when_configured(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>Findme</main>", encoding="utf-8")
    config = AppConfig(data_root=tmp_path / "data", sources=())
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)
    rebuild_search_corpus(config, db_path)

    client = TestClient(create_app(config, db_path, static_dir=static_dir))

    assert client.get("/").text == "<main>Findme</main>"
