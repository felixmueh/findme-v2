# Laptop Sandbox Environment

This profile is for the laptop Docker sandbox.

## Accessible

- Repository working tree, usually shared with the laptop host.
- `sample-assets` for normal development and test data.
- `/media/NAS/main/Documents/DND` may or may not be mounted. Even if present, it is not the normal indexing source from this environment.

## Writable

- Repository files for normal development.
- `.findme-data` through `config/findme.dev.toml`. This may be shared with the laptop host.
- `sample-assets` for selected copied fixtures.
- Development tools and dependencies may be installed in the sandbox when needed.

## Read-Only

- Treat `/media/NAS/main/Documents/DND` as read-only if it is mounted.

## Safe Commands

```bash
uv run pytest
npm --prefix frontend run build
uv run findme init-db --config config/findme.dev.toml
uv run findme index --config config/findme.dev.toml
uv run findme serve --config config/findme.dev.toml --host 0.0.0.0 --port "$(cat .port)" --static-dir frontend/dist
```

Publish the sandbox port recorded in `.port` to the host. Open the app from the laptop host at:

```text
http://localhost:<port-from-.port>
```

## Requires Explicit Confirmation

- Copying selected assets from `/media/NAS/main/Documents/DND` into `sample-assets`.
- Any command that needs Docker host access, host credentials, systemd, or firewall access.
- Long-running benchmark or indexing jobs that do not use `sample-assets`.

## Do Not Do

- Do not index `/media/NAS/main/Documents/DND` from the laptop sandbox.
- Do not bulk-mirror the full DND library into `sample-assets`.
- Do not write generated data into asset source directories.

## Indexing

Use the sample asset config:

```bash
uv run findme index --config config/findme.dev.toml
```

Laptop and laptop-sbx intentionally share the same dev config and may share `.findme-data`.
