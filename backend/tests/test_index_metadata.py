from pathlib import Path

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources
from findme.search_corpus import rebuild_search_corpus, search_corpus_is_stale

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
