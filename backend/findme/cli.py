from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import time

import uvicorn

from findme.api import create_app
from findme.config import ConfigError, load_config
from findme.db import connect, initialize_database
from findme.indexer import index_sources


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        db_path = config.data_root / "findme.sqlite3"

        if args.command == "init-db":
            with connect(db_path) as db:
                initialize_database(db)
            print(f"initialized database: {db_path}")
            return 0

        if args.command == "index":
            thumbnail_workers = (
                args.thumbnail_workers
                if args.thumbnail_workers is not None
                else config.thumbnail_workers
            )
            if thumbnail_workers < 1:
                print("--thumbnail-workers must be a positive integer", file=sys.stderr)
                return 2
            if args.rebuild_thumbnails:
                shutil.rmtree(config.data_root / "cache" / "thumbs", ignore_errors=True)
            progress = _IndexProgressPrinter()
            with connect(db_path) as db:
                initialize_database(db)
                summary = index_sources(
                    db,
                    config,
                    thumbnail_workers=thumbnail_workers,
                    progress=progress,
                )
            progress.finish()
            print(
                "indexed "
                f"{summary.scanned_files} files, "
                f"upserted {summary.upserted_assets}, "
                f"generated {summary.generated_thumbnails} thumbnails, "
                f"marked stale {summary.marked_stale}"
            )
            return 0

        if args.command == "serve":
            app = create_app(config, db_path, static_dir=args.static_dir)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="findme")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="initialize the index database")
    init_db.add_argument("--config", type=Path, required=True)

    index = subparsers.add_parser("index", help="index configured asset sources")
    index.add_argument("--config", type=Path, required=True)
    index.add_argument(
        "--rebuild-thumbnails",
        action="store_true",
        help="delete cached thumbnails before indexing so they are regenerated",
    )
    index.add_argument(
        "--thumbnail-workers",
        type=int,
        help="override configured thumbnail worker count for this index run",
    )

    serve = subparsers.add_parser("serve", help="serve the API and built frontend")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--static-dir", type=Path, default=Path("frontend/dist"))

    return parser


class _IndexProgressPrinter:
    def __init__(self) -> None:
        self._last_printed = 0.0
        self._interval_seconds = 1.0

    def __call__(self, progress) -> None:
        now = time.monotonic()
        if now - self._last_printed < self._interval_seconds:
            return
        self._last_printed = now
        rate = (
            progress.generated_thumbnails / progress.elapsed_seconds
            if progress.elapsed_seconds > 0
            else 0.0
        )
        print(
            "indexing "
            f"source={progress.current_source_id} "
            f"scanned={progress.scanned_files} "
            f"upserted={progress.upserted_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"thumbs/s={rate:.1f} "
            f"stale={progress.marked_stale} "
            f"elapsed={progress.elapsed_seconds:.1f}s",
            file=sys.stderr,
        )

    def finish(self) -> None:
        self._last_printed = 0.0


if __name__ == "__main__":
    main()
