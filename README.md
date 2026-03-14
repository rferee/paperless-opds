# Paperless-OPDS

Middleware that exposes a [Paperless-ngx](https://docs.paperless-ngx.com/) document library as an [OPDS 1.2](https://specs.opds.io/opds-1.2) catalog feed. Point any OPDS-compatible reader (KOReader, Librera, Moon+ Reader, etc.) at it and browse/download your documents directly.

## Features

- **Full OPDS 1.2 catalog** with navigation and acquisition feeds
- **Browse by** tag, correspondent, document type, or custom select fields
- **Search** via OpenSearch
- **Proxied downloads** — readers never need Paperless credentials
- **Thumbnail & preview caching** — disk-cached JPEG images, pre-generated at startup
- **Document list pre-cache** — full document list cached in memory, refreshed periodically
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
    volumes:
      - thumbnails:/data/thumbnails
    restart: unless-stopped

volumes:
  thumbnails:
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
| `THUMBNAIL_DIR` | — | `/data/thumbnails` | Directory for cached thumbnail/preview images |
| `THUMBNAIL_PREGENERATE` | — | `true` | Pre-generate all thumbnails on startup |
| `THUMB_MAX_SIZE` | — | `300` | Max pixel size (longest side) for thumbnails |
| `PREVIEW_MAX_SIZE` | — | `1000` | Max pixel size (longest side) for previews |
| `THUMBNAIL_QUALITY` | — | `85` | JPEG quality (1-100) |
| `THUMBNAIL_BATCH_SIZE` | — | `50` | Documents per batch during pre-generation |
| `THUMBNAIL_MAX_RETRIES` | — | `3` | Retries per document on generation failure |
| `DOWNLOAD_ORIGINAL` | — | `false` | Serve the original (pre-processing) file instead of the archived version |
| `OPDS_INCLUDE_CONTENT` | — | `false` | Include document text snippets in OPDS feed entries |
| `DOCUMENT_CACHE_ENABLED` | — | `true` | Cache full document list in memory for faster feeds |
| `DOCUMENT_CACHE_REFRESH_INTERVAL` | — | `600` | Seconds between document cache refresh cycles |
| `DOCUMENT_CACHE_BATCH_SIZE` | — | `100` | Documents per page when fetching from Paperless API |

Example `config.json`:

```json
{
  "paperless": {
    "url": "https://paperless.example.com",
    "api_key": "your-api-key-here"
  }
}
```

## Thumbnail & Preview Caching

Thumbnails and previews are served as JPEG images (not PDFs). On first startup, the app fetches thumbnail/preview data from Paperless-ngx for all documents and caches resized images to disk. This ensures fast image serving for OPDS readers like Panels, KOReader, etc.

- **Thumbnails** (`/thumb/{id}`): Small JPEG (~300px longest side), sourced from Paperless's thumbnail API
- **Previews** (`/preview/{id}`): Larger JPEG (~1000px longest side), rendered from the first page of the document PDF

Cached images are stored in `THUMBNAIL_DIR` (default `/data/thumbnails`) under `thumb/` and `preview/` subdirectories. Mount this as a Docker volume to persist across container restarts.

Pre-generation runs in the background at startup and processes documents sequentially in batches. If a thumbnail/preview is requested before pre-generation reaches it, it will be generated on demand.

Set `THUMBNAIL_PREGENERATE=false` to disable startup pre-generation (images will only be generated on first request).

## Document List Pre-Cache

When `DOCUMENT_CACHE_ENABLED=true` (default), the app loads the full document list into memory at startup and refreshes it every `DOCUMENT_CACHE_REFRESH_INTERVAL` seconds (default 600 / 10 minutes). This makes OPDS feed responses much faster since they don't need to query the Paperless API on every request.

The cache refresh cycle also checks for missing thumbnails and generates them, combining both operations into a single background loop. When the cache is enabled, it replaces the standalone `THUMBNAIL_PREGENERATE` behavior.

Search always queries the Paperless API directly (full-text search cannot be cached). If the cache is not yet ready (during initial startup), requests fall back to direct API calls.

Set `DOCUMENT_CACHE_ENABLED=false` to disable caching and use direct API calls for every request (original behavior).

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
