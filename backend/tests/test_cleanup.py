from pathlib import Path

from findme.cleanup import cleanup_thumbnails
from findme.db import connect, initialize_database


def test_cleanup_thumbnails_removes_only_unreferenced_thumbnail_files(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    referenced_thumb = data_root / "cache" / "thumbs" / "aa" / "referenced.webp"
    orphan_thumb = data_root / "cache" / "thumbs" / "bb" / "orphan.webp"
    referenced_thumb.parent.mkdir(parents=True)
    orphan_thumb.parent.mkdir(parents=True)
    referenced_thumb.write_bytes(b"referenced")
    orphan_thumb.write_bytes(b"orphan")

    with connect(data_root / "findme.sqlite3") as db:
        initialize_database(db)
        db.execute(
            "INSERT INTO sources(id, display_name, root, priority) VALUES (?, ?, ?, ?)",
            ("dnd", "DND", str(tmp_path / "assets"), 10),
        )
        db.execute(
            """
            INSERT INTO assets(
              asset_id, source_id, relative_path, absolute_path, filename, stem,
              extension, directory, size_bytes, mtime_ns, media_type,
              thumbnail_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "current",
                "dnd",
                "current.png",
                str(tmp_path / "assets" / "current.png"),
                "current.png",
                "current",
                ".png",
                "",
                1,
                1,
                "image",
                str(referenced_thumb),
            ),
        )

        summary = cleanup_thumbnails(db, data_root=data_root)

    assert summary.unreferenced_thumbnail_files == 1
    assert referenced_thumb.is_file()
    assert not orphan_thumb.exists()


def test_cleanup_thumbnails_preserves_referenced_thumbnail_variants(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    baseline_thumb = data_root / "cache" / "thumbs" / "aa" / "asset-1-1-256.webp"
    variant_thumb = data_root / "cache" / "thumbs" / "aa" / "asset-1-1-512.webp"
    orphan_thumb = data_root / "cache" / "thumbs" / "aa" / "asset-1-1-1024.webp"
    baseline_thumb.parent.mkdir(parents=True)
    baseline_thumb.write_bytes(b"baseline")
    variant_thumb.write_bytes(b"variant")
    orphan_thumb.write_bytes(b"orphan")

    with connect(data_root / "findme.sqlite3") as db:
        initialize_database(db)
        db.execute(
            "INSERT INTO sources(id, display_name, root, priority) VALUES (?, ?, ?, ?)",
            ("dnd", "DND", str(tmp_path / "assets"), 10),
        )
        db.execute(
            """
            INSERT INTO assets(
              asset_id, source_id, relative_path, absolute_path, filename, stem,
              extension, directory, size_bytes, mtime_ns, media_type,
              thumbnail_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset",
                "dnd",
                "asset.png",
                str(tmp_path / "assets" / "asset.png"),
                "asset.png",
                "asset",
                ".png",
                "",
                1,
                1,
                "image",
                str(baseline_thumb),
            ),
        )
        db.execute(
            """
            INSERT INTO asset_thumbnail_variants(
              asset_id, source_id, relative_path, kind, max_edge,
              width, height, cache_path, generated_from_size_bytes,
              generated_from_mtime_ns
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset",
                "dnd",
                "asset.png",
                "thumbnail",
                512,
                512,
                256,
                str(variant_thumb),
                1,
                1,
            ),
        )

        summary = cleanup_thumbnails(db, data_root=data_root)

    assert summary.unreferenced_thumbnail_files == 1
    assert baseline_thumb.is_file()
    assert variant_thumb.is_file()
    assert not orphan_thumb.exists()


def test_cleanup_thumbnails_dry_run_preserves_unreferenced_thumbnail_files(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    orphan_thumb = data_root / "cache" / "thumbs" / "bb" / "orphan.webp"
    orphan_thumb.parent.mkdir(parents=True)
    orphan_thumb.write_bytes(b"orphan")

    with connect(data_root / "findme.sqlite3") as db:
        initialize_database(db)
        summary = cleanup_thumbnails(db, data_root=data_root, dry_run=True)

    assert summary.unreferenced_thumbnail_files == 1
    assert orphan_thumb.is_file()
