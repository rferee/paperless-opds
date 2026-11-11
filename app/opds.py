import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from app.models import PaperlessDocument

ATOM_NS = "http://www.w3.org/2005/Atom"
OPDS_NS = "http://opds-spec.org/2010/catalog"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
DC_NS = "http://purl.org/dc/terms/"

NAVIGATION_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQUISITION_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"

ET.register_namespace("", ATOM_NS)
ET.register_namespace("opds", OPDS_NS)
ET.register_namespace("opensearch", OPENSEARCH_NS)
ET.register_namespace("dc", DC_NS)

# Characters outside the XML 1.0 Char range: ElementTree escapes markup but writes these unchanged,
# which makes the feed unparsable (or, for lone surrogates, fails to encode as UTF-8)
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _el(parent: ET.Element, tag: str, text: str | None = None, **attribs: str) -> ET.Element:
    elem = ET.SubElement(parent, tag, {name: _INVALID_XML.sub("", value) for name, value in attribs.items()})
    if text is not None:
        elem.text = _INVALID_XML.sub("", text)
    return elem


def _add_pagination_links(
    feed: ET.Element,
    self_href: str,
    page: int,
    total_count: int,
    page_size: int,
    feed_type: str,
) -> None:
    total_pages = max(1, math.ceil(total_count / page_size))
    sep = "&" if "?" in self_href else "?"

    _el(feed, "link", rel="first", href=f"{self_href}{sep}page=1", type=feed_type)
    _el(feed, "link", rel="last", href=f"{self_href}{sep}page={total_pages}", type=feed_type)
    if page > 1:
        _el(feed, "link", rel="previous", href=f"{self_href}{sep}page={page - 1}", type=feed_type)
    if page < total_pages:
        _el(feed, "link", rel="next", href=f"{self_href}{sep}page={page + 1}", type=feed_type)

    _el(feed, f"{{{OPENSEARCH_NS}}}totalResults", str(total_count))
    _el(feed, f"{{{OPENSEARCH_NS}}}itemsPerPage", str(page_size))
    _el(feed, f"{{{OPENSEARCH_NS}}}startIndex", str((page - 1) * page_size + 1))


def build_root_feed(
    base_url: str,
    title: str,
    extra_nav: list[tuple[str, str, str, str]] | None = None,
) -> str:
    """Build the root navigation feed.

    extra_nav: list of (id, title, content, href) tuples for additional navigation feed entries.
    """
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    _el(feed, "id", "urn:uuid:paperless-opds-root")
    _el(feed, "title", title)
    _el(feed, "updated", _now_iso())
    author = _el(feed, "author")
    _el(author, "name", title)

    _el(feed, "link", rel="self", href=f"{base_url}/opds", type=NAVIGATION_TYPE)
    _el(feed, "link", rel="start", href=f"{base_url}/opds", type=NAVIGATION_TYPE)
    _el(feed, "link", rel="search", href=f"{base_url}/opds/opensearch.xml", type="application/opensearchdescription+xml")

    nav_entries: list[tuple[str, str, str, str, str]] = [
        ("urn:uuid:paperless-opds-all", "All Documents", "Browse all documents", f"{base_url}/opds/all", ACQUISITION_TYPE),
        ("urn:uuid:paperless-opds-recent", "Recent Documents", "Recently added documents", f"{base_url}/opds/recent", ACQUISITION_TYPE),
        ("urn:uuid:paperless-opds-tags", "By Tag", "Browse documents by tag", f"{base_url}/opds/tags", NAVIGATION_TYPE),
        ("urn:uuid:paperless-opds-correspondents", "By Correspondent", "Browse documents by correspondent", f"{base_url}/opds/correspondents", NAVIGATION_TYPE),
        ("urn:uuid:paperless-opds-doctypes", "By Document Type", "Browse documents by type", f"{base_url}/opds/document-types", NAVIGATION_TYPE),
    ]
    if extra_nav:
        nav_entries.extend((*nav, NAVIGATION_TYPE) for nav in extra_nav)

    now = _now_iso()
    for entry_id, entry_title, entry_content, href, link_type in nav_entries:
        entry = _el(feed, "entry")
        _el(entry, "title", entry_title)
        _el(entry, "id", entry_id)
        _el(entry, "updated", now)
        _el(entry, "content", entry_content, type="text")
        _el(entry, "link", rel="subsection", href=href, type=link_type)

    return _serialize(feed)


def build_navigation_feed(
    base_url: str,
    title: str,
    self_href: str,
    items: list[dict],
) -> str:
    """Build a navigation feed listing items (tags, correspondents, doc types).

    Each item dict: {id, name, count, href}; a count of None leaves out the "<n> documents" content.
    """
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    _el(feed, "id", f"urn:uuid:paperless-opds-nav-{title.lower().replace(' ', '-')}")
    _el(feed, "title", title)
    _el(feed, "updated", _now_iso())

    _el(feed, "link", rel="self", href=self_href, type=NAVIGATION_TYPE)
    _el(feed, "link", rel="start", href=f"{base_url}/opds", type=NAVIGATION_TYPE)
    _el(feed, "link", rel="search", href=f"{base_url}/opds/opensearch.xml", type="application/opensearchdescription+xml")

    now = _now_iso()
    for item in items:
        entry = _el(feed, "entry")
        _el(entry, "title", item["name"])
        _el(entry, "id", f"urn:uuid:paperless-opds-item-{item['id']}")
        _el(entry, "updated", now)
        if item["count"] is not None:
            _el(entry, "content", f"{item['count']} documents", type="text")
        _el(entry, "link", rel="subsection", href=item["href"], type=ACQUISITION_TYPE)

    return _serialize(feed)


def build_acquisition_feed(
    base_url: str,
    title: str,
    self_href: str,
    documents: list[PaperlessDocument],
    correspondent_map: dict[int, str],
    tag_map: dict[int, str],
    page: int,
    total_count: int,
    page_size: int,
    include_content: bool = False,
    download_original: bool = False,
) -> str:
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    _el(feed, "id", f"urn:uuid:paperless-opds-acq-{title.lower().replace(' ', '-')}")
    _el(feed, "title", title)
    _el(feed, "updated", _now_iso())

    sep = "&" if "?" in self_href else "?"
    _el(feed, "link", rel="self", href=f"{self_href}{sep}page={page}", type=ACQUISITION_TYPE)
    _el(feed, "link", rel="start", href=f"{base_url}/opds", type=NAVIGATION_TYPE)
    _el(feed, "link", rel="search", href=f"{base_url}/opds/opensearch.xml", type="application/opensearchdescription+xml")

    _add_pagination_links(feed, self_href, page, total_count, page_size, ACQUISITION_TYPE)

    for doc in documents:
        entry = _el(feed, "entry")
        _el(entry, "title", doc.title)
        _el(entry, "id", f"urn:uuid:paperless-doc-{doc.id}")

        if doc.correspondent and doc.correspondent in correspondent_map:
            author = _el(entry, "author")
            _el(author, "name", correspondent_map[doc.correspondent])

        _el(entry, "updated", doc.modified or doc.added or _now_iso())

        if include_content and doc.content:
            snippet = doc.content[:200].strip()
            if len(doc.content) > 200:
                snippet += "..."
            _el(entry, "content", snippet, type="text")

        if doc.created:
            _el(entry, f"{{{DC_NS}}}issued", doc.created)

        for tag_id in doc.tags:
            if tag_id in tag_map:
                _el(entry, "category", term=tag_map[tag_id], label=tag_map[tag_id])

        _el(
            entry,
            "link",
            rel="http://opds-spec.org/acquisition",
            href=f"{base_url}/download/{doc.id}",
            # Paperless serves the archived PDF instead of the original whenever one exists
            type="application/pdf" if not download_original and doc.archived_file_name else doc.mime_type,
        )
        _el(
            entry,
            "link",
            rel="http://opds-spec.org/image",
            href=f"{base_url}/preview/{doc.id}",
            type="image/jpeg",
        )
        _el(
            entry,
            "link",
            rel="http://opds-spec.org/image/thumbnail",
            href=f"{base_url}/thumb/{doc.id}",
            type="image/jpeg",
        )

    return _serialize(feed)


def build_opensearch_description(base_url: str, title: str) -> str:
    root = ET.Element("OpenSearchDescription", xmlns="http://a9.com/-/spec/opensearch/1.1/")
    _el(root, "ShortName", title)
    _el(root, "Description", f"Search {title}")
    _el(root, "InputEncoding", "UTF-8")
    _el(root, "OutputEncoding", "UTF-8")
    ET.SubElement(root, "Url", type=ACQUISITION_TYPE, template=f"{base_url}/opds/search?query={{searchTerms}}")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _serialize(root: ET.Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
