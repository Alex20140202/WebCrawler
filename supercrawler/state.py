from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, Iterable, List, Optional, Set, Tuple

STATE_VERSION = 2


class CrawlState:
    """Durable crawl progress so a long crawl can be resumed."""

    def __init__(
        self,
        seen: Optional[Iterable[str]] = None,
        pending: Optional[Iterable[Tuple[str, int]]] = None,
        pages_crawled: int = 0,
        next_depth: int = 0,
        signature: str = "",
        covered: Optional[Iterable[str]] = None,
    ):
        self.seen: Set[str] = set(seen or ())
        self.pending: List[Tuple[str, int]] = [(u, int(d)) for u, d in (pending or ())]
        self.pages_crawled = int(pages_crawled)
        self.next_depth = int(next_depth)
        self.signature = signature
        # Sitemap URLs already fetched across all runs, so coverage stays
        # cumulative when a crawl is resumed instead of restarting at zero.
        self.covered: Set[str] = set(covered or ())

    def add_seen(self, url: str) -> None:
        self.seen.add(url)

    def is_seen(self, url: str) -> bool:
        return url in self.seen

    def push(self, url: str, depth: int) -> None:
        if url in self.seen:
            return
        self.seen.add(url)
        self.pending.append((url, depth))

    def pop_batch(self, size: int, max_depth: int) -> List[Tuple[str, int]]:
        """Take up to `size` URLs whose depth is within budget.

        A negative `max_depth` means unlimited depth.
        """
        batch: List[Tuple[str, int]] = []
        unlimited = max_depth < 0
        while self.pending and len(batch) < size:
            url, depth = self.pending.pop(0)
            if not unlimited and depth > max_depth:
                continue
            batch.append((url, depth))
        return batch

    def as_dict(self) -> Dict:
        return {
            "version": STATE_VERSION,
            "signature": self.signature,
            "pages_crawled": self.pages_crawled,
            "next_depth": self.next_depth,
            "seen": sorted(self.seen),
            "covered": sorted(self.covered),
            "pending": [list(item) for item in self.pending],
        }


def save_state(path: str, state: CrawlState) -> Optional[str]:
    if not path:
        return None
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory or ".", delete=False, suffix=".tmp"
    )
    try:
        json.dump(state.as_dict(), handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
        return path
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def load_state(path: str) -> Optional[CrawlState]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None
    pending = [(str(u), int(d)) for u, d in payload.get("pending", [])]
    return CrawlState(
        seen=payload.get("seen", []),
        pending=pending,
        pages_crawled=payload.get("pages_crawled", 0),
        next_depth=payload.get("next_depth", 0),
        signature=payload.get("signature", ""),
        covered=payload.get("covered", []),
    )