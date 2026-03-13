import asyncio
import io
import logging
from pathlib import Path

import pymupdf
from PIL import Image

from app.config import settings
from app.paperless_client import PaperlessClient

logger = logging.getLogger("uvicorn.error")


class ThumbnailCacheManager:
    def __init__(self, client: PaperlessClient) -> None:
        self._client = client
        self._thumb_dir = Path(settings.thumbnail_dir) / "thumb"
        self._preview_dir = Path(settings.thumbnail_dir) / "preview"

    def get_thumb_path(self, doc_id: int) -> Path:
        return self._thumb_dir / f"{doc_id}.jpg"

    def get_preview_path(self, doc_id: int) -> Path:
        return self._preview_dir / f"{doc_id}.jpg"

    def has_thumb(self, doc_id: int) -> bool:
        p = self.get_thumb_path(doc_id)
        return p.exists() and p.stat().st_size > 0

    def has_preview(self, doc_id: int) -> bool:
        p = self.get_preview_path(doc_id)
        return p.exists() and p.stat().st_size > 0

    async def generate_thumb(self, doc_id: int) -> Path:
        """Fetch thumbnail from Paperless and resize to a small JPEG."""
        data, _ = await self._client.get_thumbnail(doc_id)
        path = self.get_thumb_path(doc_id)

        def _process() -> None:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((settings.thumb_max_size, settings.thumb_max_size))
            img = img.convert("RGB")
            img.save(path, "JPEG", quality=settings.thumbnail_quality)

        await asyncio.to_thread(_process)
        return path

    async def generate_preview(self, doc_id: int) -> Path:
        """Fetch preview PDF from Paperless and render first page as JPEG."""
        pdf_bytes = await self._client.get_preview_bytes(doc_id)
        path = self.get_preview_path(doc_id)

        def _process() -> None:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                page = doc[0]
                # Calculate zoom so longest side = preview_max_size
                max_dim = max(page.rect.width, page.rect.height)
                zoom = settings.preview_max_size / max_dim if max_dim > 0 else 1.0
                mat = pymupdf.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                img.save(path, "JPEG", quality=settings.thumbnail_quality)
            finally:
                doc.close()

        await asyncio.to_thread(_process)
        return path

    async def ensure_thumb(self, doc_id: int) -> Path:
        if self.has_thumb(doc_id):
            return self.get_thumb_path(doc_id)
        logger.info("[thumbnails] On-demand generating thumb for doc %d", doc_id)
        return await self.generate_thumb(doc_id)

    async def ensure_preview(self, doc_id: int) -> Path:
        if self.has_preview(doc_id):
            return self.get_preview_path(doc_id)
        logger.info("[thumbnails] On-demand generating preview for doc %d", doc_id)
        return await self.generate_preview(doc_id)

    async def pregenerate_all(self) -> None:
        """Pre-generate missing thumbnails and previews, streaming pages from Paperless."""
        logger.info("[thumbnails] Starting pre-generation worker")
        logger.info("[thumbnails] Cache directory: %s", settings.thumbnail_dir)
        logger.info("[thumbnails] Thumb size: %dpx, Preview size: %dpx, Quality: %d%%",
                     settings.thumb_max_size, settings.preview_max_size, settings.thumbnail_quality)

        generated = 0
        failed = 0
        skipped = 0
        total_docs = 0
        api_page = 1
        batch_size = settings.thumbnail_batch_size
        batch_buffer: list[int] = []

        while True:
            logger.info("[thumbnails] Fetching document page %d from Paperless...", api_page)
            try:
                resp = await self._client._client.get(
                    "/api/documents/",
                    params={"page": api_page, "page_size": 100, "fields": "id"},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                logger.exception("[thumbnails] Failed to fetch document page %d — aborting", api_page)
                break

            page_ids = [r["id"] for r in data.get("results", [])]
            page_total = data.get("count", 0)
            total_docs += len(page_ids)

            if api_page == 1:
                logger.info("[thumbnails] Paperless reports %d total documents", page_total)

            # Filter to only docs missing thumb or preview
            for doc_id in page_ids:
                if not self.has_thumb(doc_id) or not self.has_preview(doc_id):
                    batch_buffer.append(doc_id)
                else:
                    skipped += 1

            # Process full batches from the buffer
            while len(batch_buffer) >= batch_size:
                batch = batch_buffer[:batch_size]
                batch_buffer = batch_buffer[batch_size:]

                logger.info("[thumbnails] Processing batch (docs: %s)",
                             ", ".join(str(d) for d in batch))

                for doc_id in batch:
                    success = await self._generate_with_retry(doc_id)
                    if success:
                        generated += 1
                        logger.info("[thumbnails] Generated images for doc %d (%d generated, %d failed, %d skipped)",
                                     doc_id, generated, failed, skipped)
                    else:
                        failed += 1
                        logger.warning("[thumbnails] Skipped doc %d after retries (%d generated, %d failed, %d skipped)",
                                        doc_id, generated, failed, skipped)

            # Check if there are more pages
            if not data.get("next"):
                break
            api_page += 1

        # Process any remaining docs in the buffer
        if batch_buffer:
            logger.info("[thumbnails] Processing final batch (docs: %s)",
                         ", ".join(str(d) for d in batch_buffer))
            for doc_id in batch_buffer:
                success = await self._generate_with_retry(doc_id)
                if success:
                    generated += 1
                    logger.info("[thumbnails] Generated images for doc %d (%d generated, %d failed, %d skipped)",
                                 doc_id, generated, failed, skipped)
                else:
                    failed += 1
                    logger.warning("[thumbnails] Skipped doc %d after retries (%d generated, %d failed, %d skipped)",
                                    doc_id, generated, failed, skipped)

        logger.info("[thumbnails] Pre-generation complete: %d total docs, %d already cached, %d generated, %d failed",
                     total_docs, skipped, generated, failed)

    async def _generate_with_retry(self, doc_id: int) -> bool:
        """Generate thumb and preview for a document with exponential backoff."""
        for attempt in range(settings.thumbnail_max_retries):
            try:
                if not self.has_thumb(doc_id):
                    logger.debug("[thumbnails] Fetching thumb for doc %d...", doc_id)
                    await self.generate_thumb(doc_id)
                if not self.has_preview(doc_id):
                    logger.debug("[thumbnails] Fetching preview for doc %d...", doc_id)
                    await self.generate_preview(doc_id)
                return True
            except Exception as exc:
                if attempt < settings.thumbnail_max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning(
                        "[thumbnails] Error generating doc %d (attempt %d/%d): %s — retrying in %ds",
                        doc_id, attempt + 1, settings.thumbnail_max_retries, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "[thumbnails] Failed doc %d after %d attempts: %s",
                        doc_id, settings.thumbnail_max_retries, exc,
                    )
        return False
