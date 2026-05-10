from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import sqlite3
import threading

from rapidfuzz import fuzz, process
import yaml

from findme.search_corpus import (
    SearchCorpus,
    SearchCorpusAsset,
    SearchCorpusError,
    SearchCorpusSnapshot,
    SearchLexicalIndex,
    count_asset_directories,
    fetch_search_corpus_assets,
    normalize_search_text,
    tokenize_search_text,
)


SearchMode = Literal["strict", "fuzzy", "domain", "combined"]
DEFAULT_FOLDER_QUALITY = 5
FOLDER_QUALITY_WEIGHT = 20.0


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
    mtime_ns: int
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
    quality: int
    quality_override: int | None


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
class _Candidate:
    asset: SearchCorpusAsset
    lane: str
    raw_score: float
    matched_terms: tuple[str, ...]
    query_variant: str


@dataclass(frozen=True)
class _RankedCandidate:
    asset: SearchCorpusAsset
    score: float


_SearchCacheKey = tuple[object, ...]


@dataclass
class _InFlightSearch:
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple[_RankedCandidate, ...] | None = None
    error: BaseException | None = None


@dataclass
class SearchIndex:
    assets: tuple[SearchCorpusAsset, ...]
    directory_counts: dict[tuple[str, str], int]
    directory_asset_indexes: dict[tuple[str, str], tuple[int, ...]]
    source_display_names: dict[str, str]
    source_sort_keys: dict[str, tuple[int, str, str]]
    folder_quality_overrides: dict[tuple[str, str], int] = field(default_factory=dict)
    preference_adjustments: tuple[float, ...] = ()
    preferences_revision: int = 0
    fuzzy_tokens: tuple[str, ...] = ()
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...] = ()
    lexical_index: "SearchLexicalIndex | None" = None
    cache_size: int = 128
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)
    _ranked_cache: OrderedDict[_SearchCacheKey, tuple[_RankedCandidate, ...]] = field(
        default_factory=OrderedDict
    )
    _in_flight: dict[_SearchCacheKey, _InFlightSearch] = field(default_factory=dict)

    @classmethod
    def load(cls, db: sqlite3.Connection) -> "SearchIndex":
        assets = fetch_search_corpus_assets(
            db,
            source_id=None,
            directory=None,
            directory_prefix=None,
        )
        folder_quality_overrides = _fetch_folder_quality_overrides(db)
        corpus = SearchCorpus.from_assets(assets)
        return cls(
            assets=assets,
            directory_counts=count_asset_directories(
                [(asset.source_id, asset.directory) for asset in assets]
            ),
            directory_asset_indexes=_asset_indexes_by_directory(assets),
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
            folder_quality_overrides=folder_quality_overrides,
            preference_adjustments=_preference_adjustments(
                assets,
                folder_quality_overrides,
            ),
            fuzzy_tokens=corpus.fuzzy_tokens,
            fuzzy_token_asset_indexes=corpus.fuzzy_token_asset_indexes,
            lexical_index=corpus.lexical_index,
        )

    @classmethod
    def load_from_snapshot(
        cls,
        db: sqlite3.Connection,
        snapshot: SearchCorpusSnapshot,
        *,
        expected_version: str,
        lexical_index_path: Path,
    ) -> "SearchIndex":
        if snapshot.version != expected_version:
            raise SearchCorpusError(
                "search corpus version mismatch "
                f"(expected {expected_version}, found {snapshot.version}); "
                "run findme rebuild-search-corpus --config <config>"
            )
        lexical_index = None
        if snapshot.assets:
            if not lexical_index_path.is_dir():
                raise SearchCorpusError(
                    f"search lexical index is missing: {lexical_index_path}; "
                    "run findme rebuild-search-corpus --config <config>"
                )
            lexical_index = SearchLexicalIndex.open(
                lexical_index_path,
                asset_count=len(snapshot.assets),
            )
        folder_quality_overrides = _fetch_folder_quality_overrides(db)
        return cls(
            assets=snapshot.assets,
            directory_counts=snapshot.directory_counts,
            directory_asset_indexes=_asset_indexes_by_directory(snapshot.assets),
            source_display_names=snapshot.source_display_names,
            source_sort_keys=snapshot.source_sort_keys,
            folder_quality_overrides=folder_quality_overrides,
            preference_adjustments=_preference_adjustments(
                snapshot.assets,
                folder_quality_overrides,
            ),
            fuzzy_tokens=snapshot.fuzzy_tokens,
            fuzzy_token_asset_indexes=snapshot.fuzzy_token_asset_indexes,
            lexical_index=lexical_index,
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
                SearchCorpus(
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
                preference_adjustment=self._preference_adjustment,
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
            SearchCorpus(
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

    def set_folder_quality(self, source_id: str, directory: str, quality: int) -> None:
        if quality < 0 or quality > 10:
            raise ValueError("folder quality must be between 0 and 10")
        if (source_id, directory) not in self.directory_counts:
            raise KeyError((source_id, directory))
        with self._cache_lock:
            next_overrides = dict(self.folder_quality_overrides)
            if quality == DEFAULT_FOLDER_QUALITY:
                next_overrides.pop((source_id, directory), None)
            else:
                next_overrides[(source_id, directory)] = quality
            self._replace_folder_quality_overrides_locked(
                source_id,
                directory,
                next_overrides,
            )

    def folder_quality(self, source_id: str, directory: str) -> tuple[int, int | None]:
        override = self.folder_quality_overrides.get((source_id, directory))
        return (
            _effective_folder_quality(
                self.folder_quality_overrides,
                source_id,
                directory,
            ),
            override,
        )

    def _replace_folder_quality_overrides_locked(
        self,
        changed_source_id: str,
        changed_directory: str,
        folder_quality_overrides: dict[tuple[str, str], int],
    ) -> None:
        preference_adjustments = _updated_preference_adjustments(
            self.directory_asset_indexes,
            self.preference_adjustments,
            self.folder_quality_overrides,
            folder_quality_overrides,
            changed_source_id=changed_source_id,
            changed_directory=changed_directory,
        )
        self.folder_quality_overrides = folder_quality_overrides
        self.preference_adjustments = preference_adjustments
        self.preferences_revision += 1
        self._ranked_cache.clear()

    def _preference_adjustment(self, asset: SearchCorpusAsset) -> float:
        return self.preference_adjustments[asset.asset_index]

    def directories_for_matches(
        self,
        matches: Iterable[SearchResult],
        *,
        source_id: str | None = None,
    ) -> list[DirectoryMatch]:
        total_counts = _filter_directory_counts(self.directory_counts, source_id)
        match_counts = count_asset_directories(
            (match.source_id, match.directory) for match in matches
        )
        return self._directory_matches_from_counts(total_counts, match_counts)

    def _directory_matches_from_counts(
        self,
        total_counts: dict[tuple[str, str], int],
        match_counts: dict[tuple[str, str], int],
    ) -> list[DirectoryMatch]:
        directories = []
        for (source, path), total_count in total_counts.items():
            matched_count = match_counts.get((source, path), 0)
            if matched_count == 0:
                continue
            name = self.source_display_names.get(source, source) if path == "" else path.rsplit("/", 1)[-1]
            depth = 0 if path == "" else len(path.split("/"))
            quality, quality_override = self.folder_quality(source, path)
            directories.append(
                DirectoryMatch(
                    source_id=source,
                    path=path,
                    name=name,
                    depth=depth,
                    asset_count=total_count,
                    matched_asset_count=matched_count,
                    quality=quality,
                    quality_override=quality_override,
                )
            )
        return sorted(
            directories,
            key=self._directory_sort_key,
        )

    def _directory_sort_key(self, directory: DirectoryMatch) -> tuple[object, ...]:
        source_key = self.source_sort_keys.get(
            directory.source_id,
            (0, directory.source_id.casefold(), directory.source_id),
        )
        return (*source_key, _directory_path_sort_key(directory.path))

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
            preferences_revision=self.preferences_revision,
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
                        SearchCorpus(
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
                    preference_adjustment=self._preference_adjustment,
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


def _default_preference_adjustment(asset: SearchCorpusAsset) -> float:
    return asset.source_priority * 0.01


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
        SearchCorpus.from_assets(
            fetch_search_corpus_assets(
                db,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
            )
        ),
        query,
        domain_vocabulary=domain_vocabulary,
        mode=mode,
        preference_adjustment=_default_preference_adjustment,
    )


def _strict_search_page(
    corpus: SearchCorpus,
    query: str,
    *,
    limit: int,
    offset: int,
    preference_adjustment: Callable[[SearchCorpusAsset], float] = _default_preference_adjustment,
    control: SearchControl | None = None,
) -> SearchResultsPage:
    query_tokens = tokenize_search_text(query)
    if not query_tokens:
        return SearchResultsPage(assets=[], total=0)
    if corpus.lexical_index is None:
        raise RuntimeError("strict lexical index is unavailable")

    ranked, total = _strict_ranked_candidates(
        corpus,
        query_tokens,
        preference_adjustment=preference_adjustment,
        control=control,
    )
    return SearchResultsPage(
        assets=[
            _record_to_result(candidate.asset, candidate.score)
            for candidate in ranked[offset : offset + limit]
        ],
        total=total,
    )


def _strict_ranked_candidates(
    corpus: SearchCorpus,
    query_tokens: tuple[str, ...],
    *,
    preference_adjustment: Callable[[SearchCorpusAsset], float],
    control: SearchControl | None = None,
) -> tuple[list[_RankedCandidate], int]:
    if corpus.lexical_index is None:
        raise RuntimeError("strict lexical index is unavailable")
    matches, total = corpus.lexical_index.search_with_scores(
        query_tokens,
        require_all_terms=True,
        limit=corpus.lexical_index.asset_count,
        offset=0,
    )
    ranked = []
    for index, (asset_index, score) in enumerate(matches, start=1):
        if control:
            control.check(index)
        asset = corpus.assets[asset_index]
        ranked.append(
            _RankedCandidate(
                asset=asset,
                score=score + preference_adjustment(asset),
            )
        )
    ranked.sort(
        key=lambda candidate: (
            -candidate.score,
            -candidate.asset.source_priority,
            candidate.asset.relative_path.casefold(),
        )
    )
    return ranked, total


def _strict_match_directories(
    corpus: SearchCorpus,
    query: str,
    *,
    control: SearchControl | None = None,
) -> list[tuple[str, str]]:
    query_tokens = tokenize_search_text(query)
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
    assets: Iterable[SearchCorpusAsset] | SearchCorpus,
    query: str,
    *,
    domain_vocabulary: DomainVocabulary,
    mode: SearchMode,
    preference_adjustment: Callable[[SearchCorpusAsset], float] = _default_preference_adjustment,
    control: SearchControl | None = None,
) -> list[_RankedCandidate]:
    corpus = (
        assets if isinstance(assets, SearchCorpus) else SearchCorpus.from_assets(assets)
    )
    query_tokens = tokenize_search_text(query)
    if not query_tokens:
        return []
    if mode not in {"strict", "fuzzy", "domain", "combined"}:
        raise ValueError(f"unsupported search mode: {mode}")

    if mode == "strict":
        ranked, _total = _strict_ranked_candidates(
            corpus,
            query_tokens,
            preference_adjustment=preference_adjustment,
            control=control,
        )
        return ranked

    candidates: list[_Candidate] = []
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
    return _rank_candidates(
        candidates,
        query_tokens,
        preference_adjustment=preference_adjustment,
        control=control,
    )


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
    preferences_revision: int = 0,
) -> _SearchCacheKey:
    return (
        normalize_search_text(query),
        mode,
        source_id,
        directory,
        directory_prefix,
        tuple(sorted(domain_vocabulary.expansions.items())),
        preferences_revision,
    )


def _domain_candidate_indexes(
    corpus: SearchCorpus,
    expanded_terms: tuple[str, ...],
) -> tuple[int, ...] | range:
    if corpus.lexical_index is None:
        return range(len(corpus.assets))
    candidate_indexes: set[int] = set()
    for term in expanded_terms:
        candidate_indexes.update(corpus.lexical_index.candidate_indexes(tokenize_search_text(term)))
    return tuple(candidate_indexes)


def _filter_corpus(
    corpus: SearchCorpus,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    control: SearchControl | None = None,
) -> SearchCorpus:
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
    return SearchCorpus.from_assets(filtered_assets)


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


def _fetch_folder_quality_overrides(
    db: sqlite3.Connection,
) -> dict[tuple[str, str], int]:
    rows = db.execute(
        """
        SELECT source_id, directory, quality
        FROM folder_quality_overrides
        """
    ).fetchall()
    return {
        (row["source_id"], row["directory"]): row["quality"]
        for row in rows
    }


def upsert_folder_quality(
    db: sqlite3.Connection,
    *,
    source_id: str,
    directory: str,
    quality: int,
) -> None:
    if quality < 0 or quality > 10:
        raise ValueError("folder quality must be between 0 and 10")
    if quality == DEFAULT_FOLDER_QUALITY:
        db.execute(
            """
            DELETE FROM folder_quality_overrides
            WHERE source_id = ? AND directory = ?
            """,
            (source_id, directory),
        )
        return
    db.execute(
        """
        INSERT INTO folder_quality_overrides(source_id, directory, quality, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(source_id, directory) DO UPDATE SET
          quality = excluded.quality,
          updated_at = CURRENT_TIMESTAMP
        """,
        (source_id, directory, quality),
    )


def _asset_indexes_by_directory(
    assets: tuple[SearchCorpusAsset, ...],
) -> dict[tuple[str, str], tuple[int, ...]]:
    indexes: dict[tuple[str, str], list[int]] = {}
    for index, asset in enumerate(assets):
        indexes.setdefault((asset.source_id, asset.directory), []).append(index)
    return {key: tuple(value) for key, value in indexes.items()}


def _preference_adjustments(
    assets: tuple[SearchCorpusAsset, ...],
    folder_quality_overrides: dict[tuple[str, str], int],
) -> tuple[float, ...]:
    return tuple(
        _default_preference_adjustment(asset)
        + _folder_quality_adjustment(
            _effective_folder_quality(
                folder_quality_overrides,
                asset.source_id,
                asset.directory,
            )
        )
        for asset in assets
    )


def _updated_preference_adjustments(
    directory_asset_indexes: dict[tuple[str, str], tuple[int, ...]],
    preference_adjustments: tuple[float, ...],
    current_overrides: dict[tuple[str, str], int],
    next_overrides: dict[tuple[str, str], int],
    *,
    changed_source_id: str,
    changed_directory: str,
) -> tuple[float, ...]:
    current_quality = _effective_folder_quality(
        current_overrides,
        changed_source_id,
        changed_directory,
    )
    next_quality = _effective_folder_quality(
        next_overrides,
        changed_source_id,
        changed_directory,
    )
    if current_quality == next_quality:
        return preference_adjustments

    more_specific_override_paths = tuple(
        path
        for (source_id, path) in next_overrides
        if source_id == changed_source_id
        and path != changed_directory
        and _directory_is_within(path, changed_directory)
    )
    adjustment_delta = _folder_quality_adjustment(
        next_quality
    ) - _folder_quality_adjustment(current_quality)
    next_adjustments = list(preference_adjustments)
    for (source_id, directory), asset_indexes in directory_asset_indexes.items():
        if source_id != changed_source_id or not _directory_is_within(
            directory,
            changed_directory,
        ):
            continue
        if _has_more_specific_folder_quality_override(
            more_specific_override_paths,
            directory,
        ):
            continue
        for asset_index in asset_indexes:
            next_adjustments[asset_index] += adjustment_delta
    return tuple(next_adjustments)


def _effective_folder_quality(
    folder_quality_overrides: dict[tuple[str, str], int],
    source_id: str,
    directory: str,
) -> int:
    for path in _directory_ancestors(directory):
        override = folder_quality_overrides.get((source_id, path))
        if override is not None:
            return override
    return DEFAULT_FOLDER_QUALITY


def _directory_is_within(directory: str, parent: str) -> bool:
    return parent == "" or directory == parent or directory.startswith(f"{parent}/")


def _has_more_specific_folder_quality_override(
    more_specific_override_paths: tuple[str, ...],
    directory: str,
) -> bool:
    for path in more_specific_override_paths:
        if _directory_is_within(directory, path):
            return True
    return False


def _directory_ancestors(directory: str) -> tuple[str, ...]:
    if not directory:
        return ("",)
    ancestors = [directory]
    current = directory
    while "/" in current:
        current = current.rsplit("/", 1)[0]
        ancestors.append(current)
    ancestors.append("")
    return tuple(ancestors)


def _directory_path_sort_key(path: str) -> tuple[str, ...]:
    if not path:
        return ()
    return tuple(part.casefold() for part in path.split("/"))


def _folder_quality_adjustment(quality: int) -> float:
    return (quality - DEFAULT_FOLDER_QUALITY) * FOLDER_QUALITY_WEIGHT


def _strict_candidates(
    corpus_or_assets: Iterable[SearchCorpusAsset] | SearchCorpus,
    query_tokens: tuple[str, ...],
    query: str,
    *,
    require_all_terms: bool,
    control: SearchControl | None = None,
) -> list[_Candidate]:
    corpus = (
        corpus_or_assets
        if isinstance(corpus_or_assets, SearchCorpus)
        else SearchCorpus.from_assets(corpus_or_assets)
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
    corpus: SearchCorpus,
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
    corpus_or_assets: Iterable[SearchCorpusAsset] | SearchCorpus,
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
        if isinstance(corpus_or_assets, SearchCorpus)
        else SearchCorpus.from_assets(corpus_or_assets)
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
    preference_adjustment: Callable[[SearchCorpusAsset], float] = _default_preference_adjustment,
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
        score += preference_adjustment(asset)
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
    asset: SearchCorpusAsset,
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
    asset: SearchCorpusAsset,
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


def _best_strict_token_score(asset: SearchCorpusAsset, token: str) -> float:
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
    corpus: SearchCorpus,
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
    asset: SearchCorpusAsset,
    query_tokens: tuple[str, ...],
    query: str,
    token_scores: tuple[float, ...],
) -> float:
    query_score = float(fuzz.WRatio(normalize_search_text(query), asset.fuzzy_text))
    score = sum(token_scores) + query_score
    if all(token in asset.filename_tokens for token in query_tokens):
        score += 80.0
    return score


def _domain_score(
    asset: SearchCorpusAsset,
    expanded_terms: tuple[str, ...],
) -> tuple[float, tuple[str, ...]]:
    matched = []
    score = 0.0
    for term in expanded_terms:
        term_tokens = tokenize_search_text(term)
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


def _record_to_result(asset: SearchCorpusAsset, score: float) -> SearchResult:
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
        mtime_ns=asset.mtime_ns,
        media_type=asset.media_type,
        thumbnail_path=asset.thumbnail_path,
        thumbnail_width=asset.thumbnail_width,
        thumbnail_height=asset.thumbnail_height,
        score=score,
    )
