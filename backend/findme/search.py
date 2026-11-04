from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import sqlite3
import threading

import numpy as np
from rapidfuzz import fuzz, process
import yaml

from findme.search_corpus import (
    SearchCorpus,
    SearchCorpusAsset,
    SearchCorpusError,
    SearchCorpusSnapshot,
    SearchLexicalIndex,
    ThumbnailVariantMetadata,
    count_asset_directories,
    fetch_search_corpus_assets,
    normalize_search_text,
    search_corpus_is_stale,
    tokenize_search_text,
)


SearchMode = Literal["strict", "fuzzy", "domain"]
DirectoryFilterState = Literal["whitelist", "blacklist"]
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
    media_width: int | None
    media_height: int | None
    duration_seconds: float | None
    thumbnail_variants: tuple[ThumbnailVariantMetadata, ...]
    score: float


@dataclass(frozen=True)
class SearchResultsPage:
    assets: list[SearchResult]
    total: int


@dataclass(frozen=True)
class SearchAssetIndexPage:
    asset_indexes: np.ndarray
    scores: np.ndarray
    total: int


@dataclass(frozen=True)
class DirectoryFilterRule:
    source_id: str
    path: str
    state: DirectoryFilterState


@dataclass(frozen=True)
class DirectoryFilter:
    rules: tuple[DirectoryFilterRule, ...] = ()
    has_whitelist: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "has_whitelist",
            any(rule.state == "whitelist" for rule in self.rules),
        )


@dataclass(frozen=True)
class SearchPreferences:
    preference_adjustment_array: np.ndarray
    directory_filter: DirectoryFilter = field(default_factory=DirectoryFilter)
    included_asset_mask: np.ndarray | None = None
    included_asset_index_order: np.ndarray | None = None
    included_directory_counts: dict[tuple[str, str], int] | None = None
    included_directory_count_array: np.ndarray | None = None
    quality_overrides: dict[tuple[str, str], int] = field(default_factory=dict)
    selection_states: dict[tuple[str, str], DirectoryFilterState] = field(default_factory=dict)
    effective_selection_states: dict[tuple[str, str], DirectoryFilterState] = field(default_factory=dict)
    settings_fingerprint: str = "empty"

    def preference_adjustment(self, asset: SearchCorpusAsset) -> float:
        return float(self.preference_adjustment_array[asset.asset_index])

    def includes(self, asset: SearchCorpusAsset) -> bool:
        return (
            self.included_asset_mask is None
            or bool(self.included_asset_mask[asset.asset_index])
        )

    def folder_quality(self, source_id: str, directory: str) -> tuple[int, int | None]:
        override = self.quality_overrides.get((source_id, directory))
        if not self.quality_overrides:
            return DEFAULT_FOLDER_QUALITY, None
        return (
            _effective_folder_quality(
                self.quality_overrides,
                source_id,
                directory,
            ),
            override,
        )

    def selection_state(self, source_id: str, directory: str) -> DirectoryFilterState | None:
        return self.selection_states.get((source_id, directory))

    def selection_effective(self, source_id: str, directory: str) -> bool:
        state = self.selection_states.get((source_id, directory))
        if state is None:
            return True
        return self.effective_selection_states.get((source_id, directory)) == state


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
class _SearchMatchArrays:
    asset_indexes: np.ndarray
    scores: np.ndarray
    total: int


@dataclass(frozen=True)
class _DirectoryRuntime:
    keys: tuple[tuple[str, str], ...]
    names: tuple[str, ...]
    depths: np.ndarray
    asset_counts: np.ndarray
    parent_ids: np.ndarray
    rollup_order: np.ndarray
    sort_order: np.ndarray
    id_by_key: dict[tuple[str, str], int]


@dataclass(frozen=True)
class SearchWithDirectoryResponses:
    page: SearchResultsPage
    directories: list[dict[str, object]]


@dataclass
class _InFlightStrictSearch:
    done: threading.Event = field(default_factory=threading.Event)
    result: _SearchMatchArrays | None = None
    error: BaseException | None = None


@dataclass
class SearchIndex:
    assets: tuple[SearchCorpusAsset, ...]
    directory_counts: dict[tuple[str, str], int]
    directory_subtree_asset_indexes: dict[tuple[str, str], np.ndarray]
    asset_directory_keys: tuple[tuple[tuple[str, str], ...], ...]
    asset_directory_ids: np.ndarray
    asset_path_sort_ranks: np.ndarray
    directory_runtime: _DirectoryRuntime
    source_display_names: dict[str, str]
    source_sort_keys: dict[str, tuple[int, str, str]]
    fuzzy_tokens: tuple[str, ...] = ()
    fuzzy_token_asset_indexes: tuple[tuple[int, ...], ...] = ()
    lexical_index: "SearchLexicalIndex | None" = None
    cache_size: int = 128
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)
    _strict_match_cache: OrderedDict[tuple[str, ...], _SearchMatchArrays] = field(
        default_factory=OrderedDict
    )
    _strict_in_flight: dict[tuple[str, ...], _InFlightStrictSearch] = field(default_factory=dict)

    @classmethod
    def load(cls, db: sqlite3.Connection) -> "SearchIndex":
        assets = fetch_search_corpus_assets(
            db,
            source_id=None,
            directory=None,
            directory_prefix=None,
        )
        corpus = SearchCorpus.from_assets(assets)
        directory_counts = count_asset_directories(
            [(asset.source_id, asset.directory) for asset in assets]
        )
        source_display_names = {
            asset.source_id: asset.source_display_name for asset in assets
        }
        source_sort_keys = {
            asset.source_id: (
                -asset.source_priority,
                asset.source_display_name.casefold(),
                asset.source_id,
            )
            for asset in assets
        }
        directory_runtime = _directory_runtime(
            directory_counts,
            source_display_names=source_display_names,
            source_sort_keys=source_sort_keys,
        )
        return cls(
            assets=assets,
            directory_counts=directory_counts,
            directory_subtree_asset_indexes=_asset_indexes_by_directory_subtree(assets),
            asset_directory_keys=_asset_directory_keys(assets),
            asset_directory_ids=_asset_directory_ids(assets, directory_runtime.id_by_key),
            asset_path_sort_ranks=_asset_path_sort_ranks(assets),
            directory_runtime=directory_runtime,
            source_display_names=source_display_names,
            source_sort_keys=source_sort_keys,
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
        if search_corpus_is_stale(db):
            raise SearchCorpusError(
                "search corpus is stale; run findme rebuild-search-corpus --config <config>"
            )
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
        directory_runtime = _directory_runtime(
            snapshot.directory_counts,
            source_display_names=snapshot.source_display_names,
            source_sort_keys=snapshot.source_sort_keys,
        )
        return cls(
            assets=snapshot.assets,
            directory_counts=snapshot.directory_counts,
            directory_subtree_asset_indexes=_asset_indexes_by_directory_subtree(
                snapshot.assets
            ),
            asset_directory_keys=_asset_directory_keys(snapshot.assets),
            asset_directory_ids=_asset_directory_ids(
                snapshot.assets,
                directory_runtime.id_by_key,
            ),
            asset_path_sort_ranks=_asset_path_sort_ranks(snapshot.assets),
            directory_runtime=directory_runtime,
            source_display_names=snapshot.source_display_names,
            source_sort_keys=snapshot.source_sort_keys,
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
        preferences: SearchPreferences | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "strict",
        limit: int = 100,
        offset: int = 0,
        control: SearchControl | None = None,
    ) -> SearchResultsPage:
        domain_vocabulary = domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY
        if mode not in {"strict", "fuzzy", "domain"}:
            raise ValueError(f"unsupported search mode: {mode}")
        preferences = preferences or empty_search_preferences(self.assets)
        return self._search_page_from_numeric_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            limit=limit,
            offset=offset,
            control=control,
        )

    def search_asset_index_page(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "strict",
        limit: int = 100,
        offset: int = 0,
        control: SearchControl | None = None,
    ) -> SearchAssetIndexPage:
        preferences = preferences or empty_search_preferences(self.assets)
        domain_vocabulary = domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY
        matches = self._search_match_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            control=control,
        )
        asset_indexes, scores = self._apply_preferences_to_match_arrays(
            matches.asset_indexes,
            matches.scores,
            preferences=preferences,
        )
        page_indexes, page_scores = self._top_page_indexes(
            asset_indexes,
            scores,
            limit=limit,
            offset=offset,
        )
        return SearchAssetIndexPage(
            asset_indexes=page_indexes,
            scores=page_scores,
            total=len(asset_indexes),
        )

    def browse(
        self,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        limit: int = 100,
        offset: int = 0,
        control: SearchControl | None = None,
    ) -> SearchResultsPage:
        preferences = preferences or empty_search_preferences(self.assets)
        results = []
        total = 0
        end = offset + limit
        for asset in self._browse_assets(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            control=control,
        ):
            if offset <= total < end:
                results.append(_record_to_result(asset, 0))
            total += 1
        return SearchResultsPage(assets=results, total=total)

    def browse_asset_indexes(
        self,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        control: SearchControl | None = None,
    ) -> Iterable[int]:
        preferences = preferences or empty_search_preferences(self.assets)
        for asset in self._browse_assets(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            control=control,
        ):
            yield asset.asset_index

    def browse_asset_index_array(
        self,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        control: SearchControl | None = None,
    ) -> np.ndarray:
        preferences = preferences or empty_search_preferences(self.assets)
        if control and control.is_cancelled():
            raise SearchSuperseded

        if source_id is None and directory is None and directory_prefix is None:
            if preferences.included_asset_index_order is not None:
                indexes = preferences.included_asset_index_order
            else:
                indexes = np.arange(len(self.assets), dtype=np.int32)
            if preferences.included_asset_mask is not None:
                indexes = indexes[preferences.included_asset_mask[indexes]]
            return indexes

        if source_id is not None and directory is None:
            indexes = self.directory_subtree_asset_indexes.get(
                (source_id, directory_prefix or ""),
                _EMPTY_ASSET_INDEXES,
            )
        elif source_id is not None and directory is not None:
            directory_id = self.directory_runtime.id_by_key.get((source_id, directory))
            indexes = (
                np.empty(0, dtype=np.int32)
                if directory_id is None
                else np.flatnonzero(self.asset_directory_ids == directory_id).astype(
                    np.int32,
                    copy=False,
                )
            )
        else:
            indexes = np.fromiter(
                self.browse_asset_indexes(
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    preferences=empty_search_preferences(self.assets),
                    control=control,
                ),
                dtype=np.int32,
            )

        if preferences.included_asset_mask is not None:
            indexes = indexes[preferences.included_asset_mask[indexes]]
        if control and control.is_cancelled():
            raise SearchSuperseded
        return indexes

    def search_asset_index_array(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "strict",
        control: SearchControl | None = None,
    ) -> np.ndarray:
        preferences = preferences or empty_search_preferences(self.assets)
        domain_vocabulary = domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY
        if not query.strip():
            return self.browse_asset_index_array(
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=preferences,
                control=control,
            )
        matches = self._search_match_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            control=control,
        )
        asset_indexes, _scores = self._apply_preferences_to_match_arrays(
            matches.asset_indexes,
            matches.scores,
            preferences=preferences,
        )
        return asset_indexes

    def compile_preferences(
        self,
        *,
        directory_filter: DirectoryFilter | None = None,
        display_directory_filter: DirectoryFilter | None = None,
        quality_overrides: dict[tuple[str, str], int] | None = None,
    ) -> SearchPreferences:
        directory_filter = directory_filter or DirectoryFilter()
        display_directory_filter = display_directory_filter or directory_filter
        quality_overrides = quality_overrides or {}
        effective_directory_filter = _bottommost_whitelist_filter(directory_filter)
        effective_display_directory_filter = _bottommost_whitelist_filter(
            display_directory_filter
        )
        included_asset_index_order = self._included_asset_index_order(
            effective_directory_filter
        )
        included_asset_mask = (
            None
            if included_asset_index_order is None
            else _asset_selection_mask(len(self.assets), included_asset_index_order)
        )
        preference_adjustment_values = self._preference_adjustments(quality_overrides)
        included_directory_counts = self._included_directory_counts(
            effective_directory_filter
        )
        return SearchPreferences(
            preference_adjustment_array=preference_adjustment_values,
            directory_filter=directory_filter,
            included_asset_mask=included_asset_mask,
            included_asset_index_order=included_asset_index_order,
            included_directory_counts=included_directory_counts,
            included_directory_count_array=self._directory_count_array(
                included_directory_counts
            ),
            quality_overrides=quality_overrides,
            selection_states={
                (rule.source_id, rule.path): rule.state
                for rule in display_directory_filter.rules
            },
            effective_selection_states={
                (rule.source_id, rule.path): rule.state
                for rule in effective_display_directory_filter.rules
            },
            settings_fingerprint=folder_settings_fingerprint(
                directory_filter,
                quality_overrides,
            ),
        )

    def _preference_adjustments(
        self,
        quality_overrides: dict[tuple[str, str], int],
    ) -> np.ndarray:
        if not quality_overrides:
            return np.zeros(len(self.assets), dtype=np.float64)

        adjustments = np.zeros(len(self.assets), dtype=np.float64)
        for (source_id, directory), quality in sorted(
            quality_overrides.items(),
            key=lambda item: (
                0 if item[0][1] == "" else len(item[0][1].split("/")),
                item[0][0],
                item[0][1],
            ),
        ):
            adjustments[
                self.directory_subtree_asset_indexes.get(
                    (source_id, directory),
                    _EMPTY_ASSET_INDEXES,
                )
            ] = _folder_quality_adjustment(quality)
        return adjustments

    def _included_asset_index_order(
        self,
        directory_filter: DirectoryFilter,
    ) -> np.ndarray | None:
        if not directory_filter.rules:
            return None
        if (
            len(directory_filter.rules) == 1
            and directory_filter.rules[0].state == "whitelist"
        ):
            rule = directory_filter.rules[0]
            return self.directory_subtree_asset_indexes.get(
                (rule.source_id, rule.path),
                _EMPTY_ASSET_INDEXES,
            )

        included = np.full(
            len(self.assets),
            not directory_filter.has_whitelist,
            dtype=bool,
        )
        for rule in sorted(
            directory_filter.rules,
            key=lambda item: (
                0 if item.path == "" else len(item.path.split("/")),
                item.source_id,
                item.path,
            ),
        ):
            included[
                self.directory_subtree_asset_indexes.get(
                    (rule.source_id, rule.path),
                    _EMPTY_ASSET_INDEXES,
                )
            ] = rule.state == "whitelist"
        return _read_only_indexes(np.flatnonzero(included))

    def _included_directory_counts(
        self,
        directory_filter: DirectoryFilter,
    ) -> dict[tuple[str, str], int] | None:
        if not directory_filter.rules:
            return None

        included_counts = (
            {}
            if directory_filter.has_whitelist
            else dict(self.directory_counts)
        )
        for rule in sorted(
            directory_filter.rules,
            key=lambda item: (
                0 if item.path == "" else len(item.path.split("/")),
                item.source_id,
                item.path,
            ),
        ):
            delta = 1 if rule.state == "whitelist" else -1
            self._apply_directory_subtree_count_delta(
                included_counts,
                source_id=rule.source_id,
                directory=rule.path,
                delta=delta,
            )
        return {key: count for key, count in included_counts.items() if count > 0}

    def _directory_count_array(
        self,
        counts: dict[tuple[str, str], int] | None,
    ) -> np.ndarray | None:
        if counts is None:
            return None
        values = np.zeros(len(self.directory_runtime.keys), dtype=np.int32)
        for key, count in counts.items():
            directory_id = self.directory_runtime.id_by_key.get(key)
            if directory_id is not None:
                values[directory_id] = count
        return values

    def _apply_directory_subtree_count_delta(
        self,
        counts: dict[tuple[str, str], int],
        *,
        source_id: str,
        directory: str,
        delta: int,
    ) -> None:
        subtree_total = self.directory_counts.get((source_id, directory), 0)
        if subtree_total == 0:
            return

        for ancestor in _directory_ancestors(directory):
            if ancestor == directory:
                continue
            key = (source_id, ancestor)
            counts[key] = counts.get(key, 0) + (delta * subtree_total)

        for (candidate_source, candidate_path), count in self.directory_counts.items():
            if candidate_source != source_id:
                continue
            if not _directory_is_within(candidate_path, directory):
                continue
            key = (candidate_source, candidate_path)
            counts[key] = counts.get(key, 0) + (delta * count)

    def directory_responses_for_scope(
        self,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        match_asset_mask: np.ndarray | None = None,
        dirty_folder_subtree_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_whitelist_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_blacklist_keys: frozenset[tuple[str, str]] = frozenset(),
        control: SearchControl | None = None,
    ) -> list[dict[str, object]]:
        preferences = preferences or empty_search_preferences(self.assets)
        if match_asset_mask is not None:
            match_asset_indexes = self._asset_index_array_for_scope(
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=empty_search_preferences(self.assets),
                control=control,
            )
            match_asset_indexes = match_asset_indexes[
                match_asset_mask[match_asset_indexes]
            ]
            match_counts = self._directory_count_array_for_asset_indexes(
                match_asset_indexes
            )
            included_counts = (
                match_counts
                if preferences.included_asset_mask is None
                else self._directory_count_array_for_asset_indexes(
                    self._asset_index_array_for_scope(
                        source_id=source_id,
                        directory=directory,
                        directory_prefix=directory_prefix,
                        preferences=preferences,
                        control=control,
                    )
                )
            )
            return self._directory_responses_from_count_arrays(
                match_counts,
                included_counts,
                preferences=preferences,
                dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                descendant_whitelist_keys=descendant_whitelist_keys,
                descendant_blacklist_keys=descendant_blacklist_keys,
            )
        if directory is None and directory_prefix is None:
            match_counts = (
                self.directory_runtime.asset_counts
                if source_id is None
                else self._directory_count_array(
                    _filter_directory_counts(self.directory_counts, source_id)
                )
            )
            if match_counts is None:
                match_counts = np.zeros(len(self.directory_runtime.keys), dtype=np.int32)
            included_counts = (
                preferences.included_directory_count_array
                if preferences.included_directory_count_array is not None
                else match_counts
            )
            if source_id is not None and preferences.included_directory_counts is not None:
                included_counts = self._directory_count_array(
                    _filter_directory_counts(
                        preferences.included_directory_counts,
                        source_id,
                    )
                )
                if included_counts is None:
                    included_counts = np.zeros(
                        len(self.directory_runtime.keys),
                        dtype=np.int32,
                    )
            return self._directory_responses_from_count_arrays(
                match_counts,
                included_counts,
                preferences=preferences,
                dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                descendant_whitelist_keys=descendant_whitelist_keys,
                descendant_blacklist_keys=descendant_blacklist_keys,
            )

        match_asset_indexes = self._asset_index_array_for_scope(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=empty_search_preferences(self.assets),
            control=control,
        )
        match_counts = self._directory_count_array_for_asset_indexes(match_asset_indexes)
        included_counts = (
            match_counts
            if preferences.included_asset_mask is None
            else self._directory_count_array_for_asset_indexes(
                self._asset_index_array_for_scope(
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    preferences=preferences,
                    control=control,
                )
            )
        )
        return self._directory_responses_from_count_arrays(
            match_counts,
            included_counts,
            preferences=preferences,
            dirty_folder_subtree_keys=dirty_folder_subtree_keys,
            descendant_whitelist_keys=descendant_whitelist_keys,
            descendant_blacklist_keys=descendant_blacklist_keys,
        )

    def _asset_index_array_for_scope(
        self,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        preferences: SearchPreferences,
        control: SearchControl | None = None,
    ) -> np.ndarray:
        return self.browse_asset_index_array(
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            control=control,
        )

    def directory_responses_for_search(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        match_asset_mask: np.ndarray | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "strict",
        dirty_folder_subtree_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_whitelist_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_blacklist_keys: frozenset[tuple[str, str]] = frozenset(),
        control: SearchControl | None = None,
    ) -> list[dict[str, object]]:
        preferences = preferences or empty_search_preferences(self.assets)
        match_counts, included_counts = self._directory_count_arrays_for_numeric_search(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            preferences=preferences,
            match_asset_mask=match_asset_mask,
            domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
            mode=mode,
            control=control,
        )
        return self._directory_responses_from_count_arrays(
            match_counts,
            included_counts,
            preferences=preferences,
            dirty_folder_subtree_keys=dirty_folder_subtree_keys,
            descendant_whitelist_keys=descendant_whitelist_keys,
            descendant_blacklist_keys=descendant_blacklist_keys,
        )

    def search_with_directory_responses(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences | None = None,
        domain_vocabulary: DomainVocabulary | None = None,
        mode: SearchMode = "strict",
        limit: int = 100,
        offset: int = 0,
        dirty_folder_subtree_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_whitelist_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_blacklist_keys: frozenset[tuple[str, str]] = frozenset(),
        control: SearchControl | None = None,
    ) -> SearchWithDirectoryResponses:
        preferences = preferences or empty_search_preferences(self.assets)
        matches = self._search_match_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
            mode=mode,
            control=control,
        )
        asset_indexes, scores = self._apply_preferences_to_match_arrays(
            matches.asset_indexes,
            matches.scores,
            preferences=preferences,
        )
        page_indexes, page_scores = self._top_page_indexes(
            asset_indexes,
            scores,
            limit=limit,
            offset=offset,
        )
        match_counts = self._directory_count_array_for_asset_indexes(
            matches.asset_indexes
        )
        selection = preferences.included_asset_mask
        included_counts = (
            match_counts
            if selection is None
            else self._directory_count_array_for_asset_indexes(asset_indexes)
        )
        return SearchWithDirectoryResponses(
            page=SearchResultsPage(
                assets=[
                    _record_to_result(self.assets[int(asset_index)], float(score))
                    for asset_index, score in zip(page_indexes, page_scores, strict=True)
                ],
                total=len(asset_indexes),
            ),
            directories=self._directory_responses_from_count_arrays(
                match_counts,
                included_counts,
                preferences=preferences,
                dirty_folder_subtree_keys=dirty_folder_subtree_keys,
                descendant_whitelist_keys=descendant_whitelist_keys,
                descendant_blacklist_keys=descendant_blacklist_keys,
            ),
        )

    def _browse_assets(
        self,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        preferences: SearchPreferences,
        control: SearchControl | None = None,
    ) -> Iterable[SearchCorpusAsset]:
        asset_indexes = preferences.included_asset_index_order
        assets = (
            self.assets
            if asset_indexes is None
            else (self.assets[asset_index] for asset_index in asset_indexes)
        )
        scope_preferences = None if asset_indexes is not None else preferences
        for index, asset in enumerate(assets, start=1):
            if control:
                control.check(index)
            if not _asset_in_scope(
                asset,
                source_id=source_id,
                directory=directory,
                directory_prefix=directory_prefix,
                preferences=scope_preferences,
            ):
                continue
            yield asset

    def _search_page_from_numeric_arrays(
        self,
        query: str,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        preferences: SearchPreferences,
        domain_vocabulary: DomainVocabulary,
        mode: SearchMode,
        limit: int,
        offset: int,
        control: SearchControl | None = None,
    ) -> SearchResultsPage:
        matches = self._search_match_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            control=control,
        )
        asset_indexes, scores = self._apply_preferences_to_match_arrays(
            matches.asset_indexes,
            matches.scores,
            preferences=preferences,
        )
        page_indexes, page_scores = self._top_page_indexes(
            asset_indexes,
            scores,
            limit=limit,
            offset=offset,
        )
        return SearchResultsPage(
            assets=[
                _record_to_result(self.assets[int(asset_index)], float(score))
                for asset_index, score in zip(page_indexes, page_scores, strict=True)
            ],
            total=len(asset_indexes),
        )

    def _directory_count_arrays_for_numeric_search(
        self,
        query: str,
        *,
        source_id: str | None = None,
        directory: str | None = None,
        directory_prefix: str | None = None,
        preferences: SearchPreferences,
        match_asset_mask: np.ndarray | None = None,
        domain_vocabulary: DomainVocabulary,
        mode: SearchMode,
        control: SearchControl | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        matches = self._search_match_arrays(
            query,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
            domain_vocabulary=domain_vocabulary,
            mode=mode,
            control=control,
        )
        match_asset_indexes = matches.asset_indexes
        if match_asset_mask is not None:
            match_asset_indexes = match_asset_indexes[
                match_asset_mask[match_asset_indexes]
            ]
        match_counts = self._directory_count_array_for_asset_indexes(
            match_asset_indexes
        )
        selection = preferences.included_asset_mask
        included_indexes = (
            match_asset_indexes
            if selection is None
            else match_asset_indexes[selection[match_asset_indexes]]
        )
        included_counts = (
            match_counts
            if selection is None
            else self._directory_count_array_for_asset_indexes(included_indexes)
        )
        return match_counts, included_counts

    def _search_match_arrays(
        self,
        query: str,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
        domain_vocabulary: DomainVocabulary,
        mode: SearchMode,
        control: SearchControl | None = None,
    ) -> _SearchMatchArrays:
        query_tokens = tokenize_search_text(query)
        if not query_tokens:
            matches = _empty_search_match_arrays()
        elif mode == "strict":
            matches = self._strict_match_arrays(query_tokens, control=control)
        elif mode == "fuzzy":
            matches = self._fuzzy_match_arrays(query_tokens, query, control=control)
        elif mode == "domain":
            matches = self._domain_match_arrays(
                query_tokens,
                query,
                domain_vocabulary,
                control=control,
            )
        else:
            raise ValueError(f"unsupported search mode: {mode}")
        return self._apply_request_scope_to_match_arrays(
            matches,
            source_id=source_id,
            directory=directory,
            directory_prefix=directory_prefix,
        )

    def _apply_request_scope_to_match_arrays(
        self,
        matches: _SearchMatchArrays,
        *,
        source_id: str | None,
        directory: str | None,
        directory_prefix: str | None,
    ) -> _SearchMatchArrays:
        if source_id is None and directory is None and directory_prefix is None:
            return matches
        keep = np.fromiter(
            (
                _asset_in_scope(
                    self.assets[int(asset_index)],
                    source_id=source_id,
                    directory=directory,
                    directory_prefix=directory_prefix,
                    preferences=None,
                )
                for asset_index in matches.asset_indexes
            ),
            dtype=bool,
            count=len(matches.asset_indexes),
        )
        return _SearchMatchArrays(
            asset_indexes=matches.asset_indexes[keep],
            scores=matches.scores[keep],
            total=int(np.count_nonzero(keep)),
        )

    def _fuzzy_match_arrays(
        self,
        query_tokens: tuple[str, ...],
        query: str,
        *,
        control: SearchControl | None = None,
    ) -> _SearchMatchArrays:
        corpus = SearchCorpus(
            self.assets,
            self.fuzzy_tokens,
            self.fuzzy_token_asset_indexes,
            self.lexical_index,
        )
        token_matches = _fuzzy_token_matches(corpus, query_tokens)
        if not token_matches:
            return _empty_search_match_arrays()

        matching_indexes = set(token_matches[0])
        for matches in token_matches[1:]:
            matching_indexes.intersection_update(matches)
            if not matching_indexes:
                return _empty_search_match_arrays()

        raw_matches = []
        for index, asset_index in enumerate(matching_indexes, start=1):
            if control:
                control.check(index)
            asset = self.assets[asset_index]
            raw_score = _fuzzy_raw_score(
                asset,
                query_tokens,
                query,
                tuple(
                    token_matches_by_index[asset_index]
                    for token_matches_by_index in token_matches
                ),
            )
            raw_matches.append((asset_index, raw_score))
        return self._lane_match_arrays("fuzzy", raw_matches)

    def _domain_match_arrays(
        self,
        query_tokens: tuple[str, ...],
        query: str,
        domain_vocabulary: DomainVocabulary,
        *,
        control: SearchControl | None = None,
    ) -> _SearchMatchArrays:
        del query
        expanded_terms = _expanded_terms(query_tokens, domain_vocabulary)
        if not expanded_terms:
            return _empty_search_match_arrays()
        corpus = SearchCorpus(
            self.assets,
            self.fuzzy_tokens,
            self.fuzzy_token_asset_indexes,
            self.lexical_index,
        )
        raw_matches = []
        candidate_indexes = _domain_candidate_indexes(corpus, expanded_terms)
        tokenized_terms = tuple(tokenize_search_text(term) for term in expanded_terms)
        for index, asset_index in enumerate(candidate_indexes, start=1):
            if control:
                control.check(index)
            asset = self.assets[asset_index]
            raw_score, _matched = _domain_score_from_tokenized_terms(
                asset,
                tokenized_terms,
            )
            if raw_score <= 0:
                continue
            raw_matches.append((asset_index, raw_score))
        return self._lane_match_arrays("domain", raw_matches)

    def _lane_match_arrays(
        self,
        lane: Literal["fuzzy", "domain"],
        raw_matches: list[tuple[int, float]],
    ) -> _SearchMatchArrays:
        if not raw_matches:
            return _empty_search_match_arrays()
        raw_matches.sort(
            key=lambda item: (
                -item[1],
                self.assets[item[0]].relative_path.casefold(),
            )
        )
        return _SearchMatchArrays(
            asset_indexes=np.fromiter(
                (asset_index for asset_index, _score in raw_matches),
                dtype=np.int32,
                count=len(raw_matches),
            ),
            scores=np.fromiter(
                (
                    _lane_score(lane, raw_score, rank)
                    for rank, (_asset_index, raw_score) in enumerate(raw_matches, start=1)
                ),
                dtype=np.float64,
                count=len(raw_matches),
            ),
            total=len(raw_matches),
        )

    def _strict_match_arrays(
        self,
        query_tokens: tuple[str, ...],
        *,
        control: SearchControl | None = None,
    ) -> _SearchMatchArrays:
        if control and control.is_cancelled():
            raise SearchSuperseded
        cached = self._strict_match_cache_get(query_tokens)
        if cached is not None:
            return cached
        owner = False
        with self._cache_lock:
            cached = self._strict_match_cache.get(query_tokens)
            if cached is not None:
                self._strict_match_cache.move_to_end(query_tokens)
                return cached
            in_flight = self._strict_in_flight.get(query_tokens)
            if in_flight is None:
                in_flight = _InFlightStrictSearch()
                self._strict_in_flight[query_tokens] = in_flight
                owner = True

        if not owner:
            return self._wait_for_strict_in_flight(query_tokens, in_flight, control)

        try:
            if self.lexical_index is None:
                if not self.assets:
                    matches = _SearchMatchArrays(
                        asset_indexes=np.empty(0, dtype=np.int32),
                        scores=np.empty(0, dtype=np.float64),
                        total=0,
                    )
                else:
                    raise RuntimeError("strict lexical index is unavailable")
            else:
                raw_matches, total = self.lexical_index.search_with_scores(
                    query_tokens,
                    require_all_terms=True,
                    limit=self.lexical_index.asset_count,
                    offset=0,
                )
                if control and control.is_cancelled():
                    raise SearchSuperseded
                matches = _SearchMatchArrays(
                    asset_indexes=np.fromiter(
                        (asset_index for asset_index, _score in raw_matches),
                        dtype=np.int32,
                        count=len(raw_matches),
                    ),
                    scores=np.fromiter(
                        (score for _asset_index, score in raw_matches),
                        dtype=np.float64,
                        count=len(raw_matches),
                    ),
                    total=total,
                )
        except BaseException as exc:
            with self._cache_lock:
                in_flight.error = exc
                self._strict_in_flight.pop(query_tokens, None)
                in_flight.done.set()
            raise
        with self._cache_lock:
            self._strict_match_cache[query_tokens] = matches
            self._strict_match_cache.move_to_end(query_tokens)
            while len(self._strict_match_cache) > self.cache_size:
                self._strict_match_cache.popitem(last=False)
            in_flight.result = matches
            self._strict_in_flight.pop(query_tokens, None)
            in_flight.done.set()
        return matches

    def _wait_for_strict_in_flight(
        self,
        query_tokens: tuple[str, ...],
        in_flight: _InFlightStrictSearch,
        control: SearchControl | None,
    ) -> _SearchMatchArrays:
        while not in_flight.done.wait(0.025):
            if control and control.is_cancelled():
                raise SearchSuperseded
        if in_flight.error is not None:
            if control and control.is_cancelled():
                raise SearchSuperseded
            cached = self._strict_match_cache_get(query_tokens)
            if cached is not None:
                return cached
            if isinstance(in_flight.error, SearchSuperseded):
                return self._strict_match_arrays(query_tokens, control=control)
            raise in_flight.error
        if in_flight.result is None:
            raise RuntimeError("strict search finished without a result")
        return in_flight.result

    def _strict_match_cache_get(
        self,
        query_tokens: tuple[str, ...],
    ) -> _SearchMatchArrays | None:
        with self._cache_lock:
            cached = self._strict_match_cache.get(query_tokens)
            if cached is None:
                return None
            self._strict_match_cache.move_to_end(query_tokens)
            return cached

    def _apply_preferences_to_match_arrays(
        self,
        asset_indexes: np.ndarray,
        scores: np.ndarray,
        *,
        preferences: SearchPreferences,
    ) -> tuple[np.ndarray, np.ndarray]:
        selection = preferences.included_asset_mask
        if selection is not None:
            included = selection[asset_indexes]
            asset_indexes = asset_indexes[included]
            scores = scores[included]
        if preferences.quality_overrides:
            scores = scores + preferences.preference_adjustment_array[asset_indexes]
        return asset_indexes, scores

    def _top_page_indexes(
        self,
        asset_indexes: np.ndarray,
        scores: np.ndarray,
        *,
        limit: int,
        offset: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        total = len(asset_indexes)
        if total == 0 or offset >= total:
            return (
                np.empty(0, dtype=np.int32),
                np.empty(0, dtype=np.float64),
            )
        page_end = min(total, offset + limit)
        if page_end < total:
            threshold = np.partition(scores, total - page_end)[total - page_end]
            candidate_positions = np.flatnonzero(scores >= threshold)
        else:
            candidate_positions = np.arange(total, dtype=np.int32)
        candidate_indexes = asset_indexes[candidate_positions]
        candidate_scores = scores[candidate_positions]
        order = np.lexsort(
            (
                self.asset_path_sort_ranks[candidate_indexes],
                -candidate_scores,
            )
        )
        page_positions = candidate_positions[order][offset:page_end]
        return asset_indexes[page_positions], scores[page_positions]

    def _directory_count_array_for_asset_indexes(
        self,
        asset_indexes: np.ndarray,
    ) -> np.ndarray:
        exact_counts = np.bincount(
            self.asset_directory_ids[asset_indexes],
            minlength=len(self.directory_runtime.keys),
        ).astype(np.int32, copy=False)
        return self._rollup_directory_counts(exact_counts)

    def _rollup_directory_counts(self, exact_counts: np.ndarray) -> np.ndarray:
        counts = exact_counts.copy()
        for directory_id in self.directory_runtime.rollup_order:
            parent_id = self.directory_runtime.parent_ids[directory_id]
            if parent_id >= 0:
                counts[parent_id] += counts[directory_id]
        return counts

    def _directory_responses_from_count_arrays(
        self,
        match_counts: np.ndarray,
        included_counts: np.ndarray,
        *,
        preferences: SearchPreferences,
        dirty_folder_subtree_keys: frozenset[tuple[str, str]],
        descendant_whitelist_keys: frozenset[tuple[str, str]] = frozenset(),
        descendant_blacklist_keys: frozenset[tuple[str, str]] = frozenset(),
    ) -> list[dict[str, object]]:
        responses = []
        runtime = self.directory_runtime
        for directory_id in runtime.sort_order:
            matched_count = int(match_counts[directory_id])
            if matched_count == 0:
                continue
            source_id, path = runtime.keys[directory_id]
            directory_key = (source_id, path)
            quality, quality_override = preferences.folder_quality(source_id, path)
            responses.append(
                {
                    "id": int(directory_id),
                    "source_id": source_id,
                    "path": path,
                    "name": runtime.names[directory_id],
                    "depth": int(runtime.depths[directory_id]),
                    "asset_count": int(runtime.asset_counts[directory_id]),
                    "matched_asset_count": matched_count,
                    "included_asset_count": int(included_counts[directory_id]),
                    "quality": quality,
                    "quality_override": quality_override,
                    "selection_state": preferences.selection_state(source_id, path),
                    "selection_effective": preferences.selection_effective(source_id, path),
                    "profile_dirty": directory_key in dirty_folder_subtree_keys,
                    "has_descendant_whitelist": directory_key in descendant_whitelist_keys,
                    "has_descendant_blacklist": directory_key in descendant_blacklist_keys,
                }
            )
        return responses

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


def empty_search_preferences(assets: tuple[SearchCorpusAsset, ...]) -> SearchPreferences:
    return SearchPreferences(
        preference_adjustment_array=np.zeros(len(assets), dtype=np.float64),
        directory_filter=DirectoryFilter(),
        included_asset_mask=None,
        included_asset_index_order=None,
        included_directory_counts=None,
        included_directory_count_array=None,
        quality_overrides={},
        selection_states={},
        effective_selection_states={},
        settings_fingerprint="empty",
    )


def _empty_search_match_arrays() -> _SearchMatchArrays:
    return _SearchMatchArrays(
        asset_indexes=np.empty(0, dtype=np.int32),
        scores=np.empty(0, dtype=np.float64),
        total=0,
    )


def folder_settings_fingerprint(
    directory_filter: DirectoryFilter,
    quality_overrides: dict[tuple[str, str], int],
) -> str:
    quality_part = tuple(
        sorted(
            (source_id, directory, quality)
            for (source_id, directory), quality in quality_overrides.items()
            if quality != DEFAULT_FOLDER_QUALITY
        )
    )
    selection_part = tuple(
        sorted(
            (rule.source_id, rule.path, rule.state)
            for rule in directory_filter.rules
        )
    )
    if not quality_part and not selection_part:
        return "empty"
    return repr((quality_part, selection_part))


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


def _asset_in_scope(
    asset: SearchCorpusAsset,
    *,
    source_id: str | None,
    directory: str | None,
    directory_prefix: str | None,
    preferences: SearchPreferences | None,
) -> bool:
    if source_id and asset.source_id != source_id:
        return False
    if directory is not None and asset.directory != directory:
        return False
    if directory_prefix and not (
        asset.directory == directory_prefix
        or asset.directory.startswith(f"{directory_prefix}/")
    ):
        return False
    if preferences and not preferences.includes(asset):
        return False
    return True


def includes_asset(
    source_id: str,
    directory: str,
    directory_filter: DirectoryFilter,
) -> bool:
    if not directory_filter.rules:
        return True
    rules_by_node = {
        (rule.source_id, rule.path): rule.state for rule in directory_filter.rules
    }
    nearest = None
    for ancestor in reversed(_directory_ancestors(directory)):
        state = rules_by_node.get((source_id, ancestor))
        if state is not None:
            nearest = state
    if nearest == "whitelist":
        return True
    if nearest == "blacklist":
        return False
    return not directory_filter.has_whitelist


def _bottommost_whitelist_filter(directory_filter: DirectoryFilter) -> DirectoryFilter:
    if not directory_filter.has_whitelist:
        return directory_filter
    rules = []
    whitelist_rules = [
        rule for rule in directory_filter.rules if rule.state == "whitelist"
    ]
    for rule in directory_filter.rules:
        if rule.state != "whitelist":
            rules.append(rule)
            continue
        has_descendant_whitelist = any(
            other.source_id == rule.source_id
            and other.path != rule.path
            and _directory_is_within(other.path, rule.path)
            for other in whitelist_rules
        )
        if not has_descendant_whitelist:
            rules.append(rule)
    return DirectoryFilter(tuple(rules))


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


_EMPTY_ASSET_INDEXES = np.empty(0, dtype=np.int32)
_EMPTY_ASSET_INDEXES.setflags(write=False)


def _asset_indexes_by_directory_subtree(
    assets: tuple[SearchCorpusAsset, ...],
) -> dict[tuple[str, str], tuple[int, ...]]:
    indexes: dict[tuple[str, str], list[int]] = {}
    for asset in assets:
        for ancestor in _directory_ancestors(asset.directory):
            indexes.setdefault((asset.source_id, ancestor), []).append(asset.asset_index)
    return {key: _read_only_indexes(value) for key, value in indexes.items()}


def _read_only_indexes(values) -> np.ndarray:
    array = np.array(values, dtype=np.int32)
    array.setflags(write=False)
    return array


def _asset_selection_mask(asset_count: int, asset_indexes: np.ndarray) -> np.ndarray:
    mask = np.zeros(asset_count, dtype=bool)
    mask[asset_indexes] = True
    return mask


def _asset_directory_keys(
    assets: tuple[SearchCorpusAsset, ...],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    keys_by_asset_index: list[tuple[tuple[str, str], ...]] = [()] * len(assets)
    for asset in assets:
        keys_by_asset_index[asset.asset_index] = tuple(
            (asset.source_id, ancestor)
            for ancestor in _directory_ancestors(asset.directory)
        )
    return tuple(keys_by_asset_index)


def _directory_runtime(
    directory_counts: dict[tuple[str, str], int],
    *,
    source_display_names: dict[str, str],
    source_sort_keys: dict[str, tuple[int, str, str]],
) -> _DirectoryRuntime:
    keys = tuple(directory_counts)
    id_by_key = {key: index for index, key in enumerate(keys)}
    names = tuple(
        source_display_names.get(source_id, source_id)
        if directory == ""
        else directory.rsplit("/", 1)[-1]
        for source_id, directory in keys
    )
    depths = np.fromiter(
        (0 if directory == "" else len(directory.split("/")) for _source_id, directory in keys),
        dtype=np.int16,
        count=len(keys),
    )
    asset_counts = np.fromiter(
        (directory_counts[key] for key in keys),
        dtype=np.int32,
        count=len(keys),
    )
    parent_ids = np.full(len(keys), -1, dtype=np.int32)
    for directory_id, (source_id, directory) in enumerate(keys):
        if directory == "":
            continue
        parent = directory.rsplit("/", 1)[0] if "/" in directory else ""
        parent_ids[directory_id] = id_by_key[(source_id, parent)]
    rollup_order = np.argsort(-depths, kind="stable").astype(np.int32)
    sort_order = np.asarray(
        sorted(
            range(len(keys)),
            key=lambda directory_id: (
                *source_sort_keys.get(
                    keys[directory_id][0],
                    (0, keys[directory_id][0].casefold(), keys[directory_id][0]),
                ),
                _directory_path_sort_key(keys[directory_id][1]),
            ),
        ),
        dtype=np.int32,
    )
    return _DirectoryRuntime(
        keys=keys,
        names=names,
        depths=depths,
        asset_counts=asset_counts,
        parent_ids=parent_ids,
        rollup_order=rollup_order,
        sort_order=sort_order,
        id_by_key=id_by_key,
    )


def _asset_directory_ids(
    assets: tuple[SearchCorpusAsset, ...],
    directory_id_by_key: dict[tuple[str, str], int],
) -> np.ndarray:
    directory_ids = np.empty(len(assets), dtype=np.int32)
    for asset in assets:
        directory_ids[asset.asset_index] = directory_id_by_key[
            (asset.source_id, asset.directory)
        ]
    return directory_ids


def _asset_path_sort_ranks(assets: tuple[SearchCorpusAsset, ...]) -> np.ndarray:
    ranks = np.empty(len(assets), dtype=np.int32)
    for rank, asset in enumerate(
        sorted(assets, key=lambda item: item.relative_path.casefold())
    ):
        ranks[asset.asset_index] = rank
    return ranks


def _count_array_to_dict(
    counts: np.ndarray,
    keys: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], int]:
    return {
        keys[index]: count
        for index, raw_count in enumerate(counts)
        if (count := int(raw_count)) > 0
    }


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


def _lane_score(lane: str, raw_score: float, rank: int) -> float:
    lane_weight = {
        "strict": 1000.0,
        "fuzzy": 500.0,
        "domain": 300.0,
    }[lane]
    return lane_weight + raw_score + (100.0 / rank)


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


def _domain_score_from_tokenized_terms(
    asset: SearchCorpusAsset,
    tokenized_terms: tuple[tuple[str, ...], ...],
) -> tuple[float, tuple[str, ...]]:
    matched = []
    score = 0.0
    for term_tokens in tokenized_terms:
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
        media_width=asset.media_width,
        media_height=asset.media_height,
        duration_seconds=asset.duration_seconds,
        thumbnail_variants=asset.thumbnail_variants,
        score=score,
    )
