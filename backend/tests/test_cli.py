from pathlib import Path

from findme.cli import run


def test_cli_init_db_and_index(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "dragon.webp").write_bytes(b"webp")
    config_path = tmp_path / "findme.toml"
    data_root = tmp_path / "data"
    config_path.write_text(
        f"""
[app]
data_root = "{data_root}"

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
media_extensions = [".webp"]
""",
        encoding="utf-8",
    )

    assert run(["init-db", "--config", str(config_path)]) == 0
    assert (data_root / "findme.sqlite3").exists()
    assert run(["index", "--config", str(config_path)]) == 0
