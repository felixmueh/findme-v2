from __future__ import annotations

# Changes to this module can change the derived search corpus format or signals.
# Increment SEARCH_CORPUS_VERSION and rebuild the search corpus after modifying it.

from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from findme.config import AppConfig
from findme.search import _AssetRecord, _TantivyLexicalIndex, _normalize_text, _tokens


SEARCH_CORPUS_VERSION = "0.2"


_EMBEDDED_IMAGE_EXTENSION_RE = re.compile(
    r"(?i)\.(?:png|jpe?g|webp|gif)$",
)
_STEM_SEPARATOR_RE = re.compile(r"[\s._-]+")
_TRAILING_GLUED_NUMERIC_VARIANT_RE = re.compile(
    r"(?P<base>.+?)(?P<variant>\d+[A-Za-z]?)$"
)
_NUMERIC_VARIANT_RE = re.compile(r"\d+[A-Za-z]?")
_LETTER_NUMERIC_VARIANT_RE = re.compile(r"[A-Za-z]{1,2}\d+")
_LETTER_VARIANT_RE = re.compile(r"(?:[A-Za-z]|[A-Z]{2})")
_VARIANT_COLORS = frozenset(
    {
        "black",
        "blue",
        "brown",
        "cyan",
        "gold",
        "gray",
        "green",
        "grey",
        "magenta",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "white",
        "yellow",
    }
)


def search_corpus_path(data_root: Path, configured_path: Path | None = None) -> Path:
    if configured_path is not None:
        return configured_path
    version = SEARCH_CORPUS_VERSION.replace(".", "_")
    return data_root / f"search_corpus_{version}.sqlite3"


def search_corpus_lexical_path(
    data_root: Path,
    configured_path: Path | None = None,
) -> Path:
    return search_corpus_path(data_root, configured_path).with_suffix(".tantivy")


def rebuild_search_corpus(config: AppConfig, db_path: Path) -> Path:
    destination = search_corpus_path(config.data_root, config.search_corpus_path)
    lexical_destination = search_corpus_lexical_path(
        config.data_root,
        config.search_corpus_path,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
    temp_lexical_path = Path(
        tempfile.mkdtemp(
            prefix=f".{lexical_destination.name}.",
            suffix=".tmp",
            dir=lexical_destination.parent,
        )
    )

    try:
        with sqlite3.connect(db_path) as source_db:
            source_db.row_factory = sqlite3.Row
            assets = _fetch_preprocessed_assets(source_db)

        with sqlite3.connect(temp_path) as corpus_db:
            corpus_db.executescript(
                """
                CREATE TABLE corpus_metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE search_assets (
                  asset_index INTEGER PRIMARY KEY,
                  asset_id TEXT NOT NULL UNIQUE,
                  source_id TEXT NOT NULL,
                  source_display_name TEXT NOT NULL,
                  source_priority INTEGER NOT NULL,
                  relative_path TEXT NOT NULL,
                  filename TEXT NOT NULL,
                  stem TEXT NOT NULL,
                  extension TEXT NOT NULL,
                  directory TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  media_type TEXT NOT NULL,
                  thumbnail_path TEXT,
                  thumbnail_width INTEGER,
                  thumbnail_height INTEGER,
                  source_tokens TEXT NOT NULL,
                  filename_tokens TEXT NOT NULL,
                  directory_tokens TEXT NOT NULL,
                  path_tokens TEXT NOT NULL,
                  normalized_text TEXT NOT NULL,
                  fuzzy_text TEXT NOT NULL,
                  collection_key TEXT NOT NULL,
                  variant_sort_key TEXT NOT NULL,
                  has_variant INTEGER NOT NULL CHECK (has_variant IN (0, 1))
                );
                """
            )
            corpus_db.execute(
                "INSERT INTO corpus_metadata(key, value) VALUES ('version', ?)",
                (SEARCH_CORPUS_VERSION,),
            )
            corpus_db.executemany(
                """
                INSERT INTO search_assets(
                  asset_index, asset_id, source_id, source_display_name, source_priority,
                  relative_path, filename, stem, extension, directory, size_bytes, mtime_ns,
                  media_type, thumbnail_path, thumbnail_width, thumbnail_height,
                  source_tokens, filename_tokens, directory_tokens, path_tokens,
                  normalized_text, fuzzy_text, collection_key, variant_sort_key, has_variant
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_asset_row(asset) for asset in assets],
            )

        _TantivyLexicalIndex.build(assets, path=temp_lexical_path)
        temp_path.replace(destination)
        _replace_directory(temp_lexical_path, lexical_destination)
        return destination
    except Exception:
        temp_path.unlink(missing_ok=True)
        shutil.rmtree(temp_lexical_path, ignore_errors=True)
        raise


def _asset_row(asset) -> tuple[object, ...]:
    return (
        asset.asset_index,
        asset.asset_id,
        asset.source_id,
        asset.source_display_name,
        asset.source_priority,
        asset.relative_path,
        asset.filename,
        asset.stem,
        asset.extension,
        asset.directory,
        asset.size_bytes,
        asset.mtime_ns,
        asset.media_type,
        asset.thumbnail_path,
        asset.thumbnail_width,
        asset.thumbnail_height,
        " ".join(asset.source_tokens),
        " ".join(asset.filename_tokens),
        " ".join(asset.directory_tokens),
        " ".join(asset.path_tokens),
        asset.normalized_text,
        asset.fuzzy_text,
        asset.collection_key,
        asset.variant_sort_key,
        int(asset.has_variant),
    )


def _fetch_preprocessed_assets(db: sqlite3.Connection) -> tuple[_AssetRecord, ...]:
    rows = db.execute(
        """
        SELECT
          assets.asset_id,
          assets.source_id,
          sources.display_name AS source_display_name,
          sources.priority AS source_priority,
          assets.relative_path,
          assets.filename,
          assets.stem,
          assets.extension,
          assets.directory,
          assets.size_bytes,
          assets.mtime_ns,
          assets.media_type,
          assets.thumbnail_path,
          assets.thumbnail_width,
          assets.thumbnail_height
        FROM assets
        JOIN sources ON sources.id = assets.source_id
        WHERE assets.stale = 0
        ORDER BY sources.priority DESC, assets.relative_path COLLATE NOCASE
        """
    ).fetchall()
    return tuple(_row_to_preprocessed_asset(row, index) for index, row in enumerate(rows))


def _row_to_preprocessed_asset(row: sqlite3.Row, asset_index: int) -> _AssetRecord:
    source_tokens = _tokens(f"{row['source_id']} {row['source_display_name']}")
    filename_tokens = _tokens(row["filename"])
    directory_tokens = _tokens(row["directory"])
    path_tokens = _tokens(row["relative_path"])
    normalized_text = _normalize_text(
        " ".join((row["filename"], row["stem"], row["directory"], row["relative_path"]))
    )
    fuzzy_text = " ".join(filename_tokens + path_tokens)
    collection_key, variant_sort_key, has_variant = _card_metadata_parts(
        source_id=row["source_id"],
        directory=row["directory"],
        stem=row["stem"],
        relative_path=row["relative_path"],
    )
    return _AssetRecord(
        asset_index=asset_index,
        asset_id=row["asset_id"],
        source_id=row["source_id"],
        source_display_name=row["source_display_name"],
        source_priority=row["source_priority"],
        relative_path=row["relative_path"],
        filename=row["filename"],
        stem=row["stem"],
        extension=row["extension"],
        directory=row["directory"],
        size_bytes=row["size_bytes"],
        mtime_ns=row["mtime_ns"],
        media_type=row["media_type"],
        thumbnail_path=row["thumbnail_path"],
        thumbnail_width=row["thumbnail_width"],
        thumbnail_height=row["thumbnail_height"],
        source_tokens=source_tokens,
        filename_tokens=filename_tokens,
        directory_tokens=directory_tokens,
        path_tokens=path_tokens,
        normalized_text=normalized_text,
        fuzzy_text=fuzzy_text,
        collection_key=collection_key,
        variant_sort_key=variant_sort_key,
        has_variant=has_variant,
    )


def _card_metadata_parts(
    *,
    source_id: str,
    directory: str,
    stem: str,
    relative_path: str,
) -> tuple[str, str, bool]:
    collection_stem = _collection_stem(stem)
    base, variant_sort_key, has_variant = _variant_parts(collection_stem)
    collection_key = "\x00".join((source_id, directory, base.casefold()))
    return collection_key, _encode_variant_sort_key(variant_sort_key, relative_path), has_variant


def _variant_parts(stem: str) -> tuple[str, tuple[tuple[int, int, str], ...], bool]:
    base_tokens: list[str] = []
    variant_sort_keys: list[tuple[int, int, str]] = []

    for raw_token in _STEM_SEPARATOR_RE.split(stem):
        if not raw_token:
            continue
        token_parts = _token_parts(raw_token)
        for token, variant in token_parts:
            if variant:
                variant_sort_keys.append(_variant_sort_key(token))
            else:
                base_tokens.append(token)

    if not variant_sort_keys:
        return stem, (), False
    if not base_tokens:
        return stem, (), False
    return " ".join(base_tokens), tuple(variant_sort_keys), True


def _token_parts(raw_token: str) -> list[tuple[str, bool]]:
    token = raw_token.strip("()[]{}")
    if _is_variant_token(token):
        return [(token, True)]
    match = _TRAILING_GLUED_NUMERIC_VARIANT_RE.match(token)
    if match and not _is_variant_token(token):
        base = match.group("base")
        variant = match.group("variant")
        if base and _is_variant_token(variant):
            return [(base, False), (variant, True)]
    return [(token, False)]


def _is_variant_token(token: str) -> bool:
    if not token:
        return False
    if token.casefold() in _VARIANT_COLORS:
        return True
    return (
        _NUMERIC_VARIANT_RE.fullmatch(token) is not None
        or _LETTER_NUMERIC_VARIANT_RE.fullmatch(token) is not None
        or _LETTER_VARIANT_RE.fullmatch(token) is not None
    )


def _variant_sort_key(variant: str) -> tuple[int, int, str]:
    numeric = re.fullmatch(r"(\d+)([A-Za-z]?)", variant)
    if numeric is not None:
        return (1, int(numeric.group(1)), numeric.group(2).casefold())
    letter_numeric = re.fullmatch(r"([A-Za-z]{1,2})(\d+)", variant)
    if letter_numeric is not None:
        return (2, int(letter_numeric.group(2)), letter_numeric.group(1).casefold())
    if variant.casefold() in _VARIANT_COLORS:
        return (3, 0, variant.casefold())
    return (4, 0, variant.casefold())


def _encode_variant_sort_key(
    variant_sort_key: tuple[tuple[int, int, str], ...],
    relative_path: str,
) -> str:
    encoded_parts = "".join(
        f"{kind}:{len(str(number)):04d}:{number}:{suffix}\x1e"
        for kind, number, suffix in variant_sort_key
    )
    return f"{encoded_parts}\x1f{relative_path.casefold()}"


def _collection_stem(stem: str) -> str:
    return _EMBEDDED_IMAGE_EXTENSION_RE.sub("", stem)


def _replace_directory(source: Path, destination: Path) -> None:
    old_destination = destination.with_name(f".{destination.name}.old")
    if old_destination.exists():
        shutil.rmtree(old_destination)
    if destination.exists():
        destination.rename(old_destination)
    source.rename(destination)
    shutil.rmtree(old_destination, ignore_errors=True)
