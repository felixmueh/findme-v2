from pathlib import Path

import pytest

from findme.config import ConfigError, load_config


def test_load_config_requires_existing_source_root(tmp_path: Path) -> None:
    config_path = tmp_path / "findme.toml"
    missing_root = tmp_path / "missing"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"

[[sources]]
id = "dnd"
display_name = "DND"
root = "{missing_root}"
priority = 10
media_types = ["image"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="source root does not exist"):
        load_config(config_path)


def test_load_config_normalizes_source_roots_and_media_extensions(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_path = tmp_path / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
media_extensions = [".PNG", "jpg"]
primary_output = "foundry"

[[sources.output_profiles]]
id = "foundry"
label = "Foundry"
prefix = "library/DND"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.data_root == tmp_path / "data"
    assert config.thumbnail_workers == 2
    assert config.sources[0].root == source_root.resolve()
    assert config.sources[0].media_extensions == frozenset({".png", ".jpg"})
    assert config.sources[0].output_profiles[0].prefix == "library/DND"


def test_load_config_reads_thumbnail_worker_count(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_path = tmp_path / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"
thumbnail_workers = 4

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.thumbnail_workers == 4


def test_load_config_reads_additional_thumbnail_mipmaps(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_path = tmp_path / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"
additional_thumbnail_mipmaps = [1024, 512, 512]

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.additional_thumbnail_mipmaps == (512, 1024)


def test_load_config_rejects_baseline_thumbnail_mipmap_in_additional_sizes(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_path = tmp_path / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"
additional_thumbnail_mipmaps = [256]

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="greater than 256"):
        load_config(config_path)


def test_load_config_reads_domain_vocabulary_path_relative_to_config(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    vocabulary_path = config_dir / "search-vocabulary.yaml"
    vocabulary_path.write_text("version: 1\nexpansions: {}\n", encoding="utf-8")
    config_path = config_dir / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"

[search]
domain_vocabulary = "search-vocabulary.yaml"

[[sources]]
id = "dnd"
display_name = "DND"
root = "{source_root}"
priority = 10
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.domain_vocabulary_path == vocabulary_path


def test_load_config_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    source_root = tmp_path / "assets"
    source_root.mkdir()
    config_path = tmp_path / "findme.toml"
    config_path.write_text(
        f"""
[app]
data_root = "{tmp_path / "data"}"

[[sources]]
id = "dnd"
display_name = "DND A"
root = "{source_root}"
priority = 10

[[sources]]
id = "dnd"
display_name = "DND B"
root = "{source_root}"
priority = 20
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate source id"):
        load_config(config_path)
