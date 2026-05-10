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
        profiles = {
            row[0]: row[1]
            for row in db.execute("SELECT id, kind FROM search_profiles").fetchall()
        }

    assert journal_mode == "wal"
    assert {
        "sources",
        "assets",
        "search_profiles",
        "search_profile_folder_settings",
        "schema_migrations",
    } <= tables

    assert profiles == {"none": "none", "default": "default"}
