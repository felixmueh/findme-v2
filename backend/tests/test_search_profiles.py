from pathlib import Path

import numpy as np

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search import DirectoryFilter, DirectoryFilterRule, SearchIndex
from findme.search_profiles import SearchProfileSessionManager

from helpers import write_image


def test_sessions_with_identical_settings_share_compiled_preferences(
    tmp_path: Path,
) -> None:
    with _indexed_db(tmp_path) as db:
        manager = SearchProfileSessionManager(SearchIndex.load(db))
        manager.update_setting(
            db,
            search_session="editor",
            source_id="dnd",
            directory="Tokens",
            selection_state="blacklist",
        )
        manager.save(db, search_session="editor")

        first = manager.preferences(db, "tab-1")
        second = manager.preferences(db, "tab-2")

        assert first is second

        manager.update_setting(
            db,
            search_session="tab-2",
            source_id="dnd",
            directory="Maps",
            quality_override=9,
        )

        assert manager.preferences(db, "tab-2") is not first
        assert manager.preferences(db, "tab-1") is first


def test_compiled_selection_stores_asset_order_as_int32_array(tmp_path: Path) -> None:
    with _indexed_db(tmp_path) as db:
        search_index = SearchIndex.load(db)

    preferences = search_index.compile_preferences(
        directory_filter=DirectoryFilter(
            (DirectoryFilterRule("dnd", "Tokens", "blacklist"),)
        ),
    )

    order = preferences.included_asset_index_order
    assert isinstance(order, np.ndarray)
    assert order.dtype == np.int32
    assert [search_index.assets[index].relative_path for index in order] == [
        "Maps/Goblin.png",
    ]


def _indexed_db(tmp_path: Path):
    source_root = tmp_path / "assets"
    write_image(source_root / "Maps" / "Goblin.png")
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
    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
    return connect(tmp_path / "findme.sqlite3")
