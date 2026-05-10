from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib


DEFAULT_MEDIA_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".webm", ".mp4"}
)


class ConfigError(ValueError):
    """Raised when server-owned configuration is invalid."""


@dataclass(frozen=True)
class OutputProfile:
    id: str
    label: str
    prefix: str


@dataclass(frozen=True)
class SourceConfig:
    id: str
    display_name: str
    root: Path
    priority: int
    media_extensions: frozenset[str] = DEFAULT_MEDIA_EXTENSIONS
    primary_output: str | None = None
    output_profiles: tuple[OutputProfile, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    data_root: Path
    sources: tuple[SourceConfig, ...]
    thumbnail_workers: int = 2
    domain_vocabulary_path: Path | None = None
    search_corpus_path: Path | None = None


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def load_config(path: Path) -> AppConfig:
    config_path = path
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    app = raw.get("app")
    if not isinstance(app, dict):
        raise ConfigError("missing [app] section")
    data_root_raw = app.get("data_root")
    if not isinstance(data_root_raw, str) or not data_root_raw:
        raise ConfigError("[app].data_root must be a non-empty string")
    thumbnail_workers = app.get("thumbnail_workers", 2)
    if not isinstance(thumbnail_workers, int) or thumbnail_workers < 1:
        raise ConfigError("[app].thumbnail_workers must be a positive integer")

    search_config = raw.get("search")
    domain_vocabulary_path = _load_domain_vocabulary_path(search_config, config_path.parent)
    search_corpus_path = _load_search_corpus_path(search_config, config_path.parent)

    source_tables = raw.get("sources")
    if not isinstance(source_tables, list) or not source_tables:
        raise ConfigError("at least one [[sources]] entry is required")

    seen_ids: set[str] = set()
    sources = []
    for table in source_tables:
        if not isinstance(table, dict):
            raise ConfigError("[[sources]] entries must be tables")
        source = _load_source(table)
        if source.id in seen_ids:
            raise ConfigError(f"duplicate source id: {source.id}")
        seen_ids.add(source.id)
        sources.append(source)

    return AppConfig(
        data_root=Path(data_root_raw).expanduser(),
        sources=tuple(sources),
        thumbnail_workers=thumbnail_workers,
        domain_vocabulary_path=domain_vocabulary_path,
        search_corpus_path=search_corpus_path,
    )


def _load_domain_vocabulary_path(raw: object, config_dir: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[search] must be a table")
    vocabulary = raw.get("domain_vocabulary")
    if vocabulary is None:
        return None
    if not isinstance(vocabulary, str) or not vocabulary:
        raise ConfigError("[search].domain_vocabulary must be a non-empty string")
    path = Path(vocabulary).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    if not path.exists():
        raise ConfigError(f"domain vocabulary file does not exist: {path}")
    if not path.is_file():
        raise ConfigError(f"domain vocabulary path is not a file: {path}")
    return path


def _load_search_corpus_path(raw: object, config_dir: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("[search] must be a table")
    corpus_path = raw.get("corpus_path")
    if corpus_path is None:
        return None
    if not isinstance(corpus_path, str) or not corpus_path:
        raise ConfigError("[search].corpus_path must be a non-empty string")
    path = Path(corpus_path).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path


def _load_source(table: dict[str, object]) -> SourceConfig:
    source_id = _required_string(table, "id", "source id")
    if not _ID_RE.match(source_id):
        raise ConfigError(f"invalid source id: {source_id}")

    display_name = _required_string(table, "display_name", f"source {source_id} display_name")
    root = Path(_required_string(table, "root", f"source {source_id} root")).expanduser()
    if not root.exists():
        raise ConfigError(f"source root does not exist for {source_id}: {root}")
    if not root.is_dir():
        raise ConfigError(f"source root is not a directory for {source_id}: {root}")

    priority = table.get("priority", 0)
    if not isinstance(priority, int):
        raise ConfigError(f"source {source_id} priority must be an integer")

    extensions = _load_extensions(table.get("media_extensions", None), source_id)
    profiles = _load_output_profiles(table.get("output_profiles", []), source_id)
    primary_output = table.get("primary_output")
    if primary_output is not None and not isinstance(primary_output, str):
        raise ConfigError(f"source {source_id} primary_output must be a string")

    return SourceConfig(
        id=source_id,
        display_name=display_name,
        root=root.resolve(),
        priority=priority,
        media_extensions=extensions,
        primary_output=primary_output,
        output_profiles=profiles,
    )


def _required_string(table: dict[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _load_extensions(raw: object, source_id: str) -> frozenset[str]:
    if raw is None:
        return DEFAULT_MEDIA_EXTENSIONS
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"source {source_id} media_extensions must be a non-empty list")

    extensions = set()
    for value in raw:
        if not isinstance(value, str) or not value:
            raise ConfigError(f"source {source_id} media_extensions entries must be strings")
        extension = value.lower()
        if not extension.startswith("."):
            extension = f".{extension}"
        extensions.add(extension)
    return frozenset(extensions)


def _load_output_profiles(raw: object, source_id: str) -> tuple[OutputProfile, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"source {source_id} output_profiles must be a list")

    profiles = []
    seen_ids: set[str] = set()
    for table in raw:
        if not isinstance(table, dict):
            raise ConfigError(f"source {source_id} output profile must be a table")
        profile_id = _required_string(table, "id", f"source {source_id} output profile id")
        if profile_id in seen_ids:
            raise ConfigError(f"duplicate output profile id for {source_id}: {profile_id}")
        seen_ids.add(profile_id)
        profiles.append(
            OutputProfile(
                id=profile_id,
                label=_required_string(table, "label", f"source {source_id} output profile label"),
                prefix=_required_string(table, "prefix", f"source {source_id} output profile prefix").strip("/"),
            )
        )
    return tuple(profiles)
