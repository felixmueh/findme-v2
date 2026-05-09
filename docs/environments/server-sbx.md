# Server Sandbox Environment

This profile is for the server Docker sandbox.

## Accessible

- Repository working tree, possibly shared with the server host.
- Fast DND source mount at `/media/NAS/main/Documents/DND`.
- Shared app data root at `/var/lib/findme-v2`.

## Writable

- Repository files for normal development.
- `/var/lib/findme-v2` for the SQLite database and generated cache files.
- Development tools and dependencies may be installed in the sandbox when needed.

## Read-Only

- `/media/NAS/main/Documents/DND` is source material. Treat it as read-only.

## Safe Commands

```bash
uv run pytest
npm --prefix frontend run build
uv run findme init-db --config config/findme.server.toml
uv run findme index --config config/findme.server.toml
uv run findme serve --config config/findme.server.toml --host 0.0.0.0 --port 8001 --static-dir frontend/dist
uv run findme search-eval --config config/findme.server.toml --eval search-eval/dnd.yaml --scope combined
```

Publish sandbox port 8001 to the host. Open the app from the host at:

```text
http://localhost:8001
```

## Requires Explicit Confirmation

- Destructive cleanup of `/var/lib/findme-v2`.
- Rebuilding all thumbnails with `--rebuild-thumbnails`.
- Any command that needs Docker host access, host credentials, systemd, firewall access, or host service changes.
- Copying or deleting anything under `/media/NAS/main/Documents/DND`.

## Do Not Do

- Do not write generated data into `/media/NAS/main/Documents/DND`.
- Do not create active environment markers inside the repository.

## Indexing

The server sandbox may index the real DND library:

```bash
uv run findme index --config config/findme.server.toml
```

The server host and server sandbox share `/var/lib/findme-v2`. Avoid running two indexing jobs at the same time against the shared data root.
