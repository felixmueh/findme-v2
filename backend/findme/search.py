from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import re
import sqlite3

from rapidfuzz import fuzz
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
    ranked = _rank_search(
        db,
        query,
        source_id=source_id,
        directory=directory,
        directory_prefix=directory_prefix,
        domain_vocabulary=domain_vocabulary or DEFAULT_DOMAIN_VOCABULARY,
        mode=mode,
    )
    return [
        _record_to_result(candidate.asset, candidate.score)
        for candidate in ranked[offset : offset + limit]
    ]


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
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    if mode not in {"strict", "fuzzy", "domain", "combined"}:
        raise ValueError(f"unsupported search mode: {mode}")

    assets = _fetch_assets(
        db,
        source_id=source_id,
        directory=directory,
        directory_prefix=directory_prefix,
    )
    candidates: list[_Candidate] = []
    if mode == "strict":
        candidates.extend(
            _strict_candidates(assets, query_tokens, query, require_all_terms=True)
        )
    if mode == "combined":
        candidates.extend(
            _strict_candidates(assets, query_tokens, query, require_all_terms=False)
        )
    if mode in {"fuzzy", "combined"}:
        candidates.extend(
            _fuzzy_candidates(
                assets,
                query_tokens,
                query,
                suppress_strict_all_terms=mode == "combined",
            )
        )
    if mode in {"domain", "combined"}:
        candidates.extend(
            _domain_candidates(assets, query_tokens, query, domain_vocabulary)
        )
    return _rank_candidates(candidates, query_tokens)


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
        """,
        params,
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def _strict_candidates(
    assets: Iterable[_AssetRecord],
    query_tokens: tuple[str, ...],
    query: str,
    *,
    require_all_terms: bool,
) -> list[_Candidate]:
    candidates = []
    for asset in assets:
        score, matched = _strict_score(asset, query_tokens)
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
    return sorted(candidates, key=lambda candidate: candidate.raw_score, reverse=True)


def _fuzzy_candidates(
    assets: Iterable[_AssetRecord],
    query_tokens: tuple[str, ...],
    query: str,
    *,
    suppress_strict_all_terms: bool,
) -> list[_Candidate]:
    candidates = []
    for asset in assets:
        strict_score, strict_matched = _strict_score(asset, query_tokens)
        if (
            suppress_strict_all_terms
            and strict_score > 0
            and set(strict_matched) == set(query_tokens)
        ):
            continue
        score, matched = _fuzzy_score(asset, query_tokens, query)
        if score <= 0:
            continue
        candidates.append(
            _Candidate(
                asset=asset,
                lane="fuzzy",
                raw_score=score,
                matched_terms=matched,
                query_variant=query,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.raw_score, reverse=True)


def _domain_candidates(
    assets: Iterable[_AssetRecord],
    query_tokens: tuple[str, ...],
    query: str,
    domain_vocabulary: DomainVocabulary,
) -> list[_Candidate]:
    expanded_terms = _expanded_terms(query_tokens, domain_vocabulary)
    if not expanded_terms:
        return []
    candidates = []
    for asset in assets:
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
    return sorted(candidates, key=lambda candidate: candidate.raw_score, reverse=True)


def _rank_candidates(
    candidates: list[_Candidate],
    query_tokens: tuple[str, ...],
) -> list[_RankedCandidate]:
    by_asset: dict[str, list[tuple[int, _Candidate]]] = defaultdict(list)
    for lane in ("strict", "fuzzy", "domain"):
        lane_candidates = [
            candidate for candidate in candidates if candidate.lane == lane
        ]
        lane_candidates.sort(key=lambda candidate: candidate.raw_score, reverse=True)
        for rank, candidate in enumerate(lane_candidates, start=1):
            by_asset[candidate.asset.asset_id].append((rank, candidate))

    ranked = []
    for lane_matches in by_asset.values():
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


def _fuzzy_score(
    asset: _AssetRecord,
    query_tokens: tuple[str, ...],
    query: str,
) -> tuple[float, tuple[str, ...]]:
    field_tokens = asset.filename_tokens + asset.path_tokens
    matched = []
    token_scores = []
    for token in query_tokens:
        threshold = 100.0 if len(token) <= 3 else 82.0
        best = max(
            (fuzz.ratio(token, field_token) for field_token in field_tokens),
            default=0.0,
        )
        if best < threshold:
            return 0.0, ()
        matched.append(token)
        token_scores.append(float(best))

    query_score = float(fuzz.token_set_ratio(_normalize_text(query), asset.normalized_text))
    score = sum(token_scores) + query_score
    if all(token in asset.filename_tokens for token in query_tokens):
        score += 80.0
    return score, tuple(matched)


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
