from pathlib import Path

import pytest

from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database, refresh_index_fingerprint
from findme.indexer import index_sources
from findme.search_corpus import (
    SearchCorpusError,
    rebuild_search_corpus,
    search_corpus_is_stale,
)

from helpers import write_image


def test_search_corpus_freshness_tracks_index_fingerprint(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "dragon.png"
    write_image(asset_path)
    db_path = tmp_path / "findme.sqlite3"
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

    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
        assert search_corpus_is_stale(db) is True

    rebuild_search_corpus(config, db_path)
    with connect(db_path) as db:
        initialize_database(db)
        assert search_corpus_is_stale(db) is False

    asset_path.unlink()
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
        assert search_corpus_is_stale(db) is True


def test_search_corpus_freshness_tracks_thumbnail_variant_changes(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "map.png", size=(1200, 600))
    db_path = tmp_path / "findme.sqlite3"
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
        additional_thumbnail_mipmaps=(512,),
    )

    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
    rebuild_search_corpus(config, db_path)

    with connect(db_path) as db:
        initialize_database(db)
        assert search_corpus_is_stale(db) is False
        db.execute("DELETE FROM asset_thumbnail_variants WHERE max_edge = 512")
        refresh_index_fingerprint(db)
        assert search_corpus_is_stale(db) is True


def test_app_startup_rejects_stale_search_corpus(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "dragon.png"
    write_image(asset_path)
    db_path = tmp_path / "findme.sqlite3"
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

    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)
    rebuild_search_corpus(config, db_path)

    asset_path.unlink()
    with connect(db_path) as db:
        initialize_database(db)
        index_sources(db, config, thumbnail_workers=1)

    with pytest.raises(SearchCorpusError, match="search corpus is stale"):
        create_app(config, db_path)
