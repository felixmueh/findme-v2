from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

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
            if args.rebuild_thumbnails:
                shutil.rmtree(config.data_root / "cache" / "thumbs", ignore_errors=True)
            with connect(db_path) as db:
                initialize_database(db)
                summary = index_sources(db, config)
            print(
                "indexed "
                f"{summary.scanned_files} files, "
                f"upserted {summary.upserted_assets}, "
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

    serve = subparsers.add_parser("serve", help="serve the API and built frontend")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--static-dir", type=Path, default=Path("frontend/dist"))

    return parser


if __name__ == "__main__":
    main()
