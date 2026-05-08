from pathlib import Path

from PIL import Image

from findme.cli import run

from helpers import write_image


def test_cli_init_db_and_index(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.webp")
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


def test_cli_index_can_rebuild_thumbnail_cache(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "ghost.png", alpha=0)
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
media_extensions = [".png"]
""",
        encoding="utf-8",
    )

    assert run(["index", "--config", str(config_path)]) == 0
    thumbnails = list((data_root / "cache" / "thumbs").rglob("*.webp"))
    assert thumbnails
    thumbnails[0].write_bytes(b"stale")

    assert run(["index", "--config", str(config_path), "--rebuild-thumbnails"]) == 0

    with Image.open(thumbnails[0]) as thumb:
        assert thumb.mode == "RGBA"


def test_cli_index_accepts_thumbnail_workers_and_reports_progress(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "ghost.png")
    config_path = tmp_path / "findme.toml"
    data_root = tmp_path / "data"
    config_path.write_text(
        f"""
[app]
data_root = "{data_root}"
thumbnail_workers = 2

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
media_extensions = [".png"]
""",
        encoding="utf-8",
    )

    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0

    captured = capsys.readouterr()
    assert "thumbs/s=" in captured.err
    assert "100%" in captured.err
    assert "1/1" in captured.err
    assert "generated 1 thumbnails" in captured.out
