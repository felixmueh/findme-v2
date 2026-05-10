from __future__ import annotations

# Changes to this module can change the derived search corpus format or signals.
# Increment SEARCH_CORPUS_VERSION and rebuild the search corpus after modifying it.

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import tempfile

import tantivy

from findme.config import AppConfig
from findme.result_cards import CardMetadata


SEARCH_CORPUS_VERSION = "0.4"


class SearchCorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchCorpusAsset:
    asset_index: int
    asset_id: str
    source_id: str
    source_display_name: str
    source_priority: int
    relative_path: str
    filename: str
    stem: str
    extension: str
    directory: str
    size_bytes: int
    mtime_ns: int
    media_type: str
    thumbnail_path: str | None
    thumbnail_width: int | None
    thumbnail_height: int | None
    source_tokens: tuple[str, ...]
    filename_tokens: tuple[str, ...]
    directory_tokens: tuple[str, ...]
    path_tokens: tuple[str, ...]
    normalized_text: str
    fuzzy_text: str
    collection_key: str = ""
    variant_sort_key: str = ""
    has_variant: bool = False


@dataclass(frozen=True)
class SearchCorpus:
    assets: tuple[SearchCorpusAsset, ...]
    fuzzy_tokens: tuple[str, ...]
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...]
    lexical_index: "SearchLexicalIndex | None" = None

    @classmethod
    def from_assets(cls, assets: Iterable[SearchCorpusAsset]) -> "SearchCorpus":
        asset_tuple = tuple(assets)
        return cls.from_preprocessed_assets(
            asset_tuple,
            lexical_index=SearchLexicalIndex.build(asset_tuple),
        )

    @classmethod
    def from_preprocessed_assets(
        cls,
        assets: Iterable[SearchCorpusAsset],
        *,
        lexical_index: "SearchLexicalIndex | None",
    ) -> "SearchCorpus":
        asset_tuple = tuple(assets)
        fuzzy_token_assets: dict[str, list[int]] = {}
        for asset_index, asset in enumerate(asset_tuple):
            for token in dict.fromkeys(asset.filename_tokens + asset.path_tokens):
                fuzzy_token_assets.setdefault(token, []).append(asset_index)
        return cls(
            assets=asset_tuple,
            fuzzy_tokens=tuple(fuzzy_token_assets),
            fuzzy_token_asset_indexes=tuple(
                tuple(asset_indexes) for asset_indexes in fuzzy_token_assets.values()
            ),
            lexical_index=lexical_index,
        )


@dataclass(frozen=True)
class SearchCorpusSnapshot:
    version: str
    assets: tuple[SearchCorpusAsset, ...]
    directory_counts: dict[tuple[str, str], int]
    source_display_names: dict[str, str]
    source_sort_keys: dict[str, tuple[int, str, str]]
    fuzzy_tokens: tuple[str, ...]
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...]

    def card_metadata_by_asset_id(self) -> dict[str, CardMetadata]:
        return card_metadata_by_asset_id(self.assets)


class SearchLexicalIndex:
    _FIELDS = ("stem", "filename", "path", "directory", "source")
    _TRIGRAM_FIELDS = (
        "filename_trigrams",
        "path_trigrams",
        "directory_trigrams",
        "source_trigrams",
    )
    _EXACT_WEIGHTS = {
        "stem": 90.0,
        "filename": 50.0,
        "path": 12.0,
        "directory": 8.0,
        "source": 22.0,
    }
    _PREFIX_WEIGHTS = {
        "stem": 58.0,
        "filename": 38.0,
        "path": 8.0,
        "directory": 5.0,
        "source": 16.0,
    }
    _INFIX_WEIGHTS = {
        "filename_trigrams": 1.5,
        "path_trigrams": 0.75,
        "directory_trigrams": 0.4,
        "source_trigrams": 0.5,
    }

    def __init__(
        self,
        index: tantivy.Index,
        searcher: tantivy.Searcher,
        asset_count: int,
    ) -> None:
        self.index = index
        self.schema = index.schema
        self.searcher = searcher
        self.asset_count = asset_count

    @classmethod
    def build(
        cls,
        assets: tuple[SearchCorpusAsset, ...],
        *,
        path: Path | None = None,
    ) -> "SearchLexicalIndex | None":
        if not assets:
            return None
        schema_builder = tantivy.SchemaBuilder()
        schema_builder.add_integer_field("asset_index", stored=True, fast=True)
        for field_name in cls._FIELDS + cls._TRIGRAM_FIELDS:
            schema_builder.add_text_field(field_name)
        schema = schema_builder.build()
        index = tantivy.Index(schema, path=str(path) if path is not None else None)
        writer = index.writer(heap_size=300_000_000, num_threads=2)
        for asset_index, asset in enumerate(assets):
            writer.add_document(
                tantivy.Document(
                    asset_index=asset_index,
                    stem=" ".join(tokenize_search_text(asset.stem)),
                    filename=" ".join(asset.filename_tokens),
                    path=" ".join(asset.path_tokens),
                    directory=" ".join(asset.directory_tokens),
                    source=" ".join(asset.source_tokens),
                    filename_trigrams=_trigram_text(asset.filename_tokens),
                    path_trigrams=_trigram_text(asset.path_tokens),
                    directory_trigrams=_trigram_text(asset.directory_tokens),
                    source_trigrams=_trigram_text(asset.source_tokens),
                )
            )
        writer.commit()
        index.reload()
        return cls(index=index, searcher=index.searcher(), asset_count=len(assets))

    @classmethod
    def open(cls, path: Path, *, asset_count: int) -> "SearchLexicalIndex":
        index = tantivy.Index.open(str(path))
        index.reload()
        return cls(index=index, searcher=index.searcher(), asset_count=asset_count)

    def candidates_with_scores(
        self,
        query_tokens: tuple[str, ...],
    ) -> tuple[tuple[int, float], ...]:
        matches, _total = self.search_with_scores(
            query_tokens,
            require_all_terms=False,
            limit=self.asset_count,
            offset=0,
        )
        return matches

    def search_with_scores(
        self,
        query_tokens: tuple[str, ...],
        *,
        require_all_terms: bool,
        limit: int,
        offset: int,
    ) -> tuple[tuple[tuple[int, float], ...], int]:
        query = self._candidate_query(
            query_tokens,
            require_all_terms=require_all_terms,
        )
        if query is None:
            return (), 0
        result = self.searcher.search(query, limit=limit, offset=offset, count=True)
        doc_addresses = [doc_address for _score, doc_address in result.hits]
        asset_indexes = self.searcher.fast_field_values("asset_index", doc_addresses)
        return (
            tuple(
                (asset_index, float(score))
                for (score, _doc_address), asset_index in zip(
                    result.hits,
                    asset_indexes,
                    strict=True,
                )
            ),
            result.count,
        )

    def candidate_indexes(self, query_tokens: tuple[str, ...]) -> tuple[int, ...]:
        return tuple(
            asset_index for asset_index, _score in self.candidates_with_scores(query_tokens)
        )

    def _candidate_query(
        self,
        query_tokens: tuple[str, ...],
        *,
        require_all_terms: bool = False,
    ) -> tantivy.Query | None:
        subqueries = [
            (
                tantivy.Occur.Must if require_all_terms else tantivy.Occur.Should,
                token_query,
            )
            for token in query_tokens
            if (token_query := self._token_query(token)) is not None
        ]
        if not subqueries:
            return None
        return tantivy.Query.boolean_query(subqueries)

    def _token_query(self, token: str) -> tantivy.Query | None:
        subqueries: list[tuple[tantivy.Occur, tantivy.Query]] = []
        for field_name in self._FIELDS:
            subqueries.append(
                (
                    tantivy.Occur.Should,
                    tantivy.Query.boost_query(
                        tantivy.Query.term_query(self.schema, field_name, token),
                        self._EXACT_WEIGHTS[field_name],
                    ),
                )
            )
            prefix_terms = [
                term
                for term, _doc_frequency in self.searcher.terms_with_prefix(
                    field_name,
                    token,
                    limit=None,
                )
                if term != token
            ]
            if prefix_terms:
                subqueries.append(
                    (
                        tantivy.Occur.Should,
                        tantivy.Query.boost_query(
                            tantivy.Query.term_set_query(
                                self.schema,
                                field_name,
                                prefix_terms,
                            ),
                            self._PREFIX_WEIGHTS[field_name],
                        ),
                    )
                )

        trigrams = _trigrams(token)
        if trigrams:
            for trigram_field in self._TRIGRAM_FIELDS:
                subqueries.append(
                    (
                        tantivy.Occur.Should,
                        tantivy.Query.boost_query(
                            tantivy.Query.boolean_query(
                                [
                                    (
                                        tantivy.Occur.Must,
                                        tantivy.Query.term_query(
                                            self.schema,
                                            trigram_field,
                                            trigram,
                                        ),
                                    )
                                    for trigram in trigrams
                                ]
                            ),
                            self._INFIX_WEIGHTS[trigram_field],
                        ),
                    )
                )

        if not subqueries:
            return None
        return tantivy.Query.boolean_query(subqueries)


_EMBEDDED_IMAGE_EXTENSION_RE = re.compile(
    r"(?i)\.(?:png|jpe?g|webp|gif)$",
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
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


def search_corpus_lexical_path(
    data_root: Path,
    configured_path: Path | None = None,
) -> Path:
    return search_corpus_snapshot_path(data_root, configured_path).with_suffix(".tantivy")


def search_corpus_snapshot_path(
    data_root: Path,
    configured_path: Path | None = None,
) -> Path:
    if configured_path is not None:
        return configured_path.with_suffix(".pickle")
    version = SEARCH_CORPUS_VERSION.replace(".", "_")
    return data_root / f"search_corpus_{version}.pickle"


def load_search_corpus_snapshot(path: Path) -> SearchCorpusSnapshot:
    if not path.is_file():
        raise SearchCorpusError(
            f"search corpus snapshot is missing: {path}; "
            "run findme rebuild-search-corpus --config <config>"
        )
    with path.open("rb") as file:
        snapshot = pickle.load(file)
    if not isinstance(snapshot, SearchCorpusSnapshot):
        raise SearchCorpusError(
            f"search corpus snapshot has unexpected type: {path}; "
            "run findme rebuild-search-corpus --config <config>"
        )
    return snapshot


def rebuild_search_corpus(config: AppConfig, db_path: Path) -> Path:
    snapshot_destination = search_corpus_snapshot_path(
        config.data_root,
        config.search_corpus_path,
    )
    lexical_destination = search_corpus_lexical_path(
        config.data_root,
        config.search_corpus_path,
    )
    snapshot_destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix=f".{snapshot_destination.name}.",
        suffix=".tmp",
        dir=snapshot_destination.parent,
        delete=False,
    ) as snapshot_temp_file:
        temp_snapshot_path = Path(snapshot_temp_file.name)
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
            assets = fetch_search_corpus_assets(source_db)

        snapshot = _snapshot_for_assets(assets)
        with temp_snapshot_path.open("wb") as snapshot_file:
            pickle.dump(snapshot, snapshot_file, protocol=pickle.HIGHEST_PROTOCOL)

        SearchLexicalIndex.build(assets, path=temp_lexical_path)
        temp_snapshot_path.replace(snapshot_destination)
        _replace_directory(temp_lexical_path, lexical_destination)
        return snapshot_destination
    except Exception:
        temp_snapshot_path.unlink(missing_ok=True)
        shutil.rmtree(temp_lexical_path, ignore_errors=True)
        raise


def _snapshot_for_assets(assets: tuple[SearchCorpusAsset, ...]) -> SearchCorpusSnapshot:
    corpus = SearchCorpus.from_preprocessed_assets(assets, lexical_index=None)
    return SearchCorpusSnapshot(
        version=SEARCH_CORPUS_VERSION,
        assets=assets,
        directory_counts=count_asset_directories(
            [(asset.source_id, asset.directory) for asset in assets]
        ),
        source_display_names={
            asset.source_id: asset.source_display_name for asset in assets
        },
        source_sort_keys={
            asset.source_id: (
                -asset.source_priority,
                asset.source_display_name.casefold(),
                asset.source_id,
            )
            for asset in assets
        },
        fuzzy_tokens=corpus.fuzzy_tokens,
        fuzzy_token_asset_indexes=corpus.fuzzy_token_asset_indexes,
    )


def fetch_search_corpus_assets(
    db: sqlite3.Connection,
    *,
    source_id: str | None = None,
    directory: str | None = None,
    directory_prefix: str | None = None,
) -> tuple[SearchCorpusAsset, ...]:
    where = ["assets.stale = 0"]
    params: list[object] = []
    if source_id:
        where.append("assets.source_id = ?")
        params.append(source_id)
    if directory is not None:
        where.append("assets.directory = ?")
        params.append(directory)
    if directory_prefix:
        where.append("(assets.directory = ? OR assets.directory LIKE ?)")
        params.extend([directory_prefix, f"{directory_prefix}/%"])

    rows = db.execute(
        f"""
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
        WHERE {" AND ".join(where)}
        ORDER BY sources.priority DESC, assets.relative_path COLLATE NOCASE
        """,
        params,
    ).fetchall()
    return tuple(_row_to_preprocessed_asset(row, index) for index, row in enumerate(rows))


def _row_to_preprocessed_asset(row: sqlite3.Row, asset_index: int) -> SearchCorpusAsset:
    source_tokens = tokenize_search_text(f"{row['source_id']} {row['source_display_name']}")
    filename_tokens = tokenize_search_text(row["filename"])
    directory_tokens = tokenize_search_text(row["directory"])
    path_tokens = tokenize_search_text(row["relative_path"])
    normalized_text = normalize_search_text(
        " ".join((row["filename"], row["stem"], row["directory"], row["relative_path"]))
    )
    fuzzy_text = " ".join(filename_tokens + path_tokens)
    collection_key, variant_sort_key, has_variant = _card_metadata_parts(
        source_id=row["source_id"],
        directory=row["directory"],
        stem=row["stem"],
        relative_path=row["relative_path"],
    )
    return SearchCorpusAsset(
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


def card_metadata_by_asset_id(
    assets: Iterable[SearchCorpusAsset],
) -> dict[str, CardMetadata]:
    return {
        asset.asset_id: CardMetadata(
            asset_id=asset.asset_id,
            collection_key=asset.collection_key,
            variant_sort_key=asset.variant_sort_key,
            has_variant=asset.has_variant,
        )
        for asset in assets
    }


def count_asset_directories(
    directories: Iterable[tuple[str, str] | tuple[str, str, int]],
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for item in directories:
        source = item[0]
        directory = item[1]
        direct_count = item[2] if len(item) == 3 else 1
        counts[(source, "")] = counts.get((source, ""), 0) + direct_count
        if not directory:
            continue
        parts = directory.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            counts[(source, path)] = counts.get((source, path), 0) + direct_count
    return counts


def tokenize_search_text(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.casefold()))


def normalize_search_text(text: str) -> str:
    return " ".join(tokenize_search_text(text))


def _trigrams(token: str) -> tuple[str, ...]:
    if len(token) < 3:
        return ()
    return tuple(dict.fromkeys(token[index : index + 3] for index in range(len(token) - 2)))


def _trigram_text(tokens: tuple[str, ...]) -> str:
    return " ".join(trigram for token in tokens for trigram in _trigrams(token))


def _replace_directory(source: Path, destination: Path) -> None:
    old_destination = destination.with_name(f".{destination.name}.old")
    if old_destination.exists():
        shutil.rmtree(old_destination)
    if destination.exists():
        destination.rename(old_destination)
    source.rename(destination)
    shutil.rmtree(old_destination, ignore_errors=True)
