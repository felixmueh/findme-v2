from pathlib import Path
import threading
import time

import findme.search as search_module
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search import (
    DomainVocabulary,
    SearchControl,
    SearchIndex,
    SearchSuperseded,
    search_assets,
    upsert_folder_quality,
)

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


def test_search_index_applies_folder_quality_to_combined_and_strict_ranking(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Bad" / "Goblin.png")
    write_image(source_root / "Good" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

        assert [result.relative_path for result in search_index.search("goblin").assets] == [
            "Bad/Goblin.png",
            "Good/Goblin.png",
        ]

        upsert_folder_quality(db, source_id="dnd", directory="Good", quality=10)
        search_index.set_folder_quality("dnd", "Good", 10)

        combined = search_index.search("goblin").assets
        strict = search_index.search("goblin", mode="strict").assets

    assert [result.relative_path for result in combined] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]
    assert [result.relative_path for result in strict] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]

    with connect(tmp_path / "findme.sqlite3") as db:
        reloaded = SearchIndex.load(db)

    assert [result.relative_path for result in reloaded.search("goblin").assets] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]

    with connect(tmp_path / "findme.sqlite3") as db:
        upsert_folder_quality(db, source_id="dnd", directory="Good", quality=5)
        reloaded.set_folder_quality("dnd", "Good", 5)
        assert reloaded.folder_quality("dnd", "Good") == (5, None)
        stored_overrides = db.execute(
            "SELECT quality FROM folder_quality_overrides WHERE source_id = ? AND directory = ?",
            ("dnd", "Good"),
        ).fetchall()

    assert stored_overrides == []
    assert [result.relative_path for result in reloaded.search("goblin").assets] == [
        "Bad/Goblin.png",
        "Good/Goblin.png",
    ]


def test_search_index_updates_only_assets_with_changed_effective_folder_quality(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Parent" / "Inherited" / "Goblin.png")
    write_image(source_root / "Parent" / "Overridden" / "Goblin.png")
    write_image(source_root / "Other" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        upsert_folder_quality(
            db,
            source_id="dnd",
            directory="Parent/Overridden",
            quality=0,
        )
        search_index = SearchIndex.load(db)

    def fail_full_recompute(*_args, **_kwargs):
        raise AssertionError("folder quality update rebuilt all preferences")

    monkeypatch.setattr(search_module, "_preference_adjustments", fail_full_recompute)

    search_index.set_folder_quality("dnd", "Parent", 10)

    assert search_index.folder_quality("dnd", "Parent/Inherited") == (10, None)
    assert search_index.folder_quality("dnd", "Parent/Overridden") == (0, 0)
    assert [result.relative_path for result in search_index.search("goblin").assets] == [
        "Parent/Inherited/Goblin.png",
        "Other/Goblin.png",
        "Parent/Overridden/Goblin.png",
    ]


def test_directories_for_matches_keep_each_source_subtree_together(tmp_path: Path) -> None:
    dnd_root = tmp_path / "dnd"
    maps_root = tmp_path / "maps"
    write_image(dnd_root / "Tokens" / "Goblin.png")
    write_image(maps_root / "Tokens" / "Cave.png")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=dnd_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="maps",
                display_name="Maps",
                root=maps_root,
                priority=5,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    assert [
        (directory.source_id, directory.path)
        for directory in search_index.directories_for_matches(
            search_index.scoped_assets()
        )
    ] == [
        ("dnd", ""),
        ("dnd", "Tokens"),
        ("maps", ""),
        ("maps", "Tokens"),
    ]


def test_search_index_stops_when_control_is_cancelled(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin.png")
    write_image(source_root / "Goblin Warrior.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    control = SearchControl(lambda: True, check_every=1)

    try:
        search_index.search("goblin", control=control)
    except SearchSuperseded:
        pass
    else:
        raise AssertionError("search did not stop after cancellation")


def test_search_index_builds_directories_from_existing_ranked_results(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Creatures").mkdir(parents=True)
    write_image(source_root / "Creatures" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    calls = 0
    original_rank_search_assets = search_module._rank_search_assets

    def count_rank_calls(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_rank_search_assets(*args, **kwargs)

    monkeypatch.setattr(search_module, "_rank_search_assets", count_rank_calls)

    page = search_index.search("goblin")
    directories = search_index.directories_for_matches(page.assets)

    assert calls == 1
    assert [(directory.path, directory.matched_asset_count) for directory in directories] == [
        ("", 1),
        ("Creatures", 1),
    ]


def test_search_index_coalesces_concurrent_identical_queries(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Creatures").mkdir(parents=True)
    write_image(source_root / "Creatures" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    calls = 0
    original_rank_search_assets = search_module._rank_search_assets

    def slow_rank_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return original_rank_search_assets(*args, **kwargs)

    monkeypatch.setattr(search_module, "_rank_search_assets", slow_rank_search)

    barrier = threading.Barrier(2)
    errors = []

    def run_search() -> None:
        try:
            barrier.wait()
            search_index.search("goblin")
        except Exception as exc:
            errors.append(exc)

    def run_second_search() -> None:
        try:
            barrier.wait()
            search_index.search("goblin")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_search), threading.Thread(target=run_second_search)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert calls == 1


def test_search_index_retries_after_cancelled_in_flight_query(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Creatures").mkdir(parents=True)
    write_image(source_root / "Creatures" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    calls = 0
    release_owner = threading.Event()
    owner_started = threading.Event()
    original_rank_search_assets = search_module._rank_search_assets

    def controlled_rank_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            owner_started.set()
            release_owner.wait(timeout=1)
        return original_rank_search_assets(*args, **kwargs)

    monkeypatch.setattr(search_module, "_rank_search_assets", controlled_rank_search)

    first_cancelled = False
    first_control = SearchControl(lambda: first_cancelled, check_every=1)
    errors = []

    def run_first_search() -> None:
        try:
            search_index.search("goblin", control=first_control)
        except SearchSuperseded:
            pass
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_first_search)
    thread.start()
    assert owner_started.wait(timeout=1)
    first_cancelled = True
    release_owner.set()

    page = search_index.search("goblin", control=SearchControl(lambda: False, check_every=1))

    thread.join(timeout=1)
    assert not thread.is_alive()
    assert errors == []
    assert calls == 2
    assert [asset.filename for asset in page.assets] == ["Goblin.png"]


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
