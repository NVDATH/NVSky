"""
Shared low-level helpers for the Bluesky client modules: lazy atproto models, tolerant attribute
access for SDK objects and raw JSON, and post-text link facets. Leaf module: imports nothing from
the other client modules.
"""
import re

from logHandler import log




class _LazyModels:
    """atproto.models takes ~0.7 s to import: load it on first use, not at NVDA start."""

    def __getattr__(self, name):
        from atproto import models as realModels
        return getattr(realModels, name)


models = _LazyModels()


def _dict_get(record):
    """
    Returns a working .get-style accessor for `record`, or None if it
    genuinely isn't dict-like. Some SDK wrapper objects have a `.get`
    ATTRIBUTE that exists (hasattr is True) but resolves to None instead of
    a callable method -- checking callable() here, not just hasattr(),
    is what actually matters. This was the real cause of a recurring
    'NoneType' object is not callable crash during sync.
    """
    getter = getattr(record, "get", None)
    return getter if callable(getter) else None


def _view_field(obj, name):
    """
    getattr(obj, name, None) isn't safe when `obj` is a plain dict instead of a resolved model (a
    union member the SDK couldn't resolve): a name like "items" then gives dict's own .items()
    method instead of None. A callable result is treated as missing and the dict .get() path is
    used.
    """
    value = getattr(obj, name, None)
    if value is None or callable(value):
        getter = _dict_get(obj)
        value = getter(name) if getter is not None else None
    return value


def expand_link_facets(text: str, facets) -> str:
    """Replaces each link facet's visible text with its full URL. Official
    clients store long URLs shortened ("example.com/pa...") in the text and
    keep the real one only in the facet, which this add-on doesn't render.
    Handles dict, namespace, and typed-model facets."""
    if not text or not facets:
        return text
    try:
        data = text.encode("utf-8")
        edits = []
        for facet in facets:
            index = _view_field(facet, "index")
            start = _view_field(index, "byteStart")
            if start is None:
                start = _view_field(index, "byte_start")
            end = _view_field(index, "byteEnd")
            if end is None:
                end = _view_field(index, "byte_end")
            uri = None
            for feature in _view_field(facet, "features") or []:
                uri = _view_field(feature, "uri")
                if uri:
                    break
            if uri and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(data):
                edits.append((start, end, uri))
        previousStart = len(data)
        for start, end, uri in sorted(edits, reverse=True):
            if end > previousStart or data[start:end].decode("utf-8", errors="replace") == uri:
                continue
            data = data[:start] + uri.encode("utf-8") + data[end:]
            previousStart = start
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"NVSky: expanding link facets failed: {e}")
        return text


URL_REGEX = re.compile(r'https?://[^\s<>"\']+')


MENTION_REGEX = re.compile(r'(?<!\w)@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})\b')


def find_all_urls(text: str) -> list:
    """Every URL in text, in order, trailing punctuation stripped."""
    return [m.group(0).rstrip(').,!?') for m in URL_REGEX.finditer(text)]


def build_facets(client, text: str) -> list:
    """
    Scans post text for @handle mentions and http(s) URLs and builds the
    RichText facets Bluesky needs to render them as clickable/highlighted
    text -- entirely client-side work, the server does none of it.
    Byte offsets (not character offsets) are required by the lexicon,
    since facets index into the UTF-8 encoding of the text, not Python
    string indices.
    """
    facets = []

    for m in URL_REGEX.finditer(text):
        url = m.group(0).rstrip(').,!?')
        startByte = len(text[:m.start()].encode("utf-8"))
        endByte = startByte + len(url.encode("utf-8"))
        facets.append({
            "index": {"byteStart": startByte, "byteEnd": endByte},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })

    for m in MENTION_REGEX.finditer(text):
        handle = m.group(1)
        try:
            did = client.com.atproto.identity.resolve_handle(params={"handle": handle}).did
        except Exception:
            continue  # not a real handle -- leave as plain text, same as bsky.app does
        startByte = len(text[:m.start()].encode("utf-8"))
        endByte = startByte + len(f"@{handle}".encode("utf-8"))
        facets.append({
            "index": {"byteStart": startByte, "byteEnd": endByte},
            "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
        })

    return facets
