from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import sqlite3

from findme.assets import AssetRecord


HASH_ALGORITHM = "sha256"
_TAG_TOKEN_RE = re.compile(r"[a-z0-9]+")


class MetadataValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TagRecord:
    slug: str
    label: str


def tags_for_asset(
    metadata_db: sqlite3.Connection,
    asset: AssetRecord,
) -> tuple[TagRecord, ...]:
    row = metadata_db.execute(
        """
        SELECT file_id
        FROM metadata_file_locations
        WHERE source_id = ?
          AND relative_path = ?
          AND missing_at IS NULL
        """,
        (asset.source_id, asset.relative_path),
    ).fetchone()
    if row is None:
        return ()
    return tags_for_file(metadata_db, row["file_id"])


def tags_for_file(
    metadata_db: sqlite3.Connection,
    file_id: str,
) -> tuple[TagRecord, ...]:
    rows = metadata_db.execute(
        """
        SELECT tags.slug, tags.label
        FROM file_tags
        JOIN tags ON tags.tag_id = file_tags.tag_id
        WHERE file_tags.file_id = ?
        ORDER BY file_tags.sort_order, tags.label COLLATE NOCASE, tags.slug
        """,
        (file_id,),
    ).fetchall()
    return _sort_tags(tuple(TagRecord(slug=row["slug"], label=row["label"]) for row in rows))


def list_tags(metadata_db: sqlite3.Connection) -> tuple[TagRecord, ...]:
    rows = metadata_db.execute(
        """
        SELECT slug, label
        FROM tags
        ORDER BY
          CASE
            WHEN slug GLOB '[0-9]*' AND slug NOT GLOB '*[^0-9]*'
            THEN 0
            ELSE 1
          END,
          CASE
            WHEN slug GLOB '[0-9]*' AND slug NOT GLOB '*[^0-9]*'
            THEN CAST(slug AS INTEGER)
            ELSE 0
          END,
          label COLLATE NOCASE,
          slug
        """
    ).fetchall()
    return tuple(TagRecord(slug=row["slug"], label=row["label"]) for row in rows)


def tag_by_slug(metadata_db: sqlite3.Connection, slug: str) -> TagRecord | None:
    row = metadata_db.execute(
        """
        SELECT slug, label
        FROM tags
        WHERE slug = ?
        """,
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return TagRecord(slug=row["slug"], label=row["label"])


def asset_ids_for_tag(
    metadata_db: sqlite3.Connection,
    slug: str,
) -> tuple[str, ...]:
    rows = metadata_db.execute(
        """
        SELECT DISTINCT metadata_file_locations.asset_id
        FROM tags
        JOIN file_tags ON file_tags.tag_id = tags.tag_id
        JOIN metadata_file_locations
          ON metadata_file_locations.file_id = file_tags.file_id
        WHERE tags.slug = ?
          AND metadata_file_locations.asset_id IS NOT NULL
          AND metadata_file_locations.missing_at IS NULL
        """,
        (slug,),
    ).fetchall()
    return tuple(row["asset_id"] for row in rows)


def set_asset_tags(
    metadata_db: sqlite3.Connection,
    asset: AssetRecord,
    labels: list[str],
) -> tuple[TagRecord, ...]:
    tags = _normalize_tag_labels(labels)
    file_id = ensure_asset_file(metadata_db, asset)
    return _replace_file_tags(metadata_db, file_id, tags)


def add_asset_tags(
    metadata_db: sqlite3.Connection,
    asset: AssetRecord,
    labels: list[str],
) -> tuple[TagRecord, ...]:
    file_id = ensure_asset_file(metadata_db, asset)
    existing = list(tags_for_file(metadata_db, file_id))
    existing_by_slug = {tag.slug: tag for tag in existing}
    for tag in _normalize_tag_labels(labels):
        existing_by_slug.setdefault(tag.slug, tag)
    return _replace_file_tags(metadata_db, file_id, _sort_tags(tuple(existing_by_slug.values())))


def remove_asset_tags(
    metadata_db: sqlite3.Connection,
    asset: AssetRecord,
    labels: list[str],
) -> tuple[TagRecord, ...]:
    row = metadata_db.execute(
        """
        SELECT file_id
        FROM metadata_file_locations
        WHERE source_id = ?
          AND relative_path = ?
          AND missing_at IS NULL
        """,
        (asset.source_id, asset.relative_path),
    ).fetchone()
    if row is None:
        return ()
    remove_slugs = {tag.slug for tag in _normalize_tag_labels(labels)}
    remaining = _sort_tags(tuple(
        tag for tag in tags_for_file(metadata_db, row["file_id"]) if tag.slug not in remove_slugs
    ))
    return _replace_file_tags(metadata_db, row["file_id"], remaining)


def ensure_asset_file(
    metadata_db: sqlite3.Connection,
    asset: AssetRecord,
) -> str:
    file_id, digest = _hash_asset(asset)
    metadata_db.execute(
        """
        INSERT INTO metadata_files(file_id, hash_algorithm, content_hash, size_bytes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(hash_algorithm, content_hash) DO UPDATE SET
          size_bytes = excluded.size_bytes,
          updated_at = CURRENT_TIMESTAMP
        """,
        (file_id, HASH_ALGORITHM, digest, asset.size_bytes),
    )
    metadata_db.execute(
        """
        INSERT INTO metadata_file_locations(
          source_id,
          relative_path,
          file_id,
          asset_id,
          size_bytes,
          mtime_ns
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, relative_path) DO UPDATE SET
          file_id = excluded.file_id,
          asset_id = excluded.asset_id,
          size_bytes = excluded.size_bytes,
          mtime_ns = excluded.mtime_ns,
          last_seen_at = CURRENT_TIMESTAMP,
          missing_at = NULL
        """,
        (
            asset.source_id,
            asset.relative_path,
            file_id,
            asset.asset_id,
            asset.size_bytes,
            asset.mtime_ns,
        ),
    )
    return file_id


def tag_response(tag: TagRecord) -> dict[str, str]:
    return {"slug": tag.slug, "label": tag.label}


def _replace_file_tags(
    metadata_db: sqlite3.Connection,
    file_id: str,
    tags: tuple[TagRecord, ...],
) -> tuple[TagRecord, ...]:
    tags = _sort_tags(tags)
    metadata_db.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
    for index, tag in enumerate(tags):
        tag_id = tag.slug
        metadata_db.execute(
            """
            INSERT INTO tags(tag_id, slug, label)
            VALUES (?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
              label = excluded.label,
              updated_at = CURRENT_TIMESTAMP
            """,
            (tag_id, tag.slug, tag.label),
        )
        metadata_db.execute(
            """
            INSERT INTO file_tags(file_id, tag_id, sort_order)
            VALUES (?, ?, ?)
            """,
            (file_id, tag_id, index),
        )
    metadata_db.execute(
        """
        DELETE FROM tags
        WHERE NOT EXISTS (
          SELECT 1
          FROM file_tags
          WHERE file_tags.tag_id = tags.tag_id
        )
        """
    )
    return tags


def _hash_asset(asset: AssetRecord) -> tuple[str, str]:
    digest = hashlib.sha256()
    with asset.absolute_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    hexdigest = digest.hexdigest()
    return f"{HASH_ALGORITHM}:{hexdigest}", hexdigest


def _normalize_tag_labels(labels: list[str]) -> tuple[TagRecord, ...]:
    normalized: list[TagRecord] = []
    seen: set[str] = set()
    for label in labels:
        clean_label = " ".join(label.strip().split())
        if not clean_label:
            continue
        slug = _slug_for_label(clean_label)
        if not slug:
            raise MetadataValidationError("tag must contain letters or numbers")
        if slug in seen:
            continue
        seen.add(slug)
        normalized.append(TagRecord(slug=slug, label=clean_label))
    return _sort_tags(tuple(normalized))


def _slug_for_label(label: str) -> str:
    return "-".join(_TAG_TOKEN_RE.findall(label.casefold()))


def _sort_tags(tags: tuple[TagRecord, ...]) -> tuple[TagRecord, ...]:
    return tuple(sorted(tags, key=_tag_sort_key))


def _tag_sort_key(tag: TagRecord) -> tuple[int, int, str]:
    if tag.slug.isdecimal():
        return (0, int(tag.slug), tag.slug)
    return (1, 0, tag.label.casefold())
