from pathlib import Path

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search import DomainVocabulary, search_assets

from helpers import write_image


def test_search_assets_matches_filename_and_path_tokens(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens" / "Goblins").mkdir(parents=True)
    (source_root / "Maps").mkdir()
    write_image(source_root / "Tokens" / "Goblins" / "Goblin Warrior.png")
    write_image(source_root / "Maps" / "Goblin Cave.jpg")
    write_image(source_root / "Tokens" / "Dragon.webp")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "goblin warrior")

    assert [result.relative_path for result in results] == [
        "Tokens/Goblins/Goblin Warrior.png",
        "Maps/Goblin Cave.jpg",
    ]
    assert results[0].score > results[1].score


def test_search_assets_applies_source_and_directory_filters(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / "Maps").mkdir()
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Maps" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "goblin", source_id="dnd", directory="Tokens")

    assert [result.relative_path for result in results] == ["Tokens/Goblin.png"]


def test_search_assets_hides_stale_assets(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    stale_path = source_root / "Goblin.png"
    write_image(stale_path)
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        stale_path.unlink()
        index_sources(db, config)
        results = search_assets(db, "goblin")

    assert results == []


def test_fuzzy_search_finds_assets_with_typo_in_query(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin Warrior.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        fuzzy_results = search_assets(db, "goblin warror", mode="fuzzy")
        strict_results = search_assets(db, "goblin warror", mode="strict")

    assert [result.relative_path for result in fuzzy_results] == ["Goblin Warrior.png"]
    assert strict_results == []


def test_domain_search_expands_broad_character_concepts(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Human Mage.png")
    write_image(source_root / "Halfling Rogue.png")
    write_image(source_root / "Dragon.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        domain_results = search_assets(db, "adventurer", mode="domain")
        strict_results = search_assets(db, "adventurer", mode="strict")

    assert [result.relative_path for result in domain_results] == [
        "Halfling Rogue.png",
        "Human Mage.png",
    ]
    assert strict_results == []


def test_domain_vocabulary_is_applied_at_query_time(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Human Mage.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        before = search_assets(db, "hero", mode="domain")
        after = search_assets(
            db,
            "hero",
            mode="domain",
            domain_vocabulary=DomainVocabulary(expansions={"hero": ("mage",)}),
        )

    assert before == []
    assert [result.relative_path for result in after] == ["Human Mage.png"]


def test_strict_search_uses_source_name_as_indexed_evidence(tmp_path: Path) -> None:
    source_root = tmp_path / "music"
    (source_root / "combat_standard").mkdir(parents=True)
    (source_root / "combat_standard" / "Training.mp3").write_bytes(b"audio")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="music",
                display_name="Music",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp3"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "combat music", mode="strict")

    assert [result.relative_path for result in results] == ["combat_standard/Training.mp3"]


def test_combined_search_penalizes_audio_sources_for_visual_object_queries(
    tmp_path: Path,
) -> None:
    visual_root = tmp_path / "mapmaking"
    sound_root = tmp_path / "sounds"
    visual_root.mkdir()
    sound_root.mkdir()
    write_image(visual_root / "StoneDoor.png")
    (sound_root / "door_big_close.mp3").write_bytes(b"audio")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="mapmaking",
                display_name="MapMaking",
                root=visual_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="sounds",
                display_name="Sounds",
                root=sound_root,
                priority=10,
                media_extensions=frozenset({".mp3"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = search_assets(db, "door")

    assert [result.relative_path for result in results] == [
        "StoneDoor.png",
        "door_big_close.mp3",
    ]


def _config(tmp_path: Path, source_root: Path) -> AppConfig:
    return AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".jpg", ".webp"}),
            ),
        ),
    )
