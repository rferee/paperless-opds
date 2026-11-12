import asyncio
import logging

from cachetools import TTLCache

from app.config import settings
from app.models import PaperlessDocument
from app.paperless_client import PaperlessClient
from app.thumbnail_cache import ThumbnailCacheManager

logger = logging.getLogger("uvicorn.error")


def _thumb_check_done_callback(task: asyncio.Task) -> None:
    """Log any exception from a background thumbnail check."""
    if task.cancelled():
        logger.info("[doc-cache] Thumbnail check was cancelled")
        return
    exc = task.exception()
    if exc:
        logger.error("[doc-cache] Thumbnail check failed: %s", exc, exc_info=exc)


class DocumentCacheManager:
    def __init__(
        self,
        client: PaperlessClient,
        thumbnail_manager: ThumbnailCacheManager,
        response_cache: TTLCache,
    ) -> None:
        self._client = client
        self._thumbnail_manager = thumbnail_manager
        self._response_cache = response_cache
        self._documents: dict[int, PaperlessDocument] = {}
        self._ready = asyncio.Event()
        self._thumb_task: asyncio.Task | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def get_documents(
        self,
        page: int = 1,
        page_size: int | None = None,
        ordering: str | None = None,
        tags__id__all: int | None = None,
        correspondent__id: int | None = None,
        document_type__id: int | None = None,
    ) -> tuple[list[PaperlessDocument], int]:
        if not self._ready.is_set():
            return await self._client.get_documents(
                page=page,
                page_size=page_size,
                ordering=ordering,
                tags__id__all=tags__id__all,
                correspondent__id=correspondent__id,
                document_type__id=document_type__id,
            )

        psize = page_size or settings.opds_page_size
        docs = list(self._documents.values())

        # Filter
        if tags__id__all is not None:
            docs = [d for d in docs if tags__id__all in d.tags]
        if correspondent__id is not None:
            docs = [d for d in docs if d.correspondent == correspondent__id]
        if document_type__id is not None:
            docs = [d for d in docs if d.document_type == document_type__id]

        # Sort
        if ordering:
            desc = ordering.startswith("-")
            field = ordering.lstrip("-")
            docs.sort(
                key=lambda d: (getattr(d, field, None) or "") if isinstance(getattr(d, field, None), str) else (getattr(d, field, None) or 0),
                reverse=desc,
            )

        total = len(docs)
        start = (page - 1) * psize
        return docs[start : start + psize], total

    async def get_documents_by_custom_field_select(
        self,
        field_id: int,
        option_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[PaperlessDocument], int]:
        if not self._ready.is_set():
            return await self._client.get_documents_by_custom_field_select(
                field_id=field_id, option_id=option_id, page=page, page_size=page_size,
            )

        psize = page_size or settings.opds_page_size
        docs = [
            d for d in self._documents.values()
            if any(cfi.field == field_id and str(cfi.value) == option_id for cfi in d.custom_fields)
        ]

        total = len(docs)
        start = (page - 1) * psize
        return docs[start : start + psize], total

    def get_tag_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for doc in self._documents.values():
            for tag_id in doc.tags:
                counts[tag_id] = counts.get(tag_id, 0) + 1
        return counts

    def get_correspondent_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for doc in self._documents.values():
            if doc.correspondent is not None:
                counts[doc.correspondent] = counts.get(doc.correspondent, 0) + 1
        return counts

    def get_document_type_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for doc in self._documents.values():
            if doc.document_type is not None:
                counts[doc.document_type] = counts.get(doc.document_type, 0) + 1
        return counts

    def get_custom_field_select_counts(self, field_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for doc in self._documents.values():
            for cfi in doc.custom_fields:
                if cfi.field == field_id and cfi.value is not None:
                    key = str(cfi.value)
                    counts[key] = counts.get(key, 0) + 1
        return counts

    async def run_sync_loop(self) -> None:
        try:
            while True:
                try:
                    await self._sync_once()
                except Exception:
                    logger.exception("[doc-cache] Sync failed, will retry next cycle")
                await asyncio.sleep(settings.document_cache_refresh_interval)
        finally:
            # Stop the background thumbnail check along with the sync loop (lifespan shutdown cancels it)
            if self._thumb_task is not None and not self._thumb_task.done():
                self._thumb_task.cancel()

    async def _sync_once(self) -> None:
        logger.info("[doc-cache] Starting sync cycle...")
        new_docs = await self._fetch_all_documents()

        # Log changes
        old_ids = set(self._documents.keys())
        new_ids = set(new_docs.keys())
        added = new_ids - old_ids
        removed = old_ids - new_ids
        if added:
            logger.info("[doc-cache] %d new document(s): %s", len(added), sorted(added))
        if removed:
            logger.info("[doc-cache] %d removed document(s): %s", len(removed), sorted(removed))

        # Atomic swap
        self._documents = new_docs
        logger.info("[doc-cache] Cache updated: %d documents", len(new_docs))

        # Drop cached images of documents that no longer exist in Paperless
        try:
            pruned = await asyncio.to_thread(self._thumbnail_manager.prune, new_ids)
        except OSError:
            logger.exception("[doc-cache] Failed to prune cached images of removed documents")
        else:
            if pruned:
                logger.info("[doc-cache] Removed %d cached image(s) of deleted documents", pruned)

        # Clear response cache so stale XML is not served
        self._response_cache.clear()

        # Set ready after first successful sync
        if not self._ready.is_set():
            self._ready.set()
            logger.info("[doc-cache] Initial cache ready")

        # Check for missing thumbnails in the background, so a long backfill never delays the next refresh.
        # THUMBNAIL_PREGENERATE=false leaves images to be generated on first request, as without the cache.
        if settings.thumbnail_pregenerate:
            if self._thumb_task is not None and not self._thumb_task.done():
                logger.info("[doc-cache] Previous thumbnail check still running, not starting another")
            else:
                self._thumb_task = asyncio.create_task(self._check_thumbnails(list(new_ids)))
                self._thumb_task.add_done_callback(_thumb_check_done_callback)

    async def _fetch_all_documents(self) -> dict[int, PaperlessDocument]:
        docs: dict[int, PaperlessDocument] = {}
        api_page = 0
        batch_size = settings.document_cache_batch_size

        # Feeds only show a short content snippet: don't hold every document's full OCR text in memory
        pages = self._client.iter_pages("/api/documents/", page_size=batch_size, truncate_content="true")
        async for data in pages:
            api_page += 1
            logger.info("[doc-cache] Fetched document page %d (batch size %d)", api_page, batch_size)

            for r in data.get("results", []):
                doc = PaperlessDocument(**r)
                docs[doc.id] = doc

            if api_page == 1:
                logger.info("[doc-cache] Paperless reports %d total documents", data.get("count", 0))

        return docs

    async def _check_thumbnails(self, doc_ids: list[int]) -> None:
        missing: list[int] = []
        for doc_id in doc_ids:
            if not self._thumbnail_manager.has_thumb(doc_id) or not self._thumbnail_manager.has_preview(doc_id):
                missing.append(doc_id)

        if not missing:
            logger.info("[doc-cache] All thumbnails up to date")
            return

        logger.info("[doc-cache] %d document(s) missing thumbnails, generating...", len(missing))
        generated = 0
        failed = 0
        batch_size = settings.thumbnail_batch_size

        for i in range(0, len(missing), batch_size):
            batch = missing[i : i + batch_size]
            logger.info("[doc-cache] Processing thumbnail batch %d-%d of %d",
                        i + 1, min(i + batch_size, len(missing)), len(missing))
            batch_generated, batch_failed = await self._thumbnail_manager.generate_batch(batch)
            generated += batch_generated
            failed += batch_failed

        logger.info("[doc-cache] Thumbnail check complete: %d generated, %d failed, %d already cached",
                    generated, failed, len(doc_ids) - len(missing))
