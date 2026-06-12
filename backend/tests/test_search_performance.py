from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Callable, TypeVar

import numpy as np
import pytest
from fastapi.testclient import TestClient

import findme.api as api_module
import findme.search as search_module
from findme.api import create_app
from findme.config import AppConfig, SourceConfig
from findme.db import connect, initialize_database
from findme.search import SearchIndex, SearchResult, _SearchMatchArrays
from findme.search_corpus import (
    SEARCH_CORPUS_VERSION,
    SearchCorpusAsset,
    SearchCorpusSnapshot,
    count_asset_directories,
)


BROAD_ASSET_COUNT = 50_000
BROAD_PAGE_LIMIT = 25
CORE_BROAD_SEARCH_SECONDS = 0.25
API_BROAD_SEARCH_SECONDS = 1.0
T = TypeVar("T")


@pytest.fixture(scope="module")
def broad_search_index() -> SearchIndex:
    assets = _synthetic_assets(BROAD_ASSET_COUNT)
    search_index = _search_index_for_assets(assets)
    matches = _SearchMatchArrays(
        asset_indexes=np.arange(len(assets), dtype=np.int32),
        scores=np.linspace(1.0, 0.0, len(assets), dtype=np.float64),
        total=len(assets),
    )
    search_index._search_match_arrays = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: matches
    )
    return search_index


def test_broad_search_materializes_only_requested_page(
    broad_search_index: SearchIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materialized = 0
    original = search_module._record_to_result

    def counted_record_to_result(
        asset: SearchCorpusAsset,
        score: float,
    ) -> SearchResult:
        nonlocal materialized
        materialized += 1
        return original(asset, score)

    monkeypatch.setattr(search_module, "_record_to_result", counted_record_to_result)

    elapsed, page = _timed_call(
        lambda: broad_search_index.search(
            "common",
            limit=BROAD_PAGE_LIMIT,
            offset=0,
        )
    )

    assert page.total == BROAD_ASSET_COUNT
    assert len(page.assets) == BROAD_PAGE_LIMIT
    assert materialized == BROAD_PAGE_LIMIT
    assert elapsed < CORE_BROAD_SEARCH_SECONDS


def test_broad_search_with_directory_counts_materializes_only_requested_page(
    broad_search_index: SearchIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materialized = 0
    original = search_module._record_to_result

    def counted_record_to_result(
        asset: SearchCorpusAsset,
        score: float,
    ) -> SearchResult:
        nonlocal materialized
        materialized += 1
        return original(asset, score)

    monkeypatch.setattr(search_module, "_record_to_result", counted_record_to_result)

    elapsed, response = _timed_call(
        lambda: broad_search_index.search_with_directory_responses(
            "common",
            limit=BROAD_PAGE_LIMIT,
            offset=0,
        )
    )

    assert response.page.total == BROAD_ASSET_COUNT
    assert len(response.page.assets) == BROAD_PAGE_LIMIT
    assert materialized == BROAD_PAGE_LIMIT
    assert len(response.directories) == 102
    assert elapsed < CORE_BROAD_SEARCH_SECONDS


@pytest.mark.parametrize(
    ("request_body", "expected_response_materializations"),
    (
        ({"q": "common", "limit": BROAD_PAGE_LIMIT}, BROAD_PAGE_LIMIT),
        (
            {"q": "common", "limit": BROAD_PAGE_LIMIT, "include_directories": True},
            BROAD_PAGE_LIMIT,
        ),
    ),
)
def test_api_broad_search_materializes_pages_not_all_matches(
    tmp_path: Path,
    broad_search_index: SearchIndex,
    monkeypatch: pytest.MonkeyPatch,
    request_body: dict[str, object],
    expected_response_materializations: int,
) -> None:
    client = _synthetic_search_client(
        tmp_path,
        broad_search_index,
        monkeypatch=monkeypatch,
    )
    record_materializations = 0
    response_materializations = 0
    original_record = search_module._record_to_result
    original_response = api_module._asset_result_response

    def counted_record_to_result(
        asset: SearchCorpusAsset,
        score: float,
    ) -> SearchResult:
        nonlocal record_materializations
        record_materializations += 1
        return original_record(asset, score)

    def counted_asset_result_response(
        result: SearchCorpusAsset,
        *,
        score: float,
    ) -> dict[str, object]:
        nonlocal response_materializations
        response_materializations += 1
        return original_response(result, score=score)

    monkeypatch.setattr(search_module, "_record_to_result", counted_record_to_result)
    monkeypatch.setattr(api_module, "_asset_result_response", counted_asset_result_response)

    elapsed, response = _timed_call(
        lambda: client.post("/api/search/query", json=request_body)
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == BROAD_ASSET_COUNT
    assert len(body["cards"]) == BROAD_PAGE_LIMIT
    assert record_materializations == 0
    assert response_materializations == expected_response_materializations
    assert elapsed < API_BROAD_SEARCH_SECONDS


@pytest.mark.parametrize(
    "request_body",
    (
        {"q": "common", "limit": BROAD_PAGE_LIMIT},
        {"q": "common", "limit": BROAD_PAGE_LIMIT, "include_directories": True},
    ),
)
def test_api_variant_heavy_search_materializes_cards_not_matching_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_body: dict[str, object],
) -> None:
    search_index = _search_index_for_assets(
        _synthetic_collection_assets(
            collection_count=100,
            variants_per_collection=31,
        )
    )
    matches = _SearchMatchArrays(
        asset_indexes=np.arange(len(search_index.assets), dtype=np.int32),
        scores=np.linspace(1.0, 0.0, len(search_index.assets), dtype=np.float64),
        total=len(search_index.assets),
    )
    search_index._search_match_arrays = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: matches
    )
    client = _synthetic_search_client(
        tmp_path,
        search_index,
        monkeypatch=monkeypatch,
    )
    response_materializations = 0
    original_response = api_module._asset_result_response

    def counted_asset_result_response(
        result: SearchCorpusAsset,
        *,
        score: float,
    ) -> dict[str, object]:
        nonlocal response_materializations
        response_materializations += 1
        return original_response(result, score=score)

    monkeypatch.setattr(api_module, "_asset_result_response", counted_asset_result_response)

    elapsed, response = _timed_call(
        lambda: client.post("/api/search/query", json=request_body)
    )
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == len(search_index.assets)
    assert len(body["cards"]) == BROAD_PAGE_LIMIT
    assert {card["kind"] for card in body["cards"]} == {"collection"}
    assert response_materializations == BROAD_PAGE_LIMIT
    assert elapsed < API_BROAD_SEARCH_SECONDS


def _timed_call(callback: Callable[[], T]) -> tuple[float, T]:
    start = perf_counter()
    result = callback()
    return perf_counter() - start, result


def _synthetic_search_client(
    tmp_path: Path,
    search_index: SearchIndex,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    config = AppConfig(
        data_root=tmp_path / "data",
        sources=(
            SourceConfig(
                id="dnd",
                display_name="DND",
                root=tmp_path / "assets",
                priority=10,
                media_extensions=frozenset({".png"}),
            ),
        ),
    )
    config.data_root.mkdir()
    db_path = tmp_path / "findme.sqlite3"
    with connect(db_path) as db:
        initialize_database(db)

    snapshot = SearchCorpusSnapshot(
        version=SEARCH_CORPUS_VERSION,
        assets=search_index.assets,
        directory_counts=search_index.directory_counts,
        source_display_names=search_index.source_display_names,
        source_sort_keys=search_index.source_sort_keys,
        fuzzy_tokens=(),
        fuzzy_token_asset_indexes=(),
    )

    monkeypatch.setattr(
        api_module,
        "load_search_corpus_snapshot",
        lambda _path: snapshot,
    )
    monkeypatch.setattr(
        SearchIndex,
        "load_from_snapshot",
        classmethod(lambda *_args, **_kwargs: search_index),
    )
    return TestClient(create_app(config, db_path))


def _synthetic_assets(count: int) -> tuple[SearchCorpusAsset, ...]:
    return tuple(_synthetic_asset(index) for index in range(count))


def _synthetic_collection_assets(
    *,
    collection_count: int,
    variants_per_collection: int,
) -> tuple[SearchCorpusAsset, ...]:
    return tuple(
        _synthetic_asset(
            (collection_index * variants_per_collection) + variant_index,
            collection_key=f"collection-{collection_index:05d}",
            variant_sort_key=f"{variant_index:05d}",
            has_variant=True,
        )
        for collection_index in range(collection_count)
        for variant_index in range(variants_per_collection)
    )


def _synthetic_asset(
    index: int,
    *,
    collection_key: str | None = None,
    variant_sort_key: str = "",
    has_variant: bool = False,
) -> SearchCorpusAsset:
    directory = f"Folder/{index // 500:03d}"
    filename = f"common-token asset {index:05d}.png"
    stem = filename.removesuffix(".png")
    relative_path = f"{directory}/{filename}"
    directory_tokens = ("folder", f"{index // 500:03d}")
    filename_tokens = ("common", "token", "asset", f"{index:05d}", "png")
    path_tokens = directory_tokens + filename_tokens
    return SearchCorpusAsset(
        asset_index=index,
        asset_id=f"asset-{index:05d}",
        source_id="dnd",
        source_display_name="DND",
        source_priority=10,
        relative_path=relative_path,
        filename=filename,
        stem=stem,
        extension=".png",
        directory=directory,
        size_bytes=1234,
        mtime_ns=1_700_000_000_000_000_000 + index,
        media_type="image",
        media_width=64,
        media_height=64,
        duration_seconds=None,
        thumbnail_variants=(),
        source_tokens=("dnd",),
        filename_tokens=filename_tokens,
        directory_tokens=directory_tokens,
        path_tokens=path_tokens,
        normalized_text=" ".join(path_tokens),
        fuzzy_text=" ".join(path_tokens),
        collection_key=collection_key or f"asset-{index:05d}",
        variant_sort_key=variant_sort_key,
        has_variant=has_variant,
    )


def _search_index_for_assets(assets: tuple[SearchCorpusAsset, ...]) -> SearchIndex:
    directory_counts = count_asset_directories(
        (asset.source_id, asset.directory) for asset in assets
    )
    source_display_names = {"dnd": "DND"}
    source_sort_keys = {"dnd": (-10, "dnd", "dnd")}
    directory_runtime = search_module._directory_runtime(
        directory_counts,
        source_display_names=source_display_names,
        source_sort_keys=source_sort_keys,
    )
    return SearchIndex(
        assets=assets,
        directory_counts=directory_counts,
        directory_subtree_asset_indexes=search_module._asset_indexes_by_directory_subtree(
            assets
        ),
        asset_directory_keys=search_module._asset_directory_keys(assets),
        asset_directory_ids=search_module._asset_directory_ids(
            assets,
            directory_runtime.id_by_key,
        ),
        asset_path_sort_ranks=search_module._asset_path_sort_ranks(assets),
        directory_runtime=directory_runtime,
        source_display_names=source_display_names,
        source_sort_keys=source_sort_keys,
        fuzzy_tokens=(),
        fuzzy_token_asset_indexes=(),
        lexical_index=None,
    )
