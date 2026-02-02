from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Tuple

_GLOBAL_VERSION = 0
_TAG_VERSIONS = defaultdict(int)


def bump_cache_version(*tags: str) -> None:
    global _GLOBAL_VERSION
    _GLOBAL_VERSION += 1
    for tag in _normalize_tags(tags):
        _TAG_VERSIONS[tag] += 1


def cache_version(*tags: str):
    if not tags:
        return _GLOBAL_VERSION
    normalized = _normalize_tags(tags)
    return tuple(_TAG_VERSIONS[tag] for tag in normalized)


def _normalize_tags(tags: Iterable[str]) -> Tuple[str, ...]:
    return tuple(tag.strip().lower() for tag in tags if tag and tag.strip())
