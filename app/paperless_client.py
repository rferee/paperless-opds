import json
from typing import AsyncIterator

import httpx
from cachetools import TTLCache

from app.config import settings
from app.models import (
    PaginatedResponse,
    PaperlessCorrespondent,
    PaperlessCustomField,
    PaperlessDocument,
    PaperlessDocumentType,
    PaperlessTag,
)


class PaperlessClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.paperless_url,
            headers={"Authorization": f"Token {settings.paperless_api_key}"},
            timeout=30.0,
        )
        self._cache: TTLCache = TTLCache(maxsize=32, ttl=settings.cache_ttl)

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_all_pages(self, endpoint: str) -> list[dict]:
        """Fetch all pages from a paginated Paperless endpoint."""
        results: list[dict] = []
        page = 1
        while True:
            resp = await self._client.get(endpoint, params={"page": page, "page_size": 100})
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        return results

    async def get_documents(
        self,
        page: int = 1,
        page_size: int | None = None,
        ordering: str | None = None,
        tags__id__all: int | None = None,
        correspondent__id: int | None = None,
        document_type__id: int | None = None,
    ) -> tuple[list[PaperlessDocument], int]:
        """Fetch a page of documents. Returns (documents, total_count)."""
        params: dict = {"page": page, "page_size": page_size or settings.opds_page_size}
        if ordering:
            params["ordering"] = ordering
        if tags__id__all is not None:
            params["tags__id__all"] = tags__id__all
        if correspondent__id is not None:
            params["correspondent__id"] = correspondent__id
        if document_type__id is not None:
            params["document_type__id"] = document_type__id

        resp = await self._client.get("/api/documents/", params=params)
        resp.raise_for_status()
        data = PaginatedResponse(**resp.json())
        docs = [PaperlessDocument(**r) for r in data.results]
        return docs, data.count

    async def search_documents(
        self, query: str, page: int = 1, page_size: int | None = None
    ) -> tuple[list[PaperlessDocument], int]:
        params: dict = {
            "query": query,
            "page": page,
            "page_size": page_size or settings.opds_page_size,
        }
        resp = await self._client.get("/api/documents/", params=params)
        resp.raise_for_status()
        data = PaginatedResponse(**resp.json())
        docs = [PaperlessDocument(**r) for r in data.results]
        return docs, data.count

    async def get_tags(self) -> list[PaperlessTag]:
        cached = self._cache.get("tags")
        if cached is not None:
            return cached
        raw = await self._fetch_all_pages("/api/tags/")
        tags = [PaperlessTag(**r) for r in raw]
        self._cache["tags"] = tags
        return tags

    async def get_correspondents(self) -> list[PaperlessCorrespondent]:
        cached = self._cache.get("correspondents")
        if cached is not None:
            return cached
        raw = await self._fetch_all_pages("/api/correspondents/")
        correspondents = [PaperlessCorrespondent(**r) for r in raw]
        self._cache["correspondents"] = correspondents
        return correspondents

    async def get_document_types(self) -> list[PaperlessDocumentType]:
        cached = self._cache.get("document_types")
        if cached is not None:
            return cached
        raw = await self._fetch_all_pages("/api/document_types/")
        doc_types = [PaperlessDocumentType(**r) for r in raw]
        self._cache["document_types"] = doc_types
        return doc_types

    async def get_tag_map(self) -> dict[int, str]:
        tags = await self.get_tags()
        return {t.id: t.name for t in tags}

    async def get_correspondent_map(self) -> dict[int, str]:
        correspondents = await self.get_correspondents()
        return {c.id: c.name for c in correspondents}

    async def get_document_type_map(self) -> dict[int, str]:
        doc_types = await self.get_document_types()
        return {dt.id: dt.name for dt in doc_types}

    async def get_custom_fields(self) -> list[PaperlessCustomField]:
        cached = self._cache.get("custom_fields")
        if cached is not None:
            return cached
        raw = await self._fetch_all_pages("/api/custom_fields/")
        fields = [PaperlessCustomField(**r) for r in raw]
        self._cache["custom_fields"] = fields
        return fields

    async def get_select_fields(self) -> list[PaperlessCustomField]:
        """Return only custom fields with data_type 'select'."""
        fields = await self.get_custom_fields()
        return [f for f in fields if f.data_type == "select"]

    async def get_documents_by_custom_field_select(
        self,
        field_id: int,
        option_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[PaperlessDocument], int]:
        """Fetch documents where a select custom field has a specific option selected."""
        query_expr = json.dumps(["AND", [[field_id, "exact", option_id]]])
        params: dict = {
            "page": page,
            "page_size": page_size or settings.opds_page_size,
            "custom_field_query": query_expr,
        }
        resp = await self._client.get("/api/documents/", params=params)
        resp.raise_for_status()
        data = PaginatedResponse(**resp.json())
        docs = [PaperlessDocument(**r) for r in data.results]
        return docs, data.count

    async def stream_download(self, doc_id: int, original: bool = False) -> tuple[AsyncIterator[bytes], dict]:
        """Stream a document download. Returns (byte_iterator, response_headers)."""
        params = {"original": "true"} if original else {}
        req = self._client.build_request("GET", f"/api/documents/{doc_id}/download/", params=params)
        resp = await self._client.send(req, stream=True)
        if not resp.is_success:
            # A streamed response holds its pooled connection until closed
            await resp.aclose()
            resp.raise_for_status()
        headers = {
            "content-type": resp.headers.get("content-type", "application/octet-stream"),
            "content-disposition": resp.headers.get("content-disposition", ""),
        }

        async def _iter_body() -> AsyncIterator[bytes]:
            # Release the connection on completion, mid-stream errors and client disconnects
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()

        return _iter_body(), {k: v for k, v in headers.items() if v}

    async def get_thumbnail(self, doc_id: int) -> bytes:
        """Fetch the thumbnail image bytes."""
        resp = await self._client.get(f"/api/documents/{doc_id}/thumb/")
        resp.raise_for_status()
        return resp.content

    async def get_preview_bytes(self, doc_id: int) -> bytes:
        """Fetch the full preview PDF as bytes (non-streaming)."""
        resp = await self._client.get(f"/api/documents/{doc_id}/preview/", timeout=60.0)
        resp.raise_for_status()
        return resp.content
