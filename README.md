# Findme V2

Personal asset finder for the server-hosted DND asset library.

## Docker server deployment

The server Compose file preserves the existing production paths so the current
index, thumbnails, search corpus, and stored absolute asset paths continue to
work:

- app data: `/var/lib/findme-v2`
- source assets: `/media/NAS/main/Documents/DND`

Build and start the app:

```bash
docker compose up -d --build
```

The Compose file binds container port `8000` to fixed host port `48317`.
Open the app at:

```text
http://localhost:48317
```

Refresh the index through the running Docker service container:

```bash
docker compose exec findme findme index --config config/findme.server.toml
docker compose restart findme
```

For first-time setup of an empty data directory, initialize the database before
indexing. If the service is not running yet because the search corpus does not
exist, use a one-off admin container:

```bash
docker compose run --rm --no-deps findme findme init-db --config config/findme.server.toml
docker compose run --rm --no-deps findme findme index --config config/findme.server.toml
docker compose up -d
```

Check container health:

```bash
docker compose ps
```
