from __future__ import annotations

import os
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

    use_sitemap: bool = True
    max_sitemaps: int = 8
    max_sitemap_entries: int = 100000
    dedupe_content: bool = True
    strip_params: bool = True
    state_file: str = ""
    stop_on_error_ratio: float = 1.0
    min_delay_on_errors: float = 5.0
    auto_tune: bool = True

    auto_actions: bool = True
    interaction_mode: str = "auto"
    max_questions: int = 10
    max_actions_per_page: int = 25
    decisions_file: str = ""

    login_url: str = ""
    login_username: str = ""
    login_password: str = ""
    login_password_env: str = "SUPERCRAWLER_PASSWORD"
    auth_header: str = ""
    session_file: str = ""
    require_login: bool = False

    def merged(self, **overrides) -> "CrawlConfig":
        applied = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(applied) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise ValueError("unknown config option(s): %s" % ", ".join(sorted(unknown)))
        return replace(self, **applied)

    @property
    def unlimited(self) -> bool:
        """True when the page budget is uncapped."""
        return self.max_pages <= 0

    def validate(self) -> None:
        if self.interaction_mode not in ("ask", "auto", "never"):
            raise ValueError(
                "interaction_mode must be ask, auto or never, got %r"
                % self.interaction_mode
            )
        if self.max_questions < 0:
            raise ValueError("max_questions cannot be negative")
        if self.max_actions_per_page < 0:
            raise ValueError("max_actions_per_page cannot be negative")
        if self.login_url and self.login_password and self.login_password_env:
            if self.login_password_env and os.environ.get(self.login_password_env):
                raise ValueError(
                    "set the password either inline or via %s, not both"
                    % self.login_password_env
                )
        if self.auth_header and ":" not in self.auth_header \
                and "=" not in self.auth_header:
            raise ValueError("auth_header must look like 'Name: value' or 'Name=value'")

    @property
    def has_login(self) -> bool:
        return bool(self.login_url or self.auth_header or self.session_file)

    @property
    def unlimited_depth(self) -> bool:
        """True when the link-depth budget is uncapped.

        Depth 0 means "seed pages only", so it is a real limit, not unlimited.
        """
        return self.max_depth < 0


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
    "wholesite": CrawlConfig(
        max_depth=-1, max_pages=0, concurrency=8, per_host_concurrency=2, delay=0.8,
        use_sitemap=True, dedupe_content=True,
    ),
    "archive": CrawlConfig(
        max_depth=-1, max_pages=0, concurrency=10, per_host_concurrency=3, delay=0.5,
        use_sitemap=True, dedupe_content=True, save_html=True,
    ),
}


def get_preset(name: Optional[str]) -> CrawlConfig:
    key = (name or "balanced").lower()
    if key not in PRESETS:
        raise ValueError(
            "unknown preset %r (available: %s)" % (name, ", ".join(sorted(PRESETS)))
        )
    return PRESETS[key]