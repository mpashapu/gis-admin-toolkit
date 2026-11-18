"""Audit content in ArcGIS Enterprise Portal or ArcGIS Online. Read-only.

Lists every item the account can see and flags common housekeeping and
security issues:

  PUBLIC          shared with everyone (confirm it should be)
  STALE           not modified in --stale-days
  UNUSED          zero views and older than --unused-days
  NO_METADATA     missing summary, description or tags
  INSECURE_URL    service URL uses http://
  NOT_PROTECTED   public item without delete protection

Example:
    gis-portal-audit https://gis.example.gov/portal --username admin
    gis-portal-audit https://www.arcgis.com --query 'owner:jsmith' --csv items.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .arcgis import ArcGISClient, ArcGISError
from .common import Status, resolve_password, setup_logging, timestamp, utc_now, write_csv, write_json

log = logging.getLogger(__name__)

PAGE_SIZE = 100  # maximum the search API returns per page
SEARCH_LIMIT = 10_000  # the search API stops paging after 10,000 results


@dataclass
class AuditedItem:
    id: str
    title: str
    type: str
    owner: str
    access: str
    created: str
    modified: str
    days_since_modified: int
    views: int
    size_mb: float
    url: str
    flags: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {**self.__dict__, "flags": " ".join(self.flags)}


def _date(epoch_ms: int | None) -> datetime | None:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


def search_items(client: ArcGISClient, portal: str, query: str) -> Iterator[dict]:
    """Page through /sharing/rest/search until nextStart is -1."""
    start = 1
    while True:
        page = client.get_json(
            f"{portal}/sharing/rest/search",
            {"q": query, "start": start, "num": PAGE_SIZE, "sortField": "modified", "sortOrder": "asc"},
        )
        if start == 1 and page.get("total", 0) > SEARCH_LIMIT:
            log.warning(
                "Query matches %d items but search returns at most %d. Narrow --query (e.g. by owner or type).",
                page["total"],
                SEARCH_LIMIT,
            )
        yield from page.get("results", [])

        next_start = page.get("nextStart", -1)
        if next_start == -1 or next_start > SEARCH_LIMIT:
            return
        if next_start <= start:  # guard against a server that doesn't advance
            log.warning("Search paging stopped advancing at start=%d", start)
            return
        start = next_start


def audit_item(raw: dict, *, stale_days: int, unused_days: int, now: datetime | None = None) -> AuditedItem:
    now = now or utc_now()
    created = _date(raw.get("created"))
    modified = _date(raw.get("modified")) or created
    days_modified = (now - modified).days if modified else 0
    days_created = (now - created).days if created else 0
    url = raw.get("url") or ""

    item = AuditedItem(
        id=raw.get("id", ""),
        title=raw.get("title", ""),
        type=raw.get("type", ""),
        owner=raw.get("owner", ""),
        access=raw.get("access", ""),
        created=created.strftime("%Y-%m-%d") if created else "",
        modified=modified.strftime("%Y-%m-%d") if modified else "",
        days_since_modified=days_modified,
        views=int(raw.get("numViews") or 0),
        size_mb=round((raw.get("size") or 0) / 1_048_576, 2),
        url=url,
    )

    if item.access == "public":
        item.flags.append("PUBLIC")
        if not raw.get("protected"):
            item.flags.append("NOT_PROTECTED")
    if days_modified >= stale_days:
        item.flags.append("STALE")
    if item.views == 0 and days_created >= unused_days:
        item.flags.append("UNUSED")
    if not (raw.get("snippet") and raw.get("description") and raw.get("tags")):
        item.flags.append("NO_METADATA")
    if url.lower().startswith("http://"):
        item.flags.append("INSECURE_URL")
    return item


def summarize(items: list[AuditedItem], portal: str, query: str) -> str:
    flag_counts = Counter(flag for item in items for flag in item.flags)
    by_type = Counter(item.type for item in items).most_common(10)
    by_owner = Counter(item.owner for item in items).most_common(10)
    by_access = Counter(item.access for item in items)
    total_mb = sum(item.size_mb for item in items)

    lines = [
        f"# Portal content audit: {portal}",
        "",
        f"{timestamp()} · query `{query}` · {len(items)} items · {total_mb:,.1f} MB",
        "",
        "## Flags",
        "",
        "| Flag | Items |",
        "|---|---:|",
        *[f"| {flag} | {count} |" for flag, count in sorted(flag_counts.items())],
        "",
        "## Sharing",
        "",
        "| Access | Items |",
        "|---|---:|",
        *[f"| {access or '(none)'} | {count} |" for access, count in sorted(by_access.items())],
        "",
        "## Top item types",
        "",
        "| Type | Items |",
        "|---|---:|",
        *[f"| {kind} | {count} |" for kind, count in by_type],
        "",
        "## Top owners",
        "",
        "| Owner | Items |",
        "|---|---:|",
        *[f"| {owner} | {count} |" for owner, count in by_owner],
        "",
    ]

    public_stale = [i for i in items if "PUBLIC" in i.flags and "STALE" in i.flags]
    if public_stale:
        lines += [
            f"## Public and stale ({len(public_stale)}): review first",
            "",
            "| Title | Type | Owner | Last modified |",
            "|---|---|---|---|",
            *[f"| {i.title} | {i.type} | {i.owner} | {i.modified} |" for i in public_stale[:25]],
            "",
        ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gis-portal-audit", description="Read-only Portal / ArcGIS Online audit.")
    parser.add_argument("portal", help="Portal URL, e.g. https://gis.example.gov/portal or https://www.arcgis.com")
    parser.add_argument("--username", help="Sign in as this user (password from GIS_PASSWORD or prompt)")
    parser.add_argument("--query", help="Search query (default: every item in your organization)")
    parser.add_argument("--stale-days", type=int, default=365, help="STALE if not modified for this long (365)")
    parser.add_argument("--unused-days", type=int, default=90, help="UNUSED if 0 views and older than this (90)")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--ca-bundle", help="CA bundle for portals using an internal certificate authority")
    parser.add_argument("-o", "--output", default="portal_audit", help="Output prefix (default: portal_audit)")
    parser.add_argument("--json", action="store_true", help="Also write raw results as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


COLUMNS = [
    "id",
    "title",
    "type",
    "owner",
    "access",
    "created",
    "modified",
    "days_since_modified",
    "views",
    "size_mb",
    "flags",
    "url",
]


def main(argv: list[str] | None = None, client: ArcGISClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    portal = args.portal.rstrip("/")
    if portal.endswith("/sharing/rest"):
        portal = portal[: -len("/sharing/rest")]

    client = client or ArcGISClient(timeout=args.timeout, verify=args.ca_bundle or True)
    try:
        if args.username:
            password = resolve_password("GIS_PASSWORD", args.username)
            client.generate_token(f"{portal}/sharing/rest/generateToken", args.username, password)

        query = args.query
        if not query:
            if not client.token:
                log.error("Anonymous audits need --query (for example 'owner:someone')")
                return int(Status.ERROR)
            org_id = client.get_json(f"{portal}/sharing/rest/portals/self").get("id")
            query = f"orgid:{org_id}"

        log.info("Searching %s for: %s", portal, query)
        raw_items = list(search_items(client, portal, query))
    except ArcGISError as error:
        log.error("%s", error)
        return int(Status.ERROR)

    items = [audit_item(r, stale_days=args.stale_days, unused_days=args.unused_days) for r in raw_items]
    rows = [i.as_row() for i in items]

    prefix = Path(args.output)
    write_csv(prefix.with_suffix(".csv"), rows, COLUMNS)
    report = prefix.with_name(f"{prefix.name}_summary.md")
    report.write_text(summarize(items, portal, query), encoding="utf-8")
    if args.json:
        write_json(prefix.with_suffix(".json"), raw_items)
    log.info("Audited %d items. Wrote %s and %s", len(items), prefix.with_suffix(".csv"), report)

    flagged = Counter(f for i in items for f in i.flags)
    if flagged:
        log.info("Flags: %s", ", ".join(f"{k}={v}" for k, v in sorted(flagged.items())))
    # Non-zero exit when something needs a human: public content nobody maintains
    public_and_stale = any("PUBLIC" in i.flags and "STALE" in i.flags for i in items)
    return int(Status.WARNING if public_and_stale else Status.OK)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
