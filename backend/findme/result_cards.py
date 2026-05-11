from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import blake2b
from typing import Any

import numpy as np


AssetResponse = dict[str, Any]
ResultCard = dict[str, Any]


@dataclass(frozen=True)
class _GroupedAsset:
    asset: AssetResponse
    collection_key: str
    variant_sort_key: str
    has_variant: bool


@dataclass(frozen=True)
class CardMetadata:
    asset_id: str
    collection_key: str
    variant_sort_key: str
    has_variant: bool


@dataclass(frozen=True)
class ResultCardResolver:
    by_collection: dict[str, tuple[_GroupedAsset, ...]]
    by_asset_id: dict[str, _GroupedAsset]
    by_asset_index: tuple[_GroupedAsset, ...]
    collection_counts: dict[str, int]
    has_variant_asset: dict[str, bool]
    card_index_by_asset_index: np.ndarray
    browse_cards: tuple[ResultCard, ...] = ()

    @classmethod
    def from_assets(
        cls,
        assets: Iterable[AssetResponse],
        *,
        card_metadata_by_asset_id: dict[str, CardMetadata],
    ) -> "ResultCardResolver":
        by_collection: dict[str, list[_GroupedAsset]] = {}
        by_asset_id: dict[str, _GroupedAsset] = {}
        collection_counts: dict[str, int] = {}
        has_variant_asset: dict[str, bool] = {}
        ordered_assets = tuple(assets)
        for asset in ordered_assets:
            asset_id = str(asset["asset_id"])
            metadata = card_metadata_by_asset_id[asset_id]
            grouped = _grouped_asset(asset, metadata)
            by_asset_id[asset_id] = grouped
            by_collection.setdefault(grouped.collection_key, []).append(grouped)
            collection_counts[grouped.collection_key] = (
                collection_counts.get(grouped.collection_key, 0) + 1
            )
            has_variant_asset[grouped.collection_key] = (
                has_variant_asset.get(grouped.collection_key, False) or grouped.has_variant
            )
        resolver = cls(
            by_collection={
                key: tuple(sorted(values, key=lambda item: item.variant_sort_key))
                for key, values in by_collection.items()
            },
            by_asset_id=by_asset_id,
            by_asset_index=tuple(
                by_asset_id[str(asset["asset_id"])] for asset in ordered_assets
            ),
            collection_counts=collection_counts,
            has_variant_asset=has_variant_asset,
            card_index_by_asset_index=np.empty(0, dtype=np.int32),
        )
        browse_cards: list[ResultCard] = []
        card_index_by_key: dict[str, int] = {}
        asset_card_indexes = np.empty(len(ordered_assets), dtype=np.int32)
        for asset_index in range(len(ordered_assets)):
            grouped = resolver.by_asset_index[asset_index]
            card_key = resolver.card_key_for_asset_index(asset_index)
            card_index = card_index_by_key.get(card_key)
            if card_index is None:
                card_index = len(browse_cards)
                card_index_by_key[card_key] = card_index
                browse_cards.append(resolver._card_for_grouped_asset(grouped))
            asset_card_indexes[asset_index] = card_index
        return cls(
            by_collection=resolver.by_collection,
            by_asset_id=resolver.by_asset_id,
            by_asset_index=resolver.by_asset_index,
            collection_counts=resolver.collection_counts,
            has_variant_asset=resolver.has_variant_asset,
            card_index_by_asset_index=asset_card_indexes,
            browse_cards=tuple(browse_cards),
        )

    def page_for(
        self,
        assets: Iterable[AssetResponse],
        *,
        offset: int,
        limit: int | None,
    ) -> tuple[list[ResultCard], int]:
        return self._page_for_grouped_assets(
            (self._grouped_for_asset(asset) for asset in assets),
            offset=offset,
            limit=limit,
        )

    def browse_page(
        self,
        *,
        offset: int,
        limit: int | None,
    ) -> tuple[list[ResultCard], int]:
        end = None if limit is None else offset + limit
        return list(self.browse_cards[offset:end]), len(self.browse_cards)

    def page_for_asset_indexes(
        self,
        asset_indexes: Iterable[int],
        *,
        offset: int,
        limit: int | None,
        total: int | None = None,
    ) -> tuple[list[ResultCard], int]:
        return self._page_for_grouped_assets(
            (self.by_asset_index[asset_index] for asset_index in asset_indexes),
            offset=offset,
            limit=limit,
            total=total,
        )

    def page_for_asset_index_array(
        self,
        asset_indexes: np.ndarray,
        *,
        offset: int,
        limit: int | None,
        total: int | None = None,
    ) -> tuple[list[ResultCard], int]:
        if len(asset_indexes) == 0:
            return [], 0 if total is None else total
        card_indexes = self.card_index_by_asset_index[asset_indexes]
        unique_positions = np.unique(card_indexes, return_index=True)[1]
        ordered_card_indexes = card_indexes[np.sort(unique_positions)]
        end = None if limit is None else offset + limit
        page_card_indexes = ordered_card_indexes[offset:end]
        return (
            [self.browse_cards[int(card_index)] for card_index in page_card_indexes],
            len(ordered_card_indexes) if total is None else total,
        )

    def _page_for_grouped_assets(
        self,
        grouped_assets: Iterable[_GroupedAsset],
        *,
        offset: int,
        limit: int | None,
        total: int | None = None,
    ) -> tuple[list[ResultCard], int]:
        emitted_collections: set[str] = set()
        cards: list[ResultCard] = []
        observed_total = 0

        for grouped in grouped_assets:
            if (
                self.collection_counts[grouped.collection_key] < 2
                or not self.has_variant_asset[grouped.collection_key]
            ):
                observed_total += 1
                if observed_total > offset and (limit is None or len(cards) < limit):
                    cards.append({"kind": "asset", "asset": grouped.asset})
                if total is not None and limit is not None and len(cards) >= limit:
                    break
                continue
            if grouped.collection_key in emitted_collections:
                continue
            emitted_collections.add(grouped.collection_key)
            observed_total += 1
            if observed_total <= offset or (limit is not None and len(cards) >= limit):
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
            if total is not None and limit is not None and len(cards) >= limit:
                break
        return cards, observed_total if total is None else total

    def card_key_for_asset_index(self, asset_index: int) -> str:
        grouped = self.by_asset_index[asset_index]
        if (
            self.collection_counts[grouped.collection_key] < 2
            or not self.has_variant_asset[grouped.collection_key]
        ):
            return f"asset:{grouped.asset['asset_id']}"
        return f"collection:{grouped.collection_key}"

    def _card_for_grouped_asset(self, grouped: _GroupedAsset) -> ResultCard:
        if (
            self.collection_counts[grouped.collection_key] < 2
            or not self.has_variant_asset[grouped.collection_key]
        ):
            return {"kind": "asset", "asset": grouped.asset}
        variants = self.by_collection[grouped.collection_key]
        representative = grouped
        if float(grouped.asset.get("score", 0)) == 0:
            representative = variants[0]
        return {
            "kind": "collection",
            "collection_key": _public_collection_key(grouped.collection_key),
            "representative": representative.asset,
            "variants": [item.asset for item in variants],
            "count": len(variants),
        }

    def _grouped_for_asset(self, asset: AssetResponse) -> _GroupedAsset:
        cached = self.by_asset_id.get(str(asset["asset_id"]))
        if cached is None:
            raise KeyError(f"asset is missing card metadata: {asset['asset_id']}")
        return _GroupedAsset(
            asset=asset,
            collection_key=cached.collection_key,
            variant_sort_key=cached.variant_sort_key,
            has_variant=cached.has_variant,
        )

def _grouped_asset(asset: AssetResponse, metadata: CardMetadata) -> _GroupedAsset:
    return _GroupedAsset(
        asset=asset,
        collection_key=metadata.collection_key,
        variant_sort_key=metadata.variant_sort_key,
        has_variant=metadata.has_variant,
    )


def _public_collection_key(collection_key: str) -> str:
    raw = collection_key.encode("utf-8")
    return blake2b(raw, digest_size=10).hexdigest()
