from typing import Any

from pydantic import BaseModel


class CustomFieldInstance(BaseModel):
    field: int
    value: Any = None


class PaperlessDocument(BaseModel):
    id: int
    title: str = ""
    content: str | None = None
    correspondent: int | None = None
    document_type: int | None = None
    tags: list[int] = []
    created: str | None = None
    modified: str | None = None
    added: str | None = None
    original_file_name: str | None = None
    archived_file_name: str | None = None
    mime_type: str = "application/octet-stream"
    page_count: int | None = None
    custom_fields: list[CustomFieldInstance] = []


class PaperlessTag(BaseModel):
    id: int
    name: str
    slug: str = ""
    document_count: int = 0


class PaperlessCorrespondent(BaseModel):
    id: int
    name: str
    slug: str = ""
    document_count: int = 0


class PaperlessDocumentType(BaseModel):
    id: int
    name: str
    slug: str = ""
    document_count: int = 0


class PaperlessCustomField(BaseModel):
    id: int
    name: str
    data_type: str
    extra_data: Any = None
    document_count: int = 0


class PaginatedResponse(BaseModel):
    count: int = 0
    next: str | None = None
    previous: str | None = None
    results: list[dict] = []
