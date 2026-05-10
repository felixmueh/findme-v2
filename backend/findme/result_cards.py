from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import blake2b
from typing import Any


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


def result_cards_for_assets(
    assets: list[AssetResponse],
    *,
    collection_assets: list[AssetResponse] | None = None,
    card_metadata_by_asset_id: dict[str, CardMetadata],
) -> list[ResultCard]:
    resolver = ResultCardResolver.from_assets(
        collection_assets or assets,
        card_metadata_by_asset_id=card_metadata_by_asset_id,
    )
    cards, _total = resolver.page_for(
        assets,
        offset=0,
        limit=len(assets),
    )
    return cards


@dataclass(frozen=True)
class ResultCardResolver:
    by_collection: dict[str, tuple[_GroupedAsset, ...]]
    by_asset_id: dict[str, _GroupedAsset]
    collection_counts: dict[str, int]
    has_variant_asset: dict[str, bool]
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
            collection_counts=collection_counts,
            has_variant_asset=has_variant_asset,
        )
        browse_cards, _total = resolver.page_for(
            ordered_assets,
            offset=0,
            limit=None,
        )
        return cls(
            by_collection=resolver.by_collection,
            by_asset_id=resolver.by_asset_id,
            collection_counts=resolver.collection_counts,
            has_variant_asset=resolver.has_variant_asset,
            browse_cards=tuple(browse_cards),
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
        emitted_collections: set[str] = set()
        cards: list[ResultCard] = []
        total = 0

        for asset in assets:
            grouped = self._grouped_for_asset(asset)
            if (
                self.collection_counts[grouped.collection_key] < 2
                or not self.has_variant_asset[grouped.collection_key]
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

    def browse_page(
        self,
        *,
        offset: int,
        limit: int | None,
        asset_ids: Iterable[str] | None = None,
    ) -> tuple[list[ResultCard], int]:
        if asset_ids is not None:
            return self.page_for_asset_ids(
                asset_ids,
                offset=offset,
                limit=limit,
            )
        end = None if limit is None else offset + limit
        return list(self.browse_cards[offset:end]), len(self.browse_cards)

    def page_for_asset_ids(
        self,
        asset_ids: Iterable[str],
        *,
        offset: int,
        limit: int | None,
    ) -> tuple[list[ResultCard], int]:
        return self.page_for(
            (self.by_asset_id[asset_id].asset for asset_id in asset_ids),
            offset=offset,
            limit=limit,
        )

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
