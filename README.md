# Paperless-OPDS

Middleware that exposes a [Paperless-ngx](https://docs.paperless-ngx.com/) document library as an [OPDS 1.2](https://specs.opds.io/opds-1.2) catalog feed. Point any OPDS-compatible reader (KOReader, Librera, Moon+ Reader, etc.) at it and browse/download your documents directly.

## Features

- **Full OPDS 1.2 catalog** with navigation and acquisition feeds
- **Browse by** tag, correspondent, document type, or custom select fields
- **Search** via OpenSearch
- **Proxied downloads** — readers never need Paperless credentials
- **Thumbnail caching** — in-memory, 10-minute TTL
- **Response caching** — all OPDS XML feeds cached 10 minutes
- **Optional HTTP Basic Auth** for protecting the feed
- **Config via env vars or `config.json`**

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  paperless-opds:
    build: .
    ports:
      - "8000:8000"
    environment:
      PAPERLESS_URL: "https://paperless.example.com"
      PAPERLESS_API_KEY: "your-api-key-here"
    restart: unless-stopped
```

```bash
docker compose up -d
```

### Docker

```bash
docker build -t paperless-opds .
docker run -d \
  -p 8000:8000 \
  -e PAPERLESS_URL="https://paperless.example.com" \
  -e PAPERLESS_API_KEY="your-api-key-here" \
  paperless-opds
```

### Local Development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# configure config.json or export env vars
./run.sh
```

## Configuration

All settings can be provided via environment variables or a `config.json` file. Env vars take priority.

| Env var | config.json | Default | Description |
|---|---|---|---|
| `PAPERLESS_URL` | `paperless.url` | *required* | Paperless-ngx base URL |
| `PAPERLESS_API_KEY` | `paperless.api_key` | *required* | API token ([Settings > API](https://docs.paperless-ngx.com/api/#authorization)) |
| `OPDS_TITLE` | — | `Paperless-OPDS` | Catalog title shown in readers |
| `OPDS_PAGE_SIZE` | — | `25` | Documents per page |
| `CACHE_TTL` | — | `300` | Metadata cache TTL in seconds (tags, correspondents, etc.) |
| `OPDS_USERNAME` | — | *unset* | Enable Basic Auth (both username and password must be set) |
| `OPDS_PASSWORD` | — | *unset* | Basic Auth password |
| `CONFIG_PATH` | — | `config.json` | Path to config file |

Example `config.json`:

```json
{
  "paperless": {
    "url": "https://paperless.example.com",
    "api_key": "your-api-key-here"
  }
}
```

## OPDS Endpoints

| Path | Type | Description |
|---|---|---|
| `/opds` | Navigation | Root catalog |
| `/opds/all` | Acquisition | All documents (paginated) |
| `/opds/recent` | Acquisition | Recently added documents |
| `/opds/tags` | Navigation | Browse by tag |
| `/opds/tags/{id}` | Acquisition | Documents with tag |
| `/opds/correspondents` | Navigation | Browse by correspondent |
| `/opds/correspondents/{id}` | Acquisition | Documents by correspondent |
| `/opds/document-types` | Navigation | Browse by document type |
| `/opds/document-types/{id}` | Acquisition | Documents by type |
| `/opds/custom-fields/{id}` | Navigation | Browse select field options |
| `/opds/custom-fields/{id}/{option}` | Acquisition | Documents by select option |
| `/opds/search?query=X` | Acquisition | Search results |
| `/opds/opensearch.xml` | OpenSearch | Search description |
| `/download/{id}` | Binary | Proxied document download |
| `/thumb/{id}` | Binary | Proxied thumbnail |
| `/preview/{id}` | Binary | Proxied document preview |

## Reader Setup

Point your OPDS reader at:

```
http://<host>:8000/opds
```

If Basic Auth is configured, enter the credentials when prompted.

## License

MIT
