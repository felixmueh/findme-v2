from pathlib import Path

from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.indexer import index_sources


def test_index_sources_adds_media_files_and_ignores_legacy_findme_data(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    (source_root / "Tokens").mkdir(parents=True)
    (source_root / ".findme").mkdir()
    (source_root / "Tokens" / "Goblin Warrior.PNG").write_bytes(b"fake-png")
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


def test_index_sources_marks_missing_assets_stale(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "dragon.webp"
    asset_path.write_bytes(b"webp")
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
        stale = db.execute("SELECT stale FROM assets WHERE relative_path = 'dragon.webp'").fetchone()[0]

    assert summary.marked_stale == 1
    assert stale == 1
