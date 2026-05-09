from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import blake2b
import re
from typing import Any


AssetResponse = dict[str, Any]
ResultCard = dict[str, Any]


_TRAILING_NUMBER_RE = re.compile(
    r"^(?P<base>.+?)[\s._-]*(?:[\(\[]?(?P<number>\d+)[\)\]]?)$"
)


@dataclass(frozen=True)
class _GroupedAsset:
    asset: AssetResponse
    collection_key: tuple[str, str, str]
    variant_sort_key: tuple[str, int, str]
    has_number_suffix: bool


def result_cards_for_assets(
    assets: list[AssetResponse],
    *,
    collection_assets: list[AssetResponse] | None = None,
) -> list[ResultCard]:
    cards, _total = ResultCardResolver.from_assets(collection_assets or assets).page_for(
        assets,
        offset=0,
        limit=len(assets),
    )
    return cards


@dataclass(frozen=True)
class ResultCardResolver:
    by_collection: dict[tuple[str, str, str], tuple[_GroupedAsset, ...]]
    by_asset_id: dict[str, _GroupedAsset]
    collection_counts: dict[tuple[str, str, str], int]
    has_numbered_asset: dict[tuple[str, str, str], bool]

    @classmethod
    def from_assets(cls, assets: Iterable[AssetResponse]) -> "ResultCardResolver":
        by_collection: dict[tuple[str, str, str], list[_GroupedAsset]] = {}
        by_asset_id: dict[str, _GroupedAsset] = {}
        collection_counts: dict[tuple[str, str, str], int] = {}
        has_numbered_asset: dict[tuple[str, str, str], bool] = {}
        for asset in assets:
            grouped = _grouped_asset(asset)
            by_asset_id[str(asset["asset_id"])] = grouped
            by_collection.setdefault(grouped.collection_key, []).append(grouped)
            collection_counts[grouped.collection_key] = (
                collection_counts.get(grouped.collection_key, 0) + 1
            )
            has_numbered_asset[grouped.collection_key] = (
                has_numbered_asset.get(grouped.collection_key, False)
                or grouped.has_number_suffix
            )
        return cls(
            by_collection={
                key: tuple(sorted(values, key=lambda item: item.variant_sort_key))
                for key, values in by_collection.items()
            },
            by_asset_id=by_asset_id,
            collection_counts=collection_counts,
            has_numbered_asset=has_numbered_asset,
        )

    def cards_for(self, assets: Iterable[AssetResponse]) -> list[ResultCard]:
        cards, _total = self.page_for(assets, offset=0, limit=None)
        return cards

    def page_for(
        self,
        assets: Iterable[AssetResponse],
        *,
        offset: int,
        limit: int | None,
    ) -> tuple[list[ResultCard], int]:
        emitted_collections: set[tuple[str, str, str]] = set()
        cards: list[ResultCard] = []
        total = 0

        for asset in assets:
            grouped = self._grouped_for_asset(asset)
            if (
                self.collection_counts[grouped.collection_key] < 2
                or not self.has_numbered_asset[grouped.collection_key]
            ):
                total += 1
                if total > offset and (limit is None or len(cards) < limit):
                    cards.append({"kind": "asset", "asset": grouped.asset})
                continue
            if grouped.collection_key in emitted_collections:
                continue
            emitted_collections.add(grouped.collection_key)
            total += 1
            if total <= offset or (limit is not None and len(cards) >= limit):
                continue
            variants = self.by_collection[grouped.collection_key]
            representative = grouped
            if float(grouped.asset.get("score", 0)) == 0:
                representative = variants[0]
            cards.append(
                {
                    "kind": "collection",
                    "collection_key": _public_collection_key(grouped.collection_key),
                    "representative": representative.asset,
                    "variants": [item.asset for item in variants],
                    "count": len(variants),
                }
            )
        return cards, total

    def _grouped_for_asset(self, asset: AssetResponse) -> _GroupedAsset:
        cached = self.by_asset_id.get(str(asset["asset_id"]))
        if cached is None:
            return _grouped_asset(asset)
        return _GroupedAsset(
            asset=asset,
            collection_key=cached.collection_key,
            variant_sort_key=cached.variant_sort_key,
            has_number_suffix=cached.has_number_suffix,
        )


def _grouped_asset(asset: AssetResponse) -> _GroupedAsset:
    source_id = str(asset["source_id"])
    directory = str(asset["directory"])
    stem = str(asset["stem"])
    base, suffix_number, has_number_suffix = _variant_parts(stem)
    collection_key = (source_id, directory, base.casefold())
    return _GroupedAsset(
        asset=asset,
        collection_key=collection_key,
        variant_sort_key=(
            base.casefold(),
            suffix_number,
            str(asset["relative_path"]).casefold(),
        ),
        has_number_suffix=has_number_suffix,
    )


def _variant_parts(stem: str) -> tuple[str, int, bool]:
    match = _TRAILING_NUMBER_RE.match(stem)
    if not match:
        return stem, 0, False
    return match.group("base").rstrip(" ._-"), int(match.group("number")), True


def _public_collection_key(collection_key: tuple[str, str, str]) -> str:
    raw = "\x00".join(collection_key).encode("utf-8")
    return blake2b(raw, digest_size=10).hexdigest()
