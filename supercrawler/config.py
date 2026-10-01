from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional, Sequence

DEFAULT_USER_AGENT = "SuperCrawler/1.0 (+https://example.org/bot)"


@dataclass(frozen=True)
class CrawlConfig:
    seeds: Sequence[str] = ()
    allowed_domains: Optional[Sequence[str]] = None
    blocked_domains: Optional[Sequence[str]] = None
    include_patterns: Optional[Sequence[str]] = None
    exclude_patterns: Optional[Sequence[str]] = None
    max_depth: int = 3
    max_pages: int = 500
    max_bytes: int = 5 * 1024 * 1024
    concurrency: int = 8
    per_host_concurrency: int = 2
    delay: float = 0.5
    timeout: float = 15.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    max_retries: int = 2
    backoff_factor: float = 1.5
    follow_external: bool = False
    follow_nofollow: bool = False
    save_html: bool = False
    output_dir: str = "output"
    verbose: bool = True

    def merged(self, **overrides) -> "CrawlConfig":
        applied = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(applied) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise ValueError("unknown config option(s): %s" % ", ".join(sorted(unknown)))
        return replace(self, **applied)


PRESETS: Dict[str, CrawlConfig] = {
    "fast": CrawlConfig(
        max_depth=2, max_pages=100, concurrency=16, per_host_concurrency=4, delay=0.1
    ),
    "balanced": CrawlConfig(),
    "thorough": CrawlConfig(
        max_depth=6, max_pages=5000, concurrency=8, per_host_concurrency=2, delay=1.0
    ),
    "polite": CrawlConfig(
        max_depth=3, max_pages=300, concurrency=2, per_host_concurrency=1, delay=2.0
    ),
}


def get_preset(name: Optional[str]) -> CrawlConfig:
    key = (name or "balanced").lower()
    if key not in PRESETS:
        raise ValueError(
            "unknown preset %r (available: %s)" % (name, ", ".join(sorted(PRESETS)))
        )
    return PRESETS[key]