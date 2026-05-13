from pathlib import Path

from findme.db import connect, initialize_database


def test_initialize_database_creates_schema_and_wal_database(tmp_path: Path) -> None:
    db_path = tmp_path / "findme.sqlite3"

    with connect(db_path) as db:
        initialize_database(db)
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        asset_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(assets)").fetchall()
        }
        profiles = {
            row[0]: row[1]
            for row in db.execute("SELECT id, kind FROM search_profiles").fetchall()
        }

    assert journal_mode == "wal"
    assert {
        "sources",
        "assets",
        "asset_thumbnail_variants",
        "search_profiles",
        "search_profile_folder_settings",
        "schema_migrations",
    } <= tables
    assert "asset_search" not in tables
    assert "stale" not in asset_columns

    assert profiles == {"none": "none", "default": "default"}


def test_initialize_database_does_not_rebuild_current_directory_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "findme.sqlite3"

    with connect(db_path) as db:
        initialize_database(db)
        statements: list[str] = []
        db.set_trace_callback(statements.append)

        initialize_database(db)

    assert not any("DROP INDEX IF EXISTS idx_assets_directory" in statement for statement in statements)
