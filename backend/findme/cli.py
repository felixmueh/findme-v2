from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from tqdm import tqdm
import uvicorn

from findme.api import create_app
from findme.config import ConfigError, load_config
from findme.db import connect, initialize_database
from findme.indexer import count_indexable_files, index_sources


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
            progress = _IndexProgressBar(total_files=count_indexable_files(config))
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
                f"failed {len(summary.thumbnail_failures)} thumbnails, "
                f"marked stale {summary.marked_stale}"
            )
            for failure in summary.thumbnail_failures:
                print(
                    "thumbnail failed: "
                    f"{failure.source_id}:{failure.relative_path}: {failure.message}",
                    file=sys.stderr,
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


class _IndexProgressBar:
    def __init__(self, *, total_files: int) -> None:
        self._last_scanned = 0
        self._bar = tqdm(
            total=total_files,
            desc="indexing",
            unit="file",
            dynamic_ncols=True,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}, {postfix}]",
            file=sys.stderr,
        )

    def __call__(self, progress) -> None:
        delta = progress.scanned_files - self._last_scanned
        if delta > 0:
            self._bar.update(delta)
            self._last_scanned = progress.scanned_files
        rate = (
            progress.generated_thumbnails / progress.elapsed_seconds
            if progress.elapsed_seconds > 0
            else 0.0
        )
        self._bar.set_postfix_str(
            f"source={progress.current_source_id} "
            f"upserted={progress.upserted_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"failed={progress.failed_thumbnails} "
            f"thumbs/s={rate:.1f} "
            f"stale={progress.marked_stale}",
            refresh=False,
        )

    def finish(self) -> None:
        self._bar.close()


if __name__ == "__main__":
    main()
