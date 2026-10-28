# Excerpt from app/crawler/scheduling.py ("# ..." marks omitted code)

"""Recrawl scheduling: intervals, crawl lanes and site tiers.

Pure (no I/O) and free of Scrapy, so the API and the seed CLI can use it without
loading the crawler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config.settings import CrawlSettings, SiteSettings
from app.db.models import CrawlLane, EmbedPriority, RecrawlOutcome, SiteTier, UrlKind

# Longest recrawl interval of each lane; anything longer is SLOW.
LANE_MAX_INTERVALS: tuple[tuple[CrawlLane, timedelta], ...] = (
    (CrawlLane.REALTIME, timedelta(minutes=5)),
    (CrawlLane.FAST, timedelta(minutes=15)),
    (CrawlLane.HOURLY, timedelta(hours=1)),
    (CrawlLane.DAILY, timedelta(hours=24)),
)
NO_FLOOR = timedelta(0)


def lane_for_interval(interval: timedelta) -> CrawlLane:
    for lane, longest in LANE_MAX_INTERVALS:
        if interval <= longest:
            return lane
    return CrawlLane.SLOW

# ...

def recrawl_bounds(
    kind: UrlKind, crawl: CrawlSettings, floor: timedelta = NO_FLOOR
) -> tuple[timedelta, timedelta]:
    """Shortest and longest interval: ``max(kind minimum, floor)`` up to the kind maximum.

    A floor above the kind maximum is capped at the maximum.
    """
    minutes, hours = {
        UrlKind.PAGE: (crawl.page_min_recrawl_minutes, crawl.page_max_recrawl_hours),
        UrlKind.SITEMAP: (crawl.sitemap_min_recrawl_minutes, crawl.sitemap_max_recrawl_hours),
        UrlKind.FEED: (crawl.feed_min_recrawl_minutes, crawl.feed_max_recrawl_hours),
    }[kind]
    high = timedelta(hours=hours)
    return min(max(timedelta(minutes=minutes), floor), high), high

# ...

def discovery_lane(own: CrawlLane, discoverer: CrawlLane | None) -> CrawlLane:
    """Lane of a URL awaiting a discovery-triggered fetch.

    ``own`` is the kind's initial lane for a URL never fetched successfully, or its
    current lane when it is already queued or made due again by a newer sitemap/feed
    date. ``discoverer`` is the lane of the URL that found it, after that URL's current
    fetch. The more urgent of the two wins. ``repository.enqueue_urls`` applies the same
    rule in SQL to rows that already exist.
    """
    return own if discoverer is None else min(own, discoverer)

# ...

@dataclass(frozen=True, slots=True)
class SiteActivity:
    """A site's measured activity over the stats window."""

    window_days: float
    # Pages published in the window: first seen in it, with their own date (feed
    # published, sitemap lastmod) in it too. Not merely discovered: a first crawl of a
    # large sitemap of old pages publishes nothing.
    published: int
    refetches: int  # successful refetches compared with the previous fetch
    changed: int  # of which the content had changed

    @property
    def published_per_day(self) -> float:
        return self.published / self.window_days

    @property
    def changed_share(self) -> float | None:
        return self.changed / self.refetches if self.refetches else None


def tier_for_stats(stats: SiteActivity, sites: SiteSettings) -> SiteTier:
    """Tier from activity: pages published a day decide, a high change share can lift a
    site to active or normal (never hot, so noisy pages alone can't make a site hot)."""
    per_day = stats.published_per_day
    share = (
        stats.changed_share if stats.refetches >= sites.min_refetches_for_changed_share else None
    )
    if per_day >= sites.hot_min_new_urls_per_day:
        return SiteTier.HOT
    if per_day >= sites.active_min_new_urls_per_day or (
        share is not None and share >= sites.active_min_changed_share
    ):
        return SiteTier.ACTIVE
    if per_day >= sites.normal_min_new_urls_per_day or (
        share is not None and share >= sites.normal_min_changed_share
    ):
        return SiteTier.NORMAL
    return SiteTier.STATIC

# ...

# --- adaptive recrawl -----------------------------------------------------------------------


def recrawl_outcome(
    kind: UrlKind,
    *,
    previous_interval_seconds: int | None,
    previous_hash: str | None,
    current_hash: str | None,
    new_urls: int,
) -> RecrawlOutcome:
    """Classify a successful fetch against the previous one.

    Content is compared by hash only when both fetches produced one. A sitemap or feed
    also counts as changed when it led to URLs the frontier did not know yet.
    """
    if previous_interval_seconds is None:
        return RecrawlOutcome.NEW
    if previous_hash is not None and current_hash is not None and previous_hash != current_hash:
        return RecrawlOutcome.CHANGED
    if kind != UrlKind.PAGE and new_urls > 0:
        return RecrawlOutcome.CHANGED
    return RecrawlOutcome.UNCHANGED


def next_recrawl_interval(
    kind: UrlKind,
    previous_seconds: float | None,
    outcome: RecrawlOutcome,
    crawl: CrawlSettings,
    floor: timedelta = NO_FLOOR,
    initial: timedelta | None = None,
) -> timedelta:
    """Interval until the next fetch, in whole seconds, within the kind's bounds raised
    to the site tier's ``floor``.

    Without a previous interval (first fetch), ``initial`` is used (default: the kind's
    initial interval; see ``tier_initial_interval``).
    """
    if outcome == RecrawlOutcome.NEW or previous_seconds is None:
        seconds = (initial or initial_recrawl_interval(kind, crawl)).total_seconds()
    elif outcome == RecrawlOutcome.CHANGED:
        seconds = previous_seconds * crawl.recrawl_shrink_factor
    else:
        seconds = previous_seconds * crawl.recrawl_growth_factor
    low, high = recrawl_bounds(kind, crawl, floor)
    return timedelta(seconds=round(min(max(seconds, low.total_seconds()), high.total_seconds())))
