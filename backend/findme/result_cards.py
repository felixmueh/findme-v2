from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import blake2b
import re
from typing import Any


AssetResponse = dict[str, Any]
ResultCard = dict[str, Any]


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


@dataclass(frozen=True)
class _GroupedAsset:
    asset: AssetResponse
    collection_key: tuple[str, str, str]
    variant_sort_key: tuple[str, tuple[tuple[int, int, str], ...], str]
    has_variant: bool


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
    has_variant_asset: dict[tuple[str, str, str], bool]

    @classmethod
    def from_assets(cls, assets: Iterable[AssetResponse]) -> "ResultCardResolver":
        by_collection: dict[tuple[str, str, str], list[_GroupedAsset]] = {}
        by_asset_id: dict[str, _GroupedAsset] = {}
        collection_counts: dict[tuple[str, str, str], int] = {}
        has_variant_asset: dict[tuple[str, str, str], bool] = {}
        for asset in assets:
            grouped = _grouped_asset(asset)
            by_asset_id[str(asset["asset_id"])] = grouped
            by_collection.setdefault(grouped.collection_key, []).append(grouped)
            collection_counts[grouped.collection_key] = (
                collection_counts.get(grouped.collection_key, 0) + 1
            )
            has_variant_asset[grouped.collection_key] = (
                has_variant_asset.get(grouped.collection_key, False) or grouped.has_variant
            )
        return cls(
            by_collection={
                key: tuple(sorted(values, key=lambda item: item.variant_sort_key))
                for key, values in by_collection.items()
            },
            by_asset_id=by_asset_id,
            collection_counts=collection_counts,
            has_variant_asset=has_variant_asset,
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

    def _grouped_for_asset(self, asset: AssetResponse) -> _GroupedAsset:
        cached = self.by_asset_id.get(str(asset["asset_id"]))
        if cached is None:
            return _grouped_asset(asset)
        return _GroupedAsset(
            asset=asset,
            collection_key=cached.collection_key,
            variant_sort_key=cached.variant_sort_key,
            has_variant=cached.has_variant,
        )


def _grouped_asset(asset: AssetResponse) -> _GroupedAsset:
    source_id = str(asset["source_id"])
    directory = str(asset["directory"])
    stem = _collection_stem(str(asset["stem"]))
    base, variant_sort_key, has_variant = _variant_parts(stem)
    collection_key = (source_id, directory, base.casefold())
    return _GroupedAsset(
        asset=asset,
        collection_key=collection_key,
        variant_sort_key=(
            base.casefold(),
            variant_sort_key,
            str(asset["relative_path"]).casefold(),
        ),
        has_variant=has_variant,
    )


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


def _collection_stem(stem: str) -> str:
    return _EMBEDDED_IMAGE_EXTENSION_RE.sub("", stem)


def _public_collection_key(collection_key: tuple[str, str, str]) -> str:
    raw = "\x00".join(collection_key).encode("utf-8")
    return blake2b(raw, digest_size=10).hexdigest()
