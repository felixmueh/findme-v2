from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import re
import sqlite3
import threading

from rapidfuzz import fuzz, process
import tantivy
import yaml


SearchMode = Literal["strict", "fuzzy", "domain", "combined"]


@dataclass(frozen=True)
class SearchResult:
    asset_id: str
    source_id: str
    source_display_name: str
    relative_path: str
    filename: str
    stem: str
    extension: str
    directory: str
    size_bytes: int
    media_type: str
    thumbnail_path: str | None
    thumbnail_width: int | None
    thumbnail_height: int | None
    score: float


@dataclass(frozen=True)
class SearchResultsPage:
    assets: list[SearchResult]
    total: int


@dataclass(frozen=True)
class DirectoryMatch:
    source_id: str
    path: str
    name: str
    depth: int
    asset_count: int
    matched_asset_count: int


class SearchSuperseded(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchControl:
    is_cancelled: Callable[[], bool]
    check_every: int = 4096

    def check(self, count: int) -> None:
        if count % self.check_every == 0 and self.is_cancelled():
            raise SearchSuperseded


@dataclass(frozen=True)
class DomainVocabulary:
    expansions: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class _AssetRecord:
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


@dataclass(frozen=True)
class _SearchCorpus:
    assets: tuple[_AssetRecord, ...]
    fuzzy_tokens: tuple[str, ...]
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...]
    lexical_index: "_TantivyLexicalIndex | None" = None

    @classmethod
    def from_assets(cls, assets: Iterable[_AssetRecord]) -> "_SearchCorpus":
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
            lexical_index=_TantivyLexicalIndex.build(asset_tuple),
        )


@dataclass(frozen=True)
class _Candidate:
    asset: _AssetRecord
    lane: str
    raw_score: float
    matched_terms: tuple[str, ...]
    query_variant: str


@dataclass(frozen=True)
class _RankedCandidate:
    asset: _AssetRecord
    score: float


_SearchCacheKey = tuple[object, ...]


@dataclass
class _InFlightSearch:
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple[_RankedCandidate, ...] | None = None
    error: BaseException | None = None


@dataclass
class SearchIndex:
    assets: tuple[_AssetRecord, ...]
    directory_counts: dict[tuple[str, str], int]
    source_display_names: dict[str, str]
    fuzzy_tokens: tuple[str, ...] = ()
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...] = ()
    lexical_index: "_TantivyLexicalIndex | None" = None
    cache_size: int = 128
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)
    _ranked_cache: OrderedDict[_SearchCacheKey, tuple[_RankedCandidate, ...]] = field(
        default_factory=OrderedDict
    )
    _in_flight: dict[_SearchCacheKey, _InFlightSearch] = field(default_factory=dict)

    @classmethod
    def load(cls, db: sqlite3.Connection) -> "SearchIndex":
        assets = _fetch_assets(
            db,
            source_id=None,
            directory=None,
            directory_prefix=None,
        )
        corpus = _SearchCorpus.from_assets(assets)
        return cls(
            assets=assets,
            directory_counts=_counts_from_asset_directories(
                [(asset.source_id, asset.directory) for asset in assets]
            ),
            source_display_names={
                asset.source_id: asset.source_display_name for asset in assets
            },
            fuzzy_tokens=corpus.fuzzy_tokens,
            fuzzy_token_asset_indexes=corpus.fuzzy_token_asset_indexes,
            lexical_index=corpus.lexical_index,
        )

    def search(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "combined",
        limit: int = 100,
        offset: int = 0,
        control: SearchControl | None = None,
    ) -> SearchResultsPage:
        domain_vocabulary = domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY
        if mode == "strict":
            corpus = _filter_corpus(
                _SearchCorpus(
                    self.assets,
                    self.fuzzy_tokens,
                    self.fuzzy_token_asset_indexes,
                    self.lexical_index,
                ),
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                control=control,
            )
            return _strict_search_page(
                corpus,
                query,
                limit=limit,
                offset=offset,
                control=control,
            )
        ranked = self._ranked_search(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            control=control,
        )
        return SearchResultsPage(
            assets=[
                _record_to_result(candidate.asset, candidate.score)
                for candidate in ranked[offset : offset + limit]
            ],
            total=len(ranked),
        )

    def scoped_assets(
        self,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        control: SearchControl | None = None,
    ) -> list[SearchResult]:
        corpus = _filter_corpus(
            _SearchCorpus(
                self.assets,
                self.fuzzy_tokens,
                self.fuzzy_token_asset_indexes,
                self.lexical_index,
            ),
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            control=control,
        )
        return [_record_to_result(asset, 0) for asset in corpus.assets]

    def directory_matches(
        self,
        query: str | None = None,
        *,
        source_id: str | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "combined",
        control: SearchControl | None = None,
    ) -> list[DirectoryMatch]:
        total_counts = _filter_directory_counts(self.directory_counts, source_id)
        match_counts = total_counts
        if query and query.strip():
            if mode == "strict":
                corpus = _filter_corpus(
                    _SearchCorpus(
                        self.assets,
                        self.fuzzy_tokens,
                        self.fuzzy_token_asset_indexes,
                        self.lexical_index,
                    ),
                    source_id=source_id,
                    directory=None,
                    directory_prefix=None,
                    control=control,
                )
                match_counts = _counts_from_asset_directories(
                    _strict_match_directories(corpus, query, control=control)
                )
            else:
                ranked = self._ranked_search(
                    query,
                    source_id=source_id,
                    directory=None,
                    directory_prefix=None,
                    domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
                    mode=mode,
                    control=control,
                )
                match_counts = _counts_from_asset_directories(
                    [
                        (candidate.asset.source_id, candidate.asset.directory)
                        for candidate in ranked
                    ]
                )

        directories = []
        for (source, path), total_count in total_counts.items():
            matched_count = match_counts.get((source, path), 0)
            if matched_count == 0:
                continue
            name = self.source_display_names.get(source, source) if path == "" else path.rsplit("/", 1)[-1]
            depth = 0 if path == "" else len(path.split("/"))
            directories.append(
                DirectoryMatch(
                    source_id=source,
                    path=path,
                    name=name,
                    depth=depth,
                    asset_count=total_count,
                    matched_asset_count=matched_count,
                )
            )
        return sorted(
            directories,
            key=lambda item: (item.path.lower() != "", item.path.lower()),
        )

    def _ranked_search(
        self,
        query: str,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        domain_vocabulary: DomainVocabulary,
        mode: SearchMode,
        control: SearchControl | None,
    ) -> tuple[_RankedCandidate, ...]:
        if control and control.is_cancelled():
            raise SearchSuperseded
        key = _search_cache_key(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
        )
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        owner = False
        with self._cache_lock:
            cached = self._cache_get_locked(key)
            if cached is not None:
                return cached
            in_flight = self._in_flight.get(key)
            if in_flight is None:
                in_flight = _InFlightSearch()
                self._in_flight[key] = in_flight
                owner = True

        if not owner:
            ranked = self._wait_for_in_flight(key, in_flight, control)
            if ranked is not None:
                return ranked
            return self._ranked_search(
                query,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                domain_vocabulary=domain_vocabulary,
                mode=mode,
                control=control,
            )

        try:
            ranked = tuple(
                _rank_search_assets(
                    _filter_corpus(
                        _SearchCorpus(
                            self.assets,
                            self.fuzzy_tokens,
                            self.fuzzy_token_asset_indexes,
                            self.lexical_index,
                        ),
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        control=control,
                    ),
                    query,
                    domain_vocabulary=domain_vocabulary,
                    mode=mode,
                    control=control,
                )
            )
        except BaseException as exc:
            with self._cache_lock:
                in_flight.error = exc
                self._in_flight.pop(key, None)
                in_flight.done.set()
            raise

        with self._cache_lock:
            self._cache_put_locked(key, ranked)
            in_flight.result = ranked
            self._in_flight.pop(key, None)
            in_flight.done.set()
        return ranked

    def _wait_for_in_flight(
        self,
        key: _SearchCacheKey,
        in_flight: _InFlightSearch,
        control: SearchControl | None,
    ) -> tuple[_RankedCandidate, ...] | None:
        while not in_flight.done.wait(0.025):
            if control and control.is_cancelled():
                raise SearchSuperseded
        if in_flight.error is not None:
            if control and control.is_cancelled():
                raise SearchSuperseded
            cached = self._cache_get(key)
            if cached is not None:
                return cached
            if isinstance(in_flight.error, SearchSuperseded):
                return None
            raise in_flight.error
        if in_flight.result is None:
            raise RuntimeError("search finished without a result")
        return in_flight.result

    def _cache_get(self, key: _SearchCacheKey) -> tuple[_RankedCandidate, ...] | None:
        with self._cache_lock:
            return self._cache_get_locked(key)

    def _cache_get_locked(
        self, key: _SearchCacheKey
    ) -> tuple[_RankedCandidate, ...] | None:
        cached = self._ranked_cache.get(key)
        if cached is None:
            return None
        self._ranked_cache.move_to_end(key)
        return cached

    def _cache_put_locked(
        self,
        key: _SearchCacheKey,
        ranked: tuple[_RankedCandidate, ...],
    ) -> None:
        self._ranked_cache[key] = ranked
        self._ranked_cache.move_to_end(key)
        while len(self._ranked_cache) > self.cache_size:
            self._ranked_cache.popitem(last=False)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
DEFAULT_DOMAIN_VOCABULARY = DomainVocabulary(
    expansions={
        "adventurer": (
            "warrior",
            "fighter",
            "mage",
            "wizard",
            "rogue",
            "thief",
            "ranger",
            "cleric",
            "paladin",
        ),
    }
)


def search_assets(
    db: sqlite3.Connection,
    query: str,
    *,
    source_id: str | None = None,
    directory: str | None = None,
    directory_prefix: str | None = None,
    domain_vocabulary: DomainVocabulary | None = None,
    mode: SearchMode = "combined",
    limit: int = 100,
    offset: int = 0,
) -> list[SearchResult]:
    return search_asset_page(
        db,
        query,
        source_id=source_id,
        directory=directory,
        directory_prefix=directory_prefix,
        domain_vocabulary=domain_vocabulary,
        mode=mode,
        limit=limit,
        offset=offset,
    ).assets


def search_asset_page(
    db: sqlite3.Connection,
    query: str,
    *,
    source_id: str | None = None,
    directory: str | None = None,
    directory_prefix: str | None = None,
    domain_vocabulary: DomainVocabulary | None = None,
    mode: SearchMode = "combined",
    limit: int = 100,
    offset: int = 0,
) -> SearchResultsPage:
    ranked = _rank_search(
        db,
        query,
        source_id=source_id,
        directory=directory,
        directory_prefix=directory_prefix,
        domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
        mode=mode,
    )
    return SearchResultsPage(
        assets=[
            _record_to_result(candidate.asset, candidate.score)
            for candidate in ranked[offset : offset + limit]
        ],
        total=len(ranked),
    )


def count_search_assets(
    db: sqlite3.Connection,
    query: str,
    *,
    source_id: str | None = None,
    directory: str | None = None,
    directory_prefix: str | None = None,
    domain_vocabulary: DomainVocabulary | None = None,
    mode: SearchMode = "combined",
) -> int:
    return len(
        _rank_search(
            db,
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
            mode=mode,
        )
    )


def load_domain_vocabulary(path: Path) -> DomainVocabulary:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("domain vocabulary must contain a mapping")
    raw_expansions = raw.get("expansions", {})
    if not isinstance(raw_expansions, dict):
        raise ValueError("domain vocabulary expansions must be a mapping")

    expansions: dict[str, tuple[str, ...]] = {}
    for raw_term, raw_terms in raw_expansions.items():
        if not isinstance(raw_term, str) or not raw_term.strip():
            raise ValueError("domain vocabulary expansion keys must be non-empty strings")
        if not isinstance(raw_terms, list) or not raw_terms:
            raise ValueError(
                f"domain vocabulary expansion for {raw_term} must be a non-empty list"
            )
        terms = []
        for expanded in raw_terms:
            if not isinstance(expanded, str) or not expanded.strip():
                raise ValueError(
                    f"domain vocabulary expansion for {raw_term} contains a non-string term"
                )
            terms.append(expanded.strip().casefold())
        expansions[raw_term.strip().casefold()] = tuple(dict.fromkeys(terms))
    return DomainVocabulary(expansions=expansions)


def _rank_search(
    db: sqlite3.Connection,
    query: str,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    domain_vocabulary: DomainVocabulary,
    mode: SearchMode,
) -> list[_RankedCandidate]:
    return _rank_search_assets(
        _SearchCorpus.from_assets(
            _fetch_assets(
                db,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
            )
        ),
        query,
        domain_vocabulary=domain_vocabulary,
        mode=mode,
    )


def _strict_search_page(
    corpus: _SearchCorpus,
    query: str,
    *,
    limit: int,
    offset: int,
    control: SearchControl | None = None,
) -> SearchResultsPage:
    query_tokens = _tokens(query)
    if not query_tokens:
        return SearchResultsPage(assets=[], total=0)
    if corpus.lexical_index is None:
        ranked = _rank_candidates(
            _strict_candidates(
                corpus.assets,
                query_tokens,
                query,
                require_all_terms=True,
                control=control,
            ),
            query_tokens,
            control=control,
        )
        return SearchResultsPage(
            assets=[
                _record_to_result(candidate.asset, candidate.score)
                for candidate in ranked[offset : offset + limit]
            ],
            total=len(ranked),
        )

    matches, total = corpus.lexical_index.search_with_scores(
        query_tokens,
        require_all_terms=True,
        limit=limit,
        offset=offset,
    )
    results = []
    for index, (asset_index, score) in enumerate(matches, start=1):
        if control:
            control.check(index)
        results.append(_record_to_result(corpus.assets[asset_index], score))
    return SearchResultsPage(assets=results, total=total)


def _strict_match_directories(
    corpus: _SearchCorpus,
    query: str,
    *,
    control: SearchControl | None = None,
) -> list[tuple[str, str]]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    if corpus.lexical_index is None:
        return [
            (candidate.asset.source_id, candidate.asset.directory)
            for candidate in _strict_candidates(
                corpus.assets,
                query_tokens,
                query,
                require_all_terms=True,
                control=control,
            )
        ]
    matches, _total = corpus.lexical_index.search_with_scores(
        query_tokens,
        require_all_terms=True,
        limit=corpus.lexical_index.asset_count,
        offset=0,
    )
    directories = []
    for index, (asset_index, _score) in enumerate(matches, start=1):
        if control:
            control.check(index)
        asset = corpus.assets[asset_index]
        directories.append((asset.source_id, asset.directory))
    return directories


def _rank_search_assets(
    assets: Iterable[_AssetRecord] | _SearchCorpus,
    query: str,
    *,
    domain_vocabulary: DomainVocabulary,
    mode: SearchMode,
    control: SearchControl | None = None,
) -> list[_RankedCandidate]:
    corpus = (
        assets if isinstance(assets, _SearchCorpus) else _SearchCorpus.from_assets(assets)
    )
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    if mode not in {"strict", "fuzzy", "domain", "combined"}:
        raise ValueError(f"unsupported search mode: {mode}")

    candidates: list[_Candidate] = []
    if mode == "strict":
        candidates.extend(
            _strict_candidates(
                corpus,
                query_tokens,
                query,
                require_all_terms=True,
                control=control,
            )
        )
    if mode == "combined":
        strict_candidates = _strict_candidates(
            corpus,
            query_tokens,
            query,
            require_all_terms=False,
            control=control,
        )
        candidates.extend(strict_candidates)
    if mode in {"fuzzy", "combined"}:
        if not (
            mode == "combined"
            and _has_plenty_of_strict_all_term_matches(
                strict_candidates,
                query_tokens,
            )
        ):
            candidates.extend(
                _fuzzy_candidates(
                    corpus,
                    query_tokens,
                    query,
                    suppress_strict_all_terms=mode == "combined",
                    control=control,
                )
            )
    if mode in {"domain", "combined"}:
        candidates.extend(
            _domain_candidates(
                corpus,
                query_tokens,
                query,
                domain_vocabulary,
                control=control,
            )
        )
    return _rank_candidates(candidates, query_tokens, control=control)


def _has_plenty_of_strict_all_term_matches(
    candidates: list[_Candidate],
    query_tokens: tuple[str, ...],
) -> bool:
    query_token_set = set(query_tokens)
    return sum(
        1
        for candidate in candidates
        if set(candidate.matched_terms) == query_token_set
    ) >= 500


def _search_cache_key(
    query: str,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    domain_vocabulary: DomainVocabulary,
    mode: SearchMode,
) -> _SearchCacheKey:
    return (
        _normalize_text(query),
        mode,
        source_id,
        directory,
        directory_prefix,
        tuple(sorted(domain_vocabulary.expansions.items())),
    )


class _TantivyLexicalIndex:
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
    def build(cls, assets: tuple[_AssetRecord, ...]) -> "_TantivyLexicalIndex | None":
        if not assets:
            return None
        schema_builder = tantivy.SchemaBuilder()
        schema_builder.add_integer_field("asset_index", stored=True, fast=True)
        for field_name in cls._FIELDS + cls._TRIGRAM_FIELDS:
            schema_builder.add_text_field(field_name)
        schema = schema_builder.build()
        index = tantivy.Index(schema)
        writer = index.writer(heap_size=300_000_000, num_threads=2)
        for asset_index, asset in enumerate(assets):
            writer.add_document(
                tantivy.Document(
                    asset_index=asset_index,
                    stem=" ".join(_tokens(asset.stem)),
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


def _trigrams(token: str) -> tuple[str, ...]:
    if len(token) < 3:
        return ()
    return tuple(dict.fromkeys(token[index : index + 3] for index in range(len(token) - 2)))


def _trigram_text(tokens: tuple[str, ...]) -> str:
    return " ".join(trigram for token in tokens for trigram in _trigrams(token))


def _domain_candidate_indexes(
    corpus: _SearchCorpus,
    expanded_terms: tuple[str, ...],
) -> tuple[int, ...] | range:
    if corpus.lexical_index is None:
        return range(len(corpus.assets))
    candidate_indexes: set[int] = set()
    for term in expanded_terms:
        candidate_indexes.update(corpus.lexical_index.candidate_indexes(_tokens(term)))
    return tuple(candidate_indexes)


def _filter_corpus(
    corpus: _SearchCorpus,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    control: SearchControl | None = None,
) -> _SearchCorpus:
    if source_id is None and directory is None and directory_prefix is None:
        return corpus

    filtered_assets = []
    for index, asset in enumerate(corpus.assets, start=1):
        if control:
            control.check(index)
        if source_id and asset.source_id != source_id:
            continue
        if directory is not None and asset.directory != directory:
            continue
        if directory_prefix and not (
            asset.directory == directory_prefix
            or asset.directory.startswith(f"{directory_prefix}/")
        ):
            continue
        filtered_assets.append(asset)
    return _SearchCorpus.from_assets(filtered_assets)


def _filter_directory_counts(
    directory_counts: dict[tuple[str, str], int],
    source_id: str | None,
) -> dict[tuple[str, str], int]:
    if not source_id:
        return directory_counts
    return {
        (source, path): count
        for (source, path), count in directory_counts.items()
        if source == source_id
    }


def _counts_from_asset_directories(
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


def _fetch_assets(
    db: sqlite3.Connection,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
) -> tuple[_AssetRecord, ...]:
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
    return tuple(_row_to_record(row) for row in rows)


def _strict_candidates(
    corpus_or_assets: Iterable[_AssetRecord] | _SearchCorpus,
    query_tokens: tuple[str, ...],
    query: str,
    *,
    require_all_terms: bool,
    control: SearchControl | None = None,
) -> list[_Candidate]:
    corpus = (
        corpus_or_assets
        if isinstance(corpus_or_assets, _SearchCorpus)
        else _SearchCorpus.from_assets(corpus_or_assets)
    )
    assets = corpus.assets
    candidate_matches = (
        corpus.lexical_index.candidates_with_scores(query_tokens)
        if corpus.lexical_index is not None
        else tuple((asset_index, 0.0) for asset_index in range(len(assets)))
    )
    candidates = []
    for index, (asset_index, lexical_score) in enumerate(candidate_matches, start=1):
        if control:
            control.check(index)
        asset = assets[asset_index]
        _fallback_score, matched = _strict_score(asset, query_tokens)
        score = _strict_tantivy_raw_score(lexical_score, query_tokens)
        if score <= 0:
            continue
        if require_all_terms and set(matched) != set(query_tokens):
            continue
        candidates.append(
            _Candidate(
                asset=asset,
                lane="strict",
                raw_score=score,
                matched_terms=matched,
                query_variant=query,
            )
        )
    return sorted(candidates, key=_candidate_lane_sort_key)


def _fuzzy_candidates(
    corpus: _SearchCorpus,
    query_tokens: tuple[str, ...],
    query: str,
    *,
    suppress_strict_all_terms: bool,
    control: SearchControl | None = None,
) -> list[_Candidate]:
    token_matches = _fuzzy_token_matches(corpus, query_tokens)
    if not token_matches:
        return []

    matching_indexes = set(token_matches[0])
    for matches in token_matches[1:]:
        matching_indexes.intersection_update(matches)
        if not matching_indexes:
            return []

    candidates = []
    for asset_index in matching_indexes:
        index = asset_index + 1
        if control:
            control.check(index)
        asset = corpus.assets[asset_index]
        strict_score, strict_matched = _strict_score(asset, query_tokens)
        if (
            suppress_strict_all_terms
            and strict_score > 0
            and set(strict_matched) == set(query_tokens)
        ):
            continue
        matched = query_tokens
        candidates.append(
            _Candidate(
                asset=asset,
                lane="fuzzy",
                raw_score=_fuzzy_raw_score(
                    asset,
                    query_tokens,
                    query,
                    tuple(
                        token_matches_by_index[asset_index]
                        for token_matches_by_index in token_matches
                    ),
                ),
                matched_terms=matched,
                query_variant=query,
            )
        )
    return sorted(candidates, key=_candidate_lane_sort_key)


def _domain_candidates(
    corpus_or_assets: Iterable[_AssetRecord] | _SearchCorpus,
    query_tokens: tuple[str, ...],
    query: str,
    domain_vocabulary: DomainVocabulary,
    *,
    control: SearchControl | None = None,
) -> list[_Candidate]:
    expanded_terms = _expanded_terms(query_tokens, domain_vocabulary)
    if not expanded_terms:
        return []
    corpus = (
        corpus_or_assets
        if isinstance(corpus_or_assets, _SearchCorpus)
        else _SearchCorpus.from_assets(corpus_or_assets)
    )
    assets = corpus.assets
    candidates = []
    candidate_indexes = _domain_candidate_indexes(corpus, expanded_terms)
    for index, asset_index in enumerate(candidate_indexes, start=1):
        if control:
            control.check(index)
        asset = assets[asset_index]
        score, matched = _domain_score(asset, expanded_terms)
        if score <= 0:
            continue
        candidates.append(
            _Candidate(
                asset=asset,
                lane="domain",
                raw_score=score,
                matched_terms=matched,
                query_variant=", ".join(expanded_terms),
            )
        )
    return sorted(candidates, key=_candidate_lane_sort_key)


def _rank_candidates(
    candidates: list[_Candidate],
    query_tokens: tuple[str, ...],
    *,
    control: SearchControl | None = None,
) -> list[_RankedCandidate]:
    by_asset: dict[str, list[tuple[int, _Candidate]]] = defaultdict(list)
    for lane in ("strict", "fuzzy", "domain"):
        if control:
            control.check(0)
        lane_candidates = [
            candidate for candidate in candidates if candidate.lane == lane
        ]
        lane_candidates.sort(key=_candidate_lane_sort_key)
        for rank, candidate in enumerate(lane_candidates, start=1):
            if control:
                control.check(rank)
            by_asset[candidate.asset.asset_id].append((rank, candidate))

    ranked = []
    for index, lane_matches in enumerate(by_asset.values(), start=1):
        if control:
            control.check(index)
        asset = lane_matches[0][1].asset
        score = sum(
            _lane_contribution(rank, candidate) for rank, candidate in lane_matches
        )
        score += asset.source_priority * 0.01
        score += _media_intent_adjustment(asset, query_tokens)
        ranked.append(_RankedCandidate(asset=asset, score=score))
    return sorted(
        ranked,
        key=lambda candidate: (
            -candidate.score,
            -candidate.asset.source_priority,
            candidate.asset.relative_path.casefold(),
        ),
    )


def _lane_contribution(rank: int, candidate: _Candidate) -> float:
    lane_weight = {
        "strict": 1000.0,
        "fuzzy": 500.0,
        "domain": 300.0,
    }[candidate.lane]
    return lane_weight + candidate.raw_score + (100.0 / rank)


def _candidate_lane_sort_key(candidate: _Candidate) -> tuple[float, int, str]:
    return (
        -candidate.raw_score,
        -candidate.asset.source_priority,
        candidate.asset.relative_path.casefold(),
    )


def _media_intent_adjustment(
    asset: _AssetRecord,
    query_tokens: tuple[str, ...],
) -> float:
    if asset.source_id not in {"music", "sounds"}:
        return 0.0
    audio_terms = {
        "ambiance",
        "ambience",
        "audio",
        "combat",
        "forest",
        "music",
        "rain",
        "sound",
        "sounds",
        "tavern",
        "thunder",
        "track",
    }
    if set(query_tokens).intersection(audio_terms):
        return 0.0
    return -220.0


def _strict_score(
    asset: _AssetRecord,
    query_tokens: tuple[str, ...],
) -> tuple[float, tuple[str, ...]]:
    matched = []
    score = 0.0
    for token in query_tokens:
        token_score = _best_strict_token_score(asset, token)
        if token_score > 0:
            matched.append(token)
            score += token_score
    if not matched:
        return 0.0, ()
    if len(set(matched)) == len(set(query_tokens)):
        score += 150.0
    if " ".join(query_tokens) in asset.normalized_text:
        score += 80.0
    return score, tuple(matched)


def _best_strict_token_score(asset: _AssetRecord, token: str) -> float:
    return max(
        _field_token_score(
            asset.filename_tokens,
            token,
            exact=50.0,
            prefix=38.0,
            infix=24.0,
        ),
        _field_token_score(
            asset.path_tokens,
            token,
            exact=28.0,
            prefix=20.0,
            infix=12.0,
        ),
        _field_token_score(
            asset.directory_tokens,
            token,
            exact=18.0,
            prefix=12.0,
            infix=6.0,
        ),
        _field_token_score(
            asset.source_tokens,
            token,
            exact=22.0,
            prefix=16.0,
            infix=8.0,
        ),
    )


def _field_token_score(
    field_tokens: tuple[str, ...],
    token: str,
    *,
    exact: float,
    prefix: float,
    infix: float,
) -> float:
    best = 0.0
    for field_token in field_tokens:
        if field_token == token:
            best = max(best, exact)
        elif field_token.startswith(token):
            best = max(best, prefix)
        elif token in field_token:
            best = max(best, infix)
    return best


def _strict_tantivy_raw_score(
    lexical_score: float,
    query_tokens: tuple[str, ...],
) -> float:
    if lexical_score <= 0:
        return 0.0
    return lexical_score * max(len(query_tokens), 1)


def _fuzzy_token_matches(
    corpus: _SearchCorpus,
    query_tokens: tuple[str, ...],
) -> tuple[dict[int, float], ...]:
    token_matches = []
    for token in query_tokens:
        threshold = 100.0 if len(token) <= 3 else 82.0
        matches: dict[int, float] = {}
        for _choice, score, token_index in process.extract(
            token,
            corpus.fuzzy_tokens,
            scorer=fuzz.ratio,
            score_cutoff=threshold,
            limit=None,
        ):
            for asset_index in corpus.fuzzy_token_asset_indexes[token_index]:
                matches[asset_index] = max(matches.get(asset_index, 0.0), float(score))
        token_matches.append(matches)
    return tuple(token_matches)


def _fuzzy_raw_score(
    asset: _AssetRecord,
    query_tokens: tuple[str, ...],
    query: str,
    token_scores: tuple[float, ...],
) -> float:
    query_score = float(fuzz.WRatio(_normalize_text(query), asset.fuzzy_text))
    score = sum(token_scores) + query_score
    if all(token in asset.filename_tokens for token in query_tokens):
        score += 80.0
    return score


def _domain_score(
    asset: _AssetRecord,
    expanded_terms: tuple[str, ...],
) -> tuple[float, tuple[str, ...]]:
    matched = []
    score = 0.0
    for term in expanded_terms:
        term_tokens = _tokens(term)
        term_score, term_matches = _strict_score(asset, term_tokens)
        if term_score <= 0:
            continue
        matched.extend(term_matches)
        score += min(term_score, 120.0)
    if not matched:
        return 0.0, ()
    return score, tuple(sorted(set(matched)))


def _expanded_terms(
    query_tokens: tuple[str, ...],
    domain_vocabulary: DomainVocabulary,
) -> tuple[str, ...]:
    expanded = []
    for token in query_tokens:
        expanded.extend(domain_vocabulary.expansions.get(token, ()))
    return tuple(dict.fromkeys(expanded))


def _row_to_record(row: sqlite3.Row) -> _AssetRecord:
    source_tokens = _tokens(f"{row['source_id']} {row['source_display_name']}")
    filename_tokens = _tokens(row["filename"])
    directory_tokens = _tokens(row["directory"])
    path_tokens = _tokens(row["relative_path"])
    normalized_text = _normalize_text(
        " ".join((row["filename"], row["stem"], row["directory"], row["relative_path"]))
    )
    fuzzy_text = " ".join(filename_tokens + path_tokens)
    return _AssetRecord(
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
    )


def _record_to_result(asset: _AssetRecord, score: float) -> SearchResult:
    return SearchResult(
        asset_id=asset.asset_id,
        source_id=asset.source_id,
        source_display_name=asset.source_display_name,
        relative_path=asset.relative_path,
        filename=asset.filename,
        stem=asset.stem,
        extension=asset.extension,
        directory=asset.directory,
        size_bytes=asset.size_bytes,
        media_type=asset.media_type,
        thumbnail_path=asset.thumbnail_path,
        thumbnail_width=asset.thumbnail_width,
        thumbnail_height=asset.thumbnail_height,
        score=score,
    )


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.casefold()))


def _normalize_text(text: str) -> str:
    return " ".join(_tokens(text))
