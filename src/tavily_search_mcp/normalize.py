"""Input normalisation, slug, FNV-1a 64-bit hash, and batch-cache key encoding.

Per SPECS §6: search-query cache keys are the trimmed, inner-whitespace-
collapsed, lowercased query. URL cache keys are the canonical URL
(scheme + host lowercased, fragment stripped). Batch cache keys are a
canonical-JSON encoding of the sorted list of normalised URLs, so a single-URL
call and a one-URL batch collide when the inputs match. File names use
`<slug>-<fnv1a64>.json` so a human can browse the cache directory.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_INNER_WS = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def normalize_query(q: str) -> str:
    if not isinstance(q, str):
        q = str(q)
    return _INNER_WS.sub(" ", q).strip().lower()


def normalize_url(url: str) -> str:
    """Canonicalise a URL for cache-key purposes.

    Scheme and host are lowercased; the path/query are kept verbatim;
    the fragment is stripped. The cache key is the *input* URL, not the
    final post-redirect URL.
    """
    if not isinstance(url, str):
        url = str(url)
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url.strip()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def slugify(normalized: str, max_len: int = 80) -> str:
    slug = _NON_SLUG.sub("-", normalized).strip("-")
    if not slug:
        slug = "empty"
    return slug[:max_len]


def fnv1a64(s: str) -> int:
    h = _FNV_OFFSET
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


def canonical_batch_key(urls: list[str]) -> str:
    """Stable cache key for a batch of URLs: JSON of the sorted, deduped, normalised list.

    Single-URL `[u]` and `urls=[u, u]` collapse to the same key.
    """
    if not isinstance(urls, list):
        urls = list(urls) if urls is not None else []
    normed = sorted({normalize_url(u) for u in urls if isinstance(u, str) and u.strip()})
    return json.dumps(normed, ensure_ascii=False, sort_keys=True)


def cache_path_for(input_key: str, base_dir: Path) -> Path:
    """Map an arbitrary cache input to a stable on-disk path.

    URLs are canonicalised (scheme + host lowercased, fragment stripped);
    everything else is treated as a query and trimmed + lowercased. Same
    input through any of the surface shapes lands on the same path.
    """
    if input_key.startswith(("http://", "https://")):
        normalized = normalize_url(input_key)
    else:
        normalized = normalize_query(input_key)
    return base_dir / f"{slugify(normalized)}-{fnv1a64(normalized):016x}.json"
