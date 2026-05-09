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


def test_cli_index_reports_thumbnail_failures(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    (source_root / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
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

    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0

    captured = capsys.readouterr()
    assert "failed 1 thumbnails" in captured.out
    assert "thumbnail failed: dnd:broken.png" in captured.err


def test_cli_search_eval_runs_combined_scope(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "goblin_warrior.png")
    config_path = tmp_path / "findme.toml"
    data_root = tmp_path / "data"
    config_path.write_text(
        f"""
[app]
data_root = "{data_root}"

[[sources]]
id = "tokens"
display_name = "Tokens"
root = "{source_root}"
priority = 10
media_extensions = [".png"]
""",
        encoding="utf-8",
    )
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  goblin_warriors:
    assets:
      - source_id: tokens
        relative_path: goblin_warrior.png
queries:
  - id: goblin-warrior
    text: goblin warrior
    expectations:
      - scope: combined
        assert: findable
        target: goblin_warriors
        importance: 1.0
        gate: true
""",
        encoding="utf-8",
    )

    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0
    assert (
        run(
            [
                "search-eval",
                "--config",
                str(config_path),
                "--eval",
                str(eval_path),
                "--scope",
                "combined",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "search evaluation: 1/1 expectations passed" in captured.out
    assert "weighted score: 1.000" in captured.out


def test_cli_search_eval_runs_strict_fuzzy_and_domain_scopes(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "goblin_warrior.png")
    write_image(source_root / "human_mage.png")
    vocabulary_path = tmp_path / "vocabulary.yaml"
    vocabulary_path.write_text(
        """
version: 1
expansions:
  hero:
    - mage
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "findme.toml"
    data_root = tmp_path / "data"
    config_path.write_text(
        f"""
[app]
data_root = "{data_root}"

[search]
domain_vocabulary = "{vocabulary_path}"

[[sources]]
id = "tokens"
display_name = "Tokens"
root = "{source_root}"
priority = 10
media_extensions = [".png"]
""",
        encoding="utf-8",
    )
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  goblin_warriors:
    assets:
      - source_id: tokens
        relative_path: goblin_warrior.png
  mages:
    assets:
      - source_id: tokens
        relative_path: human_mage.png
queries:
  - id: goblin-warrior
    text: goblin warrior
    expectations:
      - scope: strict
        assert: findable
        target: goblin_warriors
        importance: 1.0
        gate: true
  - id: goblin-warrior-typo
    text: goblin warror
    expectations:
      - scope: fuzzy
        assert: findable
        target: goblin_warriors
        importance: 1.0
        gate: true
  - id: hero
    text: hero
    expectations:
      - scope: domain
        assert: findable
        target: mages
        importance: 1.0
        gate: true
""",
        encoding="utf-8",
    )

    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0
    assert (
        run(
            [
                "search-eval",
                "--config",
                str(config_path),
                "--eval",
                str(eval_path),
                "--scope",
                "strict",
                "--scope",
                "fuzzy",
                "--scope",
                "domain",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "search evaluation: 3/3 expectations passed" in captured.out
