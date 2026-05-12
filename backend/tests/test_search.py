from pathlib import Path
import threading
import time

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search import (
    DirectoryFilter,
    DirectoryFilterRule,
    DomainVocabulary,
    SearchControl,
    SearchIndex,
    SearchSuperseded,
    folder_settings_fingerprint,
    includes_asset,
)

from helpers import write_image


def test_search_index_matches_filename_and_path_tokens(tmp_path: Path) -> None:
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
        results = SearchIndex.load(db).search("goblin").assets

    assert [result.relative_path for result in results] == [
        "Tokens/Goblins/Goblin Warrior.png",
        "Maps/Goblin Cave.jpg",
    ]
    assert all(result.score > 0 for result in results)


def test_search_index_applies_source_and_directory_filters(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / "Maps").mkdir()
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Maps" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        results = SearchIndex.load(db).search(
            "goblin",
            source_id="dnd",
            directory="Tokens",
        ).assets

    assert [result.relative_path for result in results] == ["Tokens/Goblin.png"]


def test_directory_filter_rules_apply_deepest_specified_ancestor() -> None:
    assert includes_asset("dnd", "Tokens", DirectoryFilter()) is True

    blacklist_only = DirectoryFilter(
        (DirectoryFilterRule("dnd", "Tokens/Unused", "blacklist"),)
    )
    assert includes_asset("dnd", "Tokens/Unused", blacklist_only) is False
    assert includes_asset("dnd", "Tokens", blacklist_only) is True
    assert includes_asset("other", "Tokens/Unused", blacklist_only) is True

    whitelist_mode = DirectoryFilter(
        (DirectoryFilterRule("dnd", "Maps", "whitelist"),)
    )
    assert includes_asset("dnd", "Maps/Caves", whitelist_mode) is True
    assert includes_asset("dnd", "Tokens", whitelist_mode) is False

    whitelist_overrides_root_blacklist = DirectoryFilter(
        (
            DirectoryFilterRule("dnd", "", "blacklist"),
            DirectoryFilterRule("dnd", "Maps", "whitelist"),
        )
    )
    assert includes_asset("dnd", "Maps/Caves", whitelist_overrides_root_blacklist) is True
    assert includes_asset("dnd", "Tokens", whitelist_overrides_root_blacklist) is False

    blacklist_overrides_parent_whitelist = DirectoryFilter(
        (
            DirectoryFilterRule("dnd", "", "whitelist"),
            DirectoryFilterRule("dnd", "Maps/Unused", "blacklist"),
        )
    )
    assert includes_asset("dnd", "Maps/Unused", blacklist_overrides_parent_whitelist) is False
    assert includes_asset("dnd", "Maps", blacklist_overrides_parent_whitelist) is True
    assert includes_asset("other", "Maps/Unused", blacklist_overrides_parent_whitelist) is False


def test_search_index_drops_removed_assets_after_reindex(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    removed_path = source_root / "Goblin.png"
    write_image(removed_path)
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        removed_path.unlink()
        index_sources(db, config)
        results = SearchIndex.load(db).search("goblin").assets

    assert results == []


def test_fuzzy_search_finds_assets_with_typo_in_query(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "Goblin Warrior.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)
        fuzzy_results = search_index.search("goblin warror", mode="fuzzy").assets
        strict_results = search_index.search("goblin warror", mode="strict").assets

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
        search_index = SearchIndex.load(db)
        domain_results = search_index.search("adventurer", mode="domain").assets
        strict_results = search_index.search("adventurer", mode="strict").assets

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
        search_index = SearchIndex.load(db)
        before = search_index.search("hero", mode="domain").assets
        after = search_index.search(
            "hero",
            mode="domain",
            domain_vocabulary=DomainVocabulary(expansions={"hero": ("mage",)}),
        ).assets

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
        results = SearchIndex.load(db).search("combat music", mode="strict").assets

    assert [result.relative_path for result in results] == ["combat_standard/Training.mp3"]


def test_search_ranking_does_not_apply_media_intent_penalty(
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
        search_index = SearchIndex.load(db)
        visual_result = search_index.search("door", source_id="mapmaking").assets[0]
        audio_result = search_index.search("door", source_id="sounds").assets[0]

    assert audio_result.score > 0
    assert visual_result.score > 0


def test_search_index_applies_profile_quality_to_strict_ranking(
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

        preferences = search_index.compile_preferences(
            quality_overrides={("dnd", "Good"): 10},
        )

        strict = search_index.search("goblin", mode="strict", preferences=preferences).assets

    assert [result.relative_path for result in strict] == [
        "Good/Goblin.png",
        "Bad/Goblin.png",
    ]


def test_profile_fingerprint_normalizes_equivalent_empty_settings() -> None:
    assert (
        folder_settings_fingerprint(DirectoryFilter(), {})
        == folder_settings_fingerprint(DirectoryFilter(), {("dnd", "Good"): 5})
        == "empty"
    )


def test_search_index_applies_profile_selection_as_hard_filter(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Goblin.png")
    write_image(source_root / "Tokens" / "Goblin.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    preferences = search_index.compile_preferences(
        directory_filter=DirectoryFilter(
            (DirectoryFilterRule("dnd", "Maps", "whitelist"),)
        ),
    )

    assert [
        result.relative_path
        for result in search_index.search("goblin", preferences=preferences).assets
    ] == ["Maps/Goblin.png"]
    assert [
        result.relative_path
        for result in search_index.browse(
            preferences=preferences,
            limit=len(search_index.assets),
        ).assets
    ] == ["Maps/Goblin.png"]


def test_search_index_compiled_selection_uses_nearest_folder_rule(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Keep.png")
    write_image(source_root / "Tokens" / "Skip.png")
    config = _config(tmp_path, source_root)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        search_index = SearchIndex.load(db)

    preferences = search_index.compile_preferences(
        directory_filter=DirectoryFilter(
            (
                DirectoryFilterRule("dnd", "", "blacklist"),
                DirectoryFilterRule("dnd", "Maps", "whitelist"),
            )
        ),
    )

    assert [
        result.relative_path
        for result in search_index.browse(
            preferences=preferences,
            limit=len(search_index.assets),
        ).assets
    ] == ["Maps/Keep.png"]


def test_directory_responses_keep_each_source_subtree_together(tmp_path: Path) -> None:
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
        (directory["source_id"], directory["path"])
        for directory in search_index.directory_responses_for_scope()
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


def test_search_with_directory_responses_reuses_search_matches(
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
    assert search_index.lexical_index is not None
    original_search_with_scores = search_index.lexical_index.search_with_scores

    def count_search_calls(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_search_with_scores(*args, **kwargs)

    monkeypatch.setattr(
        search_index.lexical_index,
        "search_with_scores",
        count_search_calls,
    )

    response = search_index.search_with_directory_responses("goblin")

    assert calls == 1
    assert [
        (directory["path"], directory["matched_asset_count"])
        for directory in response.directories
    ] == [
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
    assert search_index.lexical_index is not None
    original_search_with_scores = search_index.lexical_index.search_with_scores

    def slow_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return original_search_with_scores(*args, **kwargs)

    monkeypatch.setattr(search_index.lexical_index, "search_with_scores", slow_search)

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
    assert search_index.lexical_index is not None
    original_search_with_scores = search_index.lexical_index.search_with_scores

    def controlled_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            owner_started.set()
            release_owner.wait(timeout=1)
        return original_search_with_scores(*args, **kwargs)

    monkeypatch.setattr(search_index.lexical_index, "search_with_scores", controlled_search)

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
