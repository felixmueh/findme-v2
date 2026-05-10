# Laptop Environment

This profile is for the laptop host, outside Docker.

## Accessible

- Repository working tree.
- `sample-assets` for normal development and test data.
- `/media/NAS/main/Documents/DND` may be reachable as a network share, but it is slow for indexing and must not be used as the normal indexing source.

## Writable

- Repository files for normal development.
- `.findme-data` through `config/findme.dev.toml`.
- `sample-assets` for selected copied fixtures.

## Read-Only

- Treat `/media/NAS/main/Documents/DND` as read-only source material when it is accessible.

## Safe Commands

```bash
uv run pytest
npm --prefix frontend run build
uv run findme init-db --config config/findme.dev.toml
uv run findme index --config config/findme.dev.toml
uv run findme serve --config config/findme.dev.toml --host 127.0.0.1 --port "$(cat .port)" --static-dir frontend/dist
```

Use the worktree-specific port recorded in `.port`. Open the app at:

```text
http://localhost:<port-from-.port>
```

## Requires Explicit Confirmation

- Copying selected assets from `/media/NAS/main/Documents/DND` into `sample-assets`.
- Any command that writes outside the repository.
- Docker, systemd, firewall, or host service commands.
- Long-running benchmark or indexing jobs that do not use `sample-assets`.

## Do Not Do

- Do not index `/media/NAS/main/Documents/DND` from the laptop host.
- Do not bulk-mirror the full DND library into `sample-assets`.
- Do not write generated data into asset source directories.

## Indexing

Use the sample asset config:

```bash
uv run findme index --config config/findme.dev.toml
```

If realistic coverage is needed, copy only the required assets into `sample-assets`, keep the sample set below 40 GB, and re-run the dev indexing command.
