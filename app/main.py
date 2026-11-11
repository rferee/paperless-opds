import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cachetools import TTLCache
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app.auth import request_is_authorized, verify_auth
from app.config import settings
from app.opds import (
    ACQUISITION_TYPE,
    NAVIGATION_TYPE,
    build_acquisition_feed,
    build_navigation_feed,
    build_opensearch_description,
    build_root_feed,
)
from app.document_cache import DocumentCacheManager
from app.paperless_client import PaperlessClient
from app.thumbnail_cache import ThumbnailCacheManager

logger = logging.getLogger("uvicorn.error")

client: PaperlessClient
cache_manager: ThumbnailCacheManager
doc_cache_manager: DocumentCacheManager | None = None
_response_cache: TTLCache = TTLCache(maxsize=256, ttl=600)


def _pregen_done_callback(task: asyncio.Task) -> None:
    """Log any exception from the background pre-generation task."""
    if task.cancelled():
        logger.info("Thumbnail pre-generation was cancelled")
        return
    exc = task.exception()
    if exc:
        logger.error("Thumbnail pre-generation failed: %s", exc, exc_info=exc)


def _sync_done_callback(task: asyncio.Task) -> None:
    """Log any exception from the background document cache sync task."""
    if task.cancelled():
        logger.info("Document cache sync was cancelled")
        return
    exc = task.exception()
    if exc:
        logger.error("Document cache sync failed: %s", exc, exc_info=exc)


def _warn_about_auth_settings() -> None:
    """Warn when Basic Auth is off, only half configured, or accepts empty credentials."""
    username, password = settings.opds_username, settings.opds_password
    if username is None and password is None:
        logger.warning("Basic Auth is disabled (OPDS_USERNAME and OPDS_PASSWORD are unset): "
                       "anyone who can reach this service can read and download every document")
    elif username is None or password is None:
        set_name, missing_name = (
            ("OPDS_PASSWORD", "OPDS_USERNAME") if username is None else ("OPDS_USERNAME", "OPDS_PASSWORD")
        )
        logger.warning("%s is set but %s is missing, so Basic Auth is DISABLED: "
                       "anyone who can reach this service can read and download every document",
                       set_name, missing_name)
    else:
        empty = [name for name, value in (("OPDS_USERNAME", username), ("OPDS_PASSWORD", password)) if value == ""]
        if empty:
            logger.warning("Basic Auth is enabled with an empty %s: requests that send the empty value are accepted",
                           " and ".join(empty))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, cache_manager, doc_cache_manager
    _warn_about_auth_settings()
    client = PaperlessClient()
    cache_manager = ThumbnailCacheManager(client)
    sync_task = None
    pregen_task = None

    if settings.document_cache_enabled:
        doc_cache_manager = DocumentCacheManager(client, cache_manager, _response_cache)
        logger.info("Launching document cache sync worker (refresh every %ds)...",
                     settings.document_cache_refresh_interval)
        sync_task = asyncio.create_task(doc_cache_manager.run_sync_loop())
        sync_task.add_done_callback(_sync_done_callback)
    elif settings.thumbnail_pregenerate:
        logger.info("Launching thumbnail pre-generation worker...")
        pregen_task = asyncio.create_task(cache_manager.pregenerate_all())
        pregen_task.add_done_callback(_pregen_done_callback)

    yield

    if sync_task and not sync_task.done():
        sync_task.cancel()
    if pregen_task and not pregen_task.done():
        pregen_task.cancel()
    await client.close()


app = FastAPI(title=settings.opds_title, lifespan=lifespan)


def _upstream_target(exc: httpx.HTTPError) -> str:
    """Method and path of the failed Paperless request, without the host or any credentials in the URL."""
    try:
        return f"{exc.request.method} {exc.request.url.raw_path.decode('ascii', 'replace')}"
    except RuntimeError:  # .request is not set
        return "request"


@app.exception_handler(httpx.HTTPStatusError)
async def paperless_status_error_handler(request: Request, exc: httpx.HTTPStatusError) -> Response:
    """Answer an error status from Paperless with 404 (if Paperless said 404) or 502 instead of a 500."""
    status = exc.response.status_code
    logger.warning("Paperless returned HTTP %d for %s", status, _upstream_target(exc))
    if status == 404:
        return Response(content="Not found", status_code=404)
    return Response(content="Paperless request failed", status_code=502)


@app.exception_handler(httpx.RequestError)
async def paperless_request_error_handler(request: Request, exc: httpx.RequestError) -> Response:
    """Answer a timeout (504) or connection error (502) talking to Paperless instead of a 500."""
    logger.warning("Paperless %s failed: %s: %s", _upstream_target(exc), type(exc).__name__, exc)
    if isinstance(exc, httpx.TimeoutException):
        return Response(content="Paperless request timed out", status_code=504)
    return Response(content="Paperless request failed", status_code=502)


@app.middleware("http")
async def response_cache_middleware(request: Request, call_next):
    """Cache OPDS XML responses for 10 minutes."""
    path = request.url.path
    # Only authorized requests may read or fill the cache; others reach verify_auth and get a 401
    if path.startswith("/opds") and await request_is_authorized(request):
        # Feed links use _base_url, which honours X-Forwarded-Proto, so the key must too
        key = _base_url(request) + request.url.path + ("?" + request.url.query if request.url.query else "")
        cached = _response_cache.get(key)
        if cached is not None:
            content, media_type = cached
            return Response(content=content, media_type=media_type)
        response = await call_next(request)
        if response.status_code == 200:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
            # call_next's response carries its type only in its headers (media_type is None)
            media_type = response.headers.get("content-type", "application/xml")
            _response_cache[key] = (body, media_type)
            return Response(content=body, media_type=media_type)
        return response
    return await call_next(request)


@app.get("/")
async def root():
    return RedirectResponse(url="/opds")


def _extract_select_options(extra_data: Any) -> list[dict[str, str]]:
    """Extract option id/label pairs from a custom field's extra_data.

    Returns list of {"id": "hex_id", "label": "Option Name"}.
    """
    raw: list = []
    if isinstance(extra_data, dict):
        raw = extra_data.get("select_options", [])
    elif isinstance(extra_data, list):
        raw = extra_data
    result = []
    for opt in raw:
        if isinstance(opt, dict):
            result.append({"id": str(opt.get("id", "")), "label": str(opt.get("label", opt.get("id", "")))})
        else:
            result.append({"id": str(opt), "label": str(opt)})
    return result


def _base_url(request: Request) -> str:
    url = str(request.base_url).rstrip("/")
    # Respect X-Forwarded-Proto from reverse proxy
    proto = request.headers.get("x-forwarded-proto")
    if proto and url.startswith("http://") and proto == "https":
        url = "https://" + url[7:]
    return url


async def _build_acquisition(
    request: Request,
    title: str,
    self_path: str,
    page: int,
    **filters,
) -> Response:
    base = _base_url(request)
    if doc_cache_manager is not None:
        docs, total = await doc_cache_manager.get_documents(page=page, **filters)
    else:
        docs, total = await client.get_documents(page=page, **filters)
    tag_map = await client.get_tag_map()
    corr_map = await client.get_correspondent_map()
    xml = build_acquisition_feed(
        base_url=base,
        title=title,
        self_href=f"{base}{self_path}",
        documents=docs,
        correspondent_map=corr_map,
        tag_map=tag_map,
        page=page,
        total_count=total,
        page_size=settings.opds_page_size,
        include_content=settings.opds_include_content,
        download_original=settings.download_original,
    )
    return Response(content=xml, media_type=ACQUISITION_TYPE)


# --- Navigation feeds ---


@app.get("/opds", dependencies=[Depends(verify_auth)])
async def root_feed(request: Request) -> Response:
    base = _base_url(request)
    select_fields = await client.get_select_fields()
    extra_nav = [
        (
            f"urn:uuid:paperless-opds-cf-{f.id}",
            f"By {f.name}",
            f"Browse documents by {f.name}",
            f"{base}/opds/custom-fields/{f.id}",
        )
        for f in sorted(select_fields, key=lambda f: f.name)
    ]
    xml = build_root_feed(base, settings.opds_title, extra_nav=extra_nav)
    return Response(content=xml, media_type=NAVIGATION_TYPE)


@app.get("/opds/tags", dependencies=[Depends(verify_auth)])
async def tags_feed(request: Request) -> Response:
    base = _base_url(request)
    tags = await client.get_tags()
    tag_counts = doc_cache_manager.get_tag_counts() if doc_cache_manager and doc_cache_manager.is_ready else None
    items = [
        {"id": t.id, "name": t.name, "count": tag_counts.get(t.id, 0) if tag_counts else t.document_count, "href": f"{base}/opds/tags/{t.id}"}
        for t in sorted(tags, key=lambda t: t.name)
    ]
    xml = build_navigation_feed(base, "Tags", f"{base}/opds/tags", items)
    return Response(content=xml, media_type=NAVIGATION_TYPE)


@app.get("/opds/correspondents", dependencies=[Depends(verify_auth)])
async def correspondents_feed(request: Request) -> Response:
    base = _base_url(request)
    correspondents = await client.get_correspondents()
    corr_counts = doc_cache_manager.get_correspondent_counts() if doc_cache_manager and doc_cache_manager.is_ready else None
    items = [
        {"id": c.id, "name": c.name, "count": corr_counts.get(c.id, 0) if corr_counts else c.document_count, "href": f"{base}/opds/correspondents/{c.id}"}
        for c in sorted(correspondents, key=lambda c: c.name)
    ]
    xml = build_navigation_feed(base, "Correspondents", f"{base}/opds/correspondents", items)
    return Response(content=xml, media_type=NAVIGATION_TYPE)


@app.get("/opds/document-types", dependencies=[Depends(verify_auth)])
async def document_types_feed(request: Request) -> Response:
    base = _base_url(request)
    doc_types = await client.get_document_types()
    dt_counts = doc_cache_manager.get_document_type_counts() if doc_cache_manager and doc_cache_manager.is_ready else None
    items = [
        {"id": dt.id, "name": dt.name, "count": dt_counts.get(dt.id, 0) if dt_counts else dt.document_count, "href": f"{base}/opds/document-types/{dt.id}"}
        for dt in sorted(doc_types, key=lambda dt: dt.name)
    ]
    xml = build_navigation_feed(base, "Document Types", f"{base}/opds/document-types", items)
    return Response(content=xml, media_type=NAVIGATION_TYPE)


# --- Acquisition feeds ---


@app.get("/opds/all", dependencies=[Depends(verify_auth)])
async def all_documents(request: Request, page: int = Query(1, ge=1)) -> Response:
    return await _build_acquisition(request, "All Documents", "/opds/all", page, ordering="title")


@app.get("/opds/recent", dependencies=[Depends(verify_auth)])
async def recent_documents(request: Request, page: int = Query(1, ge=1)) -> Response:
    return await _build_acquisition(request, "Recent Documents", "/opds/recent", page, ordering="-added")


@app.get("/opds/tags/{tag_id}", dependencies=[Depends(verify_auth)])
async def tag_documents(request: Request, tag_id: int, page: int = Query(1, ge=1)) -> Response:
    tag_map = await client.get_tag_map()
    name = tag_map.get(tag_id, f"Tag {tag_id}")
    return await _build_acquisition(request, name, f"/opds/tags/{tag_id}", page, tags__id__all=tag_id)


@app.get("/opds/correspondents/{corr_id}", dependencies=[Depends(verify_auth)])
async def correspondent_documents(request: Request, corr_id: int, page: int = Query(1, ge=1)) -> Response:
    corr_map = await client.get_correspondent_map()
    name = corr_map.get(corr_id, f"Correspondent {corr_id}")
    return await _build_acquisition(request, name, f"/opds/correspondents/{corr_id}", page, correspondent__id=corr_id)


@app.get("/opds/document-types/{dt_id}", dependencies=[Depends(verify_auth)])
async def document_type_documents(request: Request, dt_id: int, page: int = Query(1, ge=1)) -> Response:
    dt_map = await client.get_document_type_map()
    name = dt_map.get(dt_id, f"Document Type {dt_id}")
    return await _build_acquisition(request, name, f"/opds/document-types/{dt_id}", page, document_type__id=dt_id)


# --- Custom field feeds ---


@app.get("/opds/custom-fields/{field_id}", dependencies=[Depends(verify_auth)])
async def custom_field_options_feed(request: Request, field_id: int) -> Response:
    """Navigation feed listing the select options for a custom field."""
    base = _base_url(request)
    select_fields = await client.get_select_fields()
    field = next((f for f in select_fields if f.id == field_id), None)
    if field is None:
        return Response(content="Not found", status_code=404)

    options = _extract_select_options(field.extra_data)
    cf_counts = doc_cache_manager.get_custom_field_select_counts(field_id) if doc_cache_manager and doc_cache_manager.is_ready else None

    items = [
        {
            "id": f"cf-{field_id}-{opt['id']}",
            "name": opt["label"],
            # Without a ready document cache there is no count to show: Paperless has no per-option document count
            "count": cf_counts.get(opt["id"], 0) if cf_counts is not None else None,
            "href": f"{base}/opds/custom-fields/{field_id}/{quote(opt['id'], safe='')}",
        }
        for opt in options
    ]
    xml = build_navigation_feed(
        base, f"By {field.name}", f"{base}/opds/custom-fields/{field_id}", items
    )
    return Response(content=xml, media_type=NAVIGATION_TYPE)


@app.get("/opds/custom-fields/{field_id}/{option_id}", dependencies=[Depends(verify_auth)])
async def custom_field_option_documents(
    request: Request, field_id: int, option_id: str, page: int = Query(1, ge=1)
) -> Response:
    """Acquisition feed for documents matching a specific select option."""
    base = _base_url(request)
    select_fields = await client.get_select_fields()
    field = next((f for f in select_fields if f.id == field_id), None)
    option_name = option_id
    if field is not None:
        options = _extract_select_options(field.extra_data)
        match = next((o for o in options if o["id"] == option_id), None)
        if match:
            option_name = match["label"]

    if doc_cache_manager is not None:
        docs, total = await doc_cache_manager.get_documents_by_custom_field_select(
            field_id=field_id, option_id=option_id, page=page,
        )
    else:
        docs, total = await client.get_documents_by_custom_field_select(
            field_id=field_id, option_id=option_id, page=page,
        )
    tag_map = await client.get_tag_map()
    corr_map = await client.get_correspondent_map()
    xml = build_acquisition_feed(
        base_url=base,
        title=option_name,
        self_href=f"{base}/opds/custom-fields/{field_id}/{quote(option_id, safe='')}",
        documents=docs,
        correspondent_map=corr_map,
        tag_map=tag_map,
        page=page,
        total_count=total,
        page_size=settings.opds_page_size,
        include_content=settings.opds_include_content,
        download_original=settings.download_original,
    )
    return Response(content=xml, media_type=ACQUISITION_TYPE)


# --- Search ---


@app.get("/opds/search", dependencies=[Depends(verify_auth)])
async def search(request: Request, query: str = Query(""), page: int = Query(1, ge=1)) -> Response:
    base = _base_url(request)
    docs, total = await client.search_documents(query=query, page=page)
    tag_map = await client.get_tag_map()
    corr_map = await client.get_correspondent_map()
    xml = build_acquisition_feed(
        base_url=base,
        title=f"Search: {query}",
        self_href=f"{base}/opds/search?{urlencode({'query': query})}",
        documents=docs,
        correspondent_map=corr_map,
        tag_map=tag_map,
        page=page,
        total_count=total,
        page_size=settings.opds_page_size,
        include_content=settings.opds_include_content,
        download_original=settings.download_original,
    )
    return Response(content=xml, media_type=ACQUISITION_TYPE)


@app.get("/opds/opensearch.xml", dependencies=[Depends(verify_auth)])
async def opensearch(request: Request) -> Response:
    xml = build_opensearch_description(_base_url(request), settings.opds_title)
    return Response(content=xml, media_type="application/opensearchdescription+xml")


# --- Proxy routes ---


@app.get("/download/{doc_id}", dependencies=[Depends(verify_auth)])
async def download_document(doc_id: int) -> StreamingResponse:
    stream, headers = await client.stream_download(doc_id, original=settings.download_original)
    return StreamingResponse(content=stream, headers=headers)


@app.get("/thumb/{doc_id}", dependencies=[Depends(verify_auth)], response_model=None)
async def thumbnail(doc_id: int):
    try:
        path = await cache_manager.ensure_thumb(doc_id)
        return FileResponse(path, media_type="image/jpeg")
    except Exception:
        logger.exception("Failed to generate thumbnail for doc %d", doc_id)
        return Response(status_code=502, content="Failed to generate thumbnail")


@app.get("/preview/{doc_id}", dependencies=[Depends(verify_auth)], response_model=None)
async def preview(doc_id: int):
    try:
        path = await cache_manager.ensure_preview(doc_id)
        return FileResponse(path, media_type="image/jpeg")
    except Exception:
        logger.exception("Failed to generate preview for doc %d", doc_id)
        return Response(status_code=502, content="Failed to generate preview")
