from pathlib import Path

from PIL import Image

from findme.cli import run
from findme.db import connect
from findme.search_corpus import load_search_corpus_snapshot, search_corpus_snapshot_path

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
    assert search_corpus_snapshot_path(data_root).exists()


def test_cli_rebuild_search_corpus_without_reindexing_assets(tmp_path: Path) -> None:
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
    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0

    write_image(source_root / "not-indexed-yet.webp")
    assert run(["rebuild-search-corpus", "--config", str(config_path)]) == 0

    snapshot = load_search_corpus_snapshot(search_corpus_snapshot_path(data_root))
    assets = sorted(snapshot.assets, key=lambda asset: asset.relative_path)
    paths = [asset.relative_path for asset in assets]
    assert paths == ["dragon.webp"]
    assert assets[0].filename_tokens == ("dragon", "webp")
    metadata = snapshot.card_metadata_by_asset_id()[assets[0].asset_id]
    assert metadata.collection_key == "dnd\x00\x00dragon"
    assert metadata.variant_sort_key == "\x1fdragon.webp"
    assert metadata.has_variant is False


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


def test_cli_index_writes_phase_logs_to_stdout(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "ghost.png")
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

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-workers",
                "1",
            ]
        )
        == 0
    )

    lines = capsys.readouterr().out.splitlines()
    assert any("counting indexable files started" in line for line in lines)
    assert any("counting indexable files finished: files=1" in line for line in lines)
    assert any("index started: files=1 thumbnail_workers=1" in line for line in lines)
    assert any("index progress: scanned=1/1" in line for line in lines)
    assert any("asset indexing finished: scanned=1 upserted=1" in line for line in lines)
    assert any("search preprocessing started" in line for line in lines)
    assert any("search preprocessing loaded assets: count=1" in line for line in lines)
    assert any("search lexical index build started: assets=1" in line for line in lines)
    assert any("search preprocessing finished:" in line for line in lines)


def test_cli_index_verbose_logs_each_file_action(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "ghost.png")
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

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-workers",
                "1",
                "-v",
            ]
        )
        == 0
    )
    first_lines = capsys.readouterr().out.splitlines()
    assert any(
        "index file queued: source=dnd path=ghost.png action=thumbnail media=image" in line
        for line in first_lines
    )
    assert any(
        "index file done: source=dnd path=ghost.png action=upserted thumbnail=generated"
        in line
        for line in first_lines
    )

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-workers",
                "1",
                "-v",
            ]
        )
        == 0
    )
    second_lines = capsys.readouterr().out.splitlines()
    assert any(
        "index file done: source=dnd path=ghost.png action=unchanged thumbnail=cached"
        in line
        for line in second_lines
    )


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


def test_cli_index_accepts_thumbnail_worker_timeout_arg(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-worker-timeout-seconds",
                "0.1",
                "--thumbnail-workers",
                "1",
            ]
        )
        == 0
    )


def test_cli_index_disables_thumbnail_worker_timeout_with_zero(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-worker-timeout-seconds",
                "0",
                "--thumbnail-workers",
                "1",
            ]
        )
        == 0
    )


def test_cli_index_rejects_negative_thumbnail_worker_timeout_arg(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    write_image(source_root / "dragon.png")
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

    assert (
        run(
            [
                "index",
                "--config",
                str(config_path),
                "--thumbnail-worker-timeout-seconds",
                "-1",
                "--thumbnail-workers",
                "1",
            ]
        )
        == 2
    )


def test_cli_index_can_retry_failed_thumbnails(tmp_path: Path, capsys) -> None:
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

    base_command = [
        "index",
        "--config",
        str(config_path),
        "--thumbnail-workers",
        "1",
    ]
    assert run(base_command) == 0
    capsys.readouterr()

    assert run(base_command) == 0
    skipped = capsys.readouterr()
    assert "failed 0 thumbnails" in skipped.out

    assert run([*base_command, "--retry-failed-thumbnails"]) == 0
    retried = capsys.readouterr()
    assert "failed 1 thumbnails" in retried.out
    assert "thumbnail failed: dnd:broken.png" in retried.err


def test_cli_cleanup_dry_run_preserves_stale_rows_and_unreferenced_cache(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "ghost.png"
    write_image(asset_path)
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
    asset_path.unlink()
    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0
    orphan = data_root / "cache" / "thumbs" / "zz" / "orphan.webp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")

    assert run(["cleanup", "--config", str(config_path), "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "would clean 1 stale assets, 0 stale search rows, 2 unreferenced cache files" in captured.out
    assert orphan.is_file()
    with connect(data_root / "findme.sqlite3") as db:
        stale_assets = db.execute("SELECT COUNT(*) FROM assets WHERE stale = 1").fetchone()[0]
    assert stale_assets == 1


def test_cli_cleanup_removes_stale_rows_and_unreferenced_cache(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    asset_path = source_root / "ghost.png"
    write_image(asset_path)
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
    asset_path.unlink()
    assert run(["index", "--config", str(config_path), "--thumbnail-workers", "1"]) == 0
    orphan = data_root / "cache" / "thumbs" / "zz" / "orphan.webp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")

    assert run(["cleanup", "--config", str(config_path)]) == 0

    captured = capsys.readouterr()
    assert "cleaned 1 stale assets, 0 stale search rows, 2 unreferenced cache files" in captured.out
    assert not orphan.exists()
    assert not (data_root / "cache" / "thumbs" / "zz").exists()
    with connect(data_root / "findme.sqlite3") as db:
        assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        search_rows = db.execute("SELECT COUNT(*) FROM asset_search").fetchone()[0]
    assert assets == 0
    assert search_rows == 0


def test_cli_search_eval_runs_strict_scope(tmp_path: Path, capsys) -> None:
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
      - scope: strict
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
                "strict",
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
