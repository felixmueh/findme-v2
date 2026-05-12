from pathlib import Path

import time

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import count_indexable_files, index_sources
from findme import thumbnails

import findme.indexer as indexer

from helpers import write_image, write_video


def test_index_sources_adds_media_files_and_ignores_legacy_findme_data(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / ".findme").mkdir()
    write_image(source_root / "Tokens" / "Goblin Warrior.PNG")
    (source_root / "Tokens" / "notes.txt").write_text("ignore", encoding="utf-8")
    (source_root / ".findme" / "old.png").write_bytes(b"ignore")

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
        summary = index_sources(db, config)
        rows = db.execute(
            "SELECT source_id, relative_path, filename, stem, extension, stale FROM assets"
        ).fetchall()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert [tuple(row) for row in rows] == [
        ("dnd", "Tokens/Goblin Warrior.PNG", "Goblin Warrior.PNG", "Goblin Warrior", ".png", 0)
    ]


def test_count_indexable_files_matches_indexed_media_scope(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / ".findme").mkdir()
    write_image(source_root / "Tokens" / "Goblin.png")
    write_image(source_root / "Tokens" / "Dragon.jpg")
    write_image(source_root / ".findme" / "old.png")
    (source_root / "Tokens" / "notes.txt").write_text("ignore", encoding="utf-8")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".png", ".jpg"}),
            ),
        ),
    )

    assert count_indexable_files(config) == 2


def test_count_and_index_source_ids_filter_only_target_source(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "tokens").mkdir(parents=True)
    (source_root / "maps").mkdir(parents=True)
    write_image(source_root / "tokens" / "Goblin.png")
    write_image(source_root / "maps" / "Dragon.jpg")

    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="tokens",
                display_name="Tokens",
                root=source_root / "tokens",
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="maps",
                display_name="Maps",
                root=source_root / "maps",
                priority=5,
                media_extensions=frozenset({".jpg"}),
            ),
        ),
    )

    assert count_indexable_files(config, source_ids=frozenset({"maps"})) == 1

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config, source_ids=frozenset({"maps"}))
        rows = db.execute("SELECT source_id FROM assets").fetchall()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert len(rows) == 1
    assert rows[0]["source_id"] == "maps"


def test_index_sources_replaces_missing_assets_for_source(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "dragon.webp"
    write_image(asset_path)
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".webp"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        asset_path.unlink()
        summary = index_sources(db, config)
        rows = db.execute("SELECT relative_path FROM assets").fetchall()

    assert summary.upserted_assets == 0
    assert rows == []


def test_index_sources_replaces_removed_config_sources_on_full_index(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    current_root = tmp_path / "current"
    old_root.mkdir()
    current_root.mkdir()
    write_image(old_root / "old-map.png")
    write_image(current_root / "current-map.png")
    initial_config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="old",
                display_name="Old",
                root=old_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="current",
                display_name="Current",
                root=current_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )
    updated_config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="current",
                display_name="Current",
                root=current_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, initial_config)
        summary = index_sources(db, updated_config)
        rows = db.execute(
            "SELECT source_id FROM assets ORDER BY source_id"
        ).fetchall()

    assert summary.scanned_files == 1
    assert [tuple(row) for row in rows] == [("current",)]


def test_index_sources_partial_index_preserves_unselected_sources(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    current_root = tmp_path / "current"
    old_root.mkdir()
    current_root.mkdir()
    write_image(old_root / "old-map.png")
    write_image(current_root / "current-map.png")
    initial_config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="old",
                display_name="Old",
                root=old_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
            SourceConfig(
                id="current",
                display_name="Current",
                root=current_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )
    partial_config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="current",
                display_name="Current",
                root=current_root,
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, initial_config)
        summary = index_sources(
            db,
            partial_config,
            source_ids=frozenset({"current"}),
        )
        rows = db.execute(
            "SELECT source_id, stale FROM assets ORDER BY source_id"
        ).fetchall()

    assert [tuple(row) for row in rows] == [("current", 0), ("old", 0)]


def test_index_sources_refreshes_asset_search_for_source(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.webp")
    write_image(source_root / "phoenix.webp")

    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".webp"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        index_sources(db, config)
        count_before = db.execute(
            "SELECT COUNT(*) FROM asset_search WHERE source_id = ?", ("dnd",)
        ).fetchone()[0]
        (source_root / "dragon.webp").unlink()
        index_sources(db, config)
        count_after = db.execute(
            "SELECT COUNT(*) FROM asset_search WHERE source_id = ?", ("dnd",)
        ).fetchone()[0]
        asset_count = db.execute(
            "SELECT COUNT(*) FROM assets WHERE source_id = ?", ("dnd",)
        ).fetchone()[0]

    assert count_before == 2
    assert count_after == 1
    assert asset_count == 1


def test_index_sources_skips_unchanged_thumbnail_generation(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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
        first = index_sources(db, config)
        second = index_sources(db, config)

    assert first.generated_thumbnails == 1
    assert first.upserted_assets == 1
    assert second.generated_thumbnails == 0
    assert second.upserted_assets == 0


def test_index_sources_skips_unchanged_video_poster_generation(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_video(source_root / "portal.mp4")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp4"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        first = index_sources(db, config, thumbnail_workers=1)
        second = index_sources(db, config, thumbnail_workers=1)

    assert first.generated_thumbnails == 1
    assert first.upserted_assets == 1
    assert second.generated_thumbnails == 0
    assert second.upserted_assets == 0


def test_index_sources_classifies_audio_files(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "thunder.mp3").write_bytes(b"audio")
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp3"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config)
        row = db.execute(
            "SELECT media_type, thumbnail_path FROM assets WHERE relative_path = 'thunder.mp3'"
        ).fetchone()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert row["media_type"] == "audio"
    assert row["thumbnail_path"] is None


def test_index_sources_updates_unchanged_audio_media_type(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    audio_path = source_root / "thunder.mp3"
    audio_path.write_bytes(b"audio")
    stat = audio_path.stat()
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=source_root,
                priority=10,
                media_extensions=frozenset({".mp3"}),
            ),
        ),
    )

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        db.execute(
            """
            INSERT INTO sources(id, display_name, root, priority)
            VALUES ('dnd', 'DND', ?, 10)
            """,
            (str(source_root),),
        )
        db.execute(
            """
            INSERT INTO assets(
              asset_id, source_id, relative_path, absolute_path, filename, stem,
              extension, directory, size_bytes, mtime_ns, media_type, stale
            )
            VALUES (
              'old-id', 'dnd', 'thunder.mp3', ?, 'thunder.mp3', 'thunder',
              '.mp3', '', ?, ?, 'other', 0
            )
            """,
            (str(audio_path), stat.st_size, stat.st_mtime_ns),
        )

        summary = index_sources(db, config)
        row = db.execute(
            "SELECT media_type, thumbnail_path FROM assets WHERE relative_path = 'thunder.mp3'"
        ).fetchone()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert row["media_type"] == "audio"
    assert row["thumbnail_path"] is None


def test_index_sources_reports_thumbnail_failures_without_aborting(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    broken_path = source_root / "broken.png"
    broken_path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
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
        summary = index_sources(db, config)
        row = db.execute(
            "SELECT relative_path, thumbnail_path FROM assets WHERE relative_path = 'broken.png'"
        ).fetchone()

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert len(summary.thumbnail_failures) == 1
    assert summary.thumbnail_failures[0].relative_path == "broken.png"
    assert "could not generate thumbnail" in summary.thumbnail_failures[0].message
    assert row["thumbnail_path"] is None


def test_index_sources_does_not_retry_unchanged_thumbnail_failures(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    broken_path = source_root / "broken.png"
    broken_path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
    write_image(source_root / "valid.png")
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
        first = index_sources(db, config, thumbnail_workers=2)
        second = index_sources(db, config, thumbnail_workers=2)
        retried = index_sources(
            db,
            config,
            thumbnail_workers=2,
            retry_failed_thumbnails=True,
        )
        rows = db.execute(
            """
            SELECT relative_path, thumbnail_path
            FROM assets
            ORDER BY relative_path
            """
        ).fetchall()

    assert first.scanned_files == 2
    assert first.upserted_assets == 2
    assert len(first.thumbnail_failures) == 1
    assert first.thumbnail_failures[0].relative_path == "broken.png"
    assert second.scanned_files == 2
    assert second.upserted_assets == 0
    assert second.thumbnail_failures == ()
    assert retried.scanned_files == 2
    assert retried.upserted_assets == 1
    assert len(retried.thumbnail_failures) == 1
    assert retried.thumbnail_failures[0].relative_path == "broken.png"
    assert rows[0]["relative_path"] == "broken.png"
    assert rows[0]["thumbnail_path"] is None
    assert rows[1]["relative_path"] == "valid.png"
    assert rows[1]["thumbnail_path"] is not None


def test_index_sources_records_non_thumbnail_errors_as_failures(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    def broken_thumbnail_for(*_: object, **__: object) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(indexer, "_thumbnail_for", broken_thumbnail_for)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(db, config)

    assert summary.scanned_files == 1
    assert summary.upserted_assets == 1
    assert len(summary.thumbnail_failures) == 1
    assert summary.thumbnail_failures[0].relative_path == "dragon.png"
    assert "boom" in summary.thumbnail_failures[0].message


def test_index_sources_marks_worker_timeout_as_failure(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    def slow_thumbnail(
        source_path: Path,
        source: object,
        *,
        data_root: Path,
        asset_id: str,
        relative_path: str,
        size_bytes: int,
        mtime_ns: int,
        media_type: str,
        ) -> thumbnails.ThumbnailResult:
        time.sleep(0.2)
        return thumbnails.generate_thumbnail(
            source_path,
            data_root,
            asset_id=asset_id,
            source_id="dnd",
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )

    monkeypatch.setattr(indexer, "_thumbnail_for", slow_thumbnail)

    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(
            db,
            config,
            thumbnail_workers=1,
            thumbnail_worker_timeout_seconds=0.1,
        )

        assert summary.scanned_files == 1
        assert summary.upserted_assets == 1
        assert len(summary.thumbnail_failures) == 1
        assert summary.thumbnail_failures[0].relative_path == "dragon.png"
        assert "timed out" in summary.thumbnail_failures[0].message.lower()

        no_retry = index_sources(
            db,
            config,
            thumbnail_workers=1,
            thumbnail_worker_timeout_seconds=0.1,
        )
        assert no_retry.scanned_files == 1
        assert no_retry.upserted_assets == 0
        assert len(no_retry.thumbnail_failures) == 0


def test_index_sources_disables_zero_second_timeout(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    original_generate_thumbnail = thumbnails.generate_thumbnail

    def slow_thumbnail(
        source_path: Path,
        source: object,
        *,
        data_root: Path,
        asset_id: str,
        relative_path: str,
        size_bytes: int,
        mtime_ns: int,
        media_type: str,
    ) -> thumbnails.ThumbnailResult:
        time.sleep(0.2)
        return original_generate_thumbnail(
            source_path,
            data_root,
            asset_id=asset_id,
            source_id="dnd",
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
        )

    monkeypatch.setattr(indexer, "_thumbnail_for", slow_thumbnail)

    start = time.monotonic()
    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(
            db,
            config,
            thumbnail_workers=1,
            thumbnail_worker_timeout_seconds=0.0,
        )
    elapsed = time.monotonic() - start

    assert summary.generated_thumbnails == 1
    assert len(summary.thumbnail_failures) == 0
    assert elapsed >= 0.2


def test_index_sources_continues_after_one_worker_times_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    fast_path = source_root / "fast.png"
    slow_path = source_root / "slow.png"
    write_image(fast_path)
    write_image(slow_path)
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

    call_count = {"calls": 0}

    original_generate_thumbnail = thumbnails.generate_thumbnail

    def slow_first_call(*args, **kwargs):  # type: ignore[override]
        call_count["calls"] += 1
        if call_count["calls"] == 1:
            time.sleep(1)
        return original_generate_thumbnail(*args, **kwargs)

    monkeypatch.setattr(indexer, "_thumbnail_for", slow_first_call)

    start = time.monotonic()
    with connect(tmp_path / "findme.sqlite3") as db:
        initialize_database(db)
        summary = index_sources(
            db,
            config,
            thumbnail_workers=2,
            thumbnail_worker_timeout_seconds=0.1,
        )
    elapsed = time.monotonic() - start

    assert elapsed < 0.8
    assert summary.scanned_files == 2
    assert summary.upserted_assets == 2
    assert summary.thumbnail_failures
