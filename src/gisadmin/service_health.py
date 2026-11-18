"""Crawl an ArcGIS Server services directory and check each service.

For every service it:
  1. requests the service metadata and times the response
  2. for MapServer / FeatureServer, runs a returnCountOnly query on the first
     layer to prove the data source is reachable (a service can answer
     metadata requests while its database connection is broken)
  3. optionally (--admin) reads configured vs. real-time state from the
     Admin API, which catches stopped services

Example:
    gis-service-health https://gis.example.gov/arcgis --slow-ms 2000 --html report.html
    gis-service-health https://gis.example.gov/server --username admin --admin
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from . import __version__
from .arcgis import ArcGISClient, ArcGISError
from .common import Status, format_table, resolve_password, setup_logging, timestamp, worst, write_csv, write_json

log = logging.getLogger(__name__)

# Built-in folders most admins don't need in a health report
DEFAULT_SKIP_FOLDERS = {"System", "Utilities"}
QUERYABLE = {"MapServer", "FeatureServer"}


@dataclass
class ServiceCheck:
    name: str
    type: str
    url: str
    status: Status = Status.OK
    metadata_ms: float | None = None
    query_ms: float | None = None
    feature_count: int | None = None
    state: str = ""
    message: str = ""

    def flag(self, status: Status, message: str) -> None:
        self.status = max(self.status, status)
        self.message = f"{self.message}; {message}" if self.message else message

    def as_row(self) -> dict:
        row = asdict(self)
        row["status"] = self.status.name
        for key in ("metadata_ms", "query_ms"):
            row[key] = "" if row[key] is None else round(row[key])
        return row


def list_services(client: ArcGISClient, server: str, skip_folders: set[str]) -> list[dict]:
    """All services in the root and in every folder: [{"name", "type"}]."""
    root = client.get_json(f"{server}/rest/services")
    services = list(root.get("services", []))
    for folder in root.get("folders", []):
        if folder in skip_folders:
            continue
        try:
            services += client.get_json(f"{server}/rest/services/{folder}").get("services", [])
        except ArcGISError as error:
            log.warning("Could not list folder %s: %s", folder, error)
    return services


def check_service(
    client: ArcGISClient,
    server: str,
    service: dict,
    *,
    slow_ms: float,
    query: bool,
    admin: bool,
) -> ServiceCheck:
    name, kind = service["name"], service["type"]
    url = f"{server}/rest/services/{name}/{kind}"
    check = ServiceCheck(name=name, type=kind, url=url)

    if admin:
        try:
            status = client.get_json(f"{server}/admin/services/{name}.{kind}/status")
            configured, realtime = status.get("configuredState", ""), status.get("realTimeState", "")
            check.state = realtime or configured
            if realtime and realtime != "STARTED":
                check.flag(Status.CRITICAL, f"service is {realtime}")
                return check  # a stopped service will fail every other check
            if configured and realtime and configured != realtime:
                check.flag(Status.WARNING, f"configured {configured} but running {realtime}")
        except ArcGISError as error:
            check.flag(Status.WARNING, f"admin status unavailable: {error}")

    try:
        meta = client.request(url)
    except Exception as error:  # noqa: BLE001 - report any failure, keep crawling
        check.flag(Status.CRITICAL, f"metadata failed: {error}")
        return check

    check.metadata_ms = meta.elapsed_ms
    if meta.elapsed_ms > slow_ms:
        check.flag(Status.WARNING, f"slow metadata ({meta.elapsed_ms:.0f} ms)")

    layers = meta.data.get("layers") or []
    if query and kind in QUERYABLE and layers:
        layer_id = layers[0].get("id", 0)
        try:
            result = client.request(f"{url}/{layer_id}/query", {"where": "1=1", "returnCountOnly": "true"})
            check.query_ms = result.elapsed_ms
            check.feature_count = result.data.get("count")
            if result.elapsed_ms > slow_ms:
                check.flag(Status.WARNING, f"slow query ({result.elapsed_ms:.0f} ms)")
        except Exception as error:  # noqa: BLE001
            check.flag(Status.CRITICAL, f"query on layer {layer_id} failed: {error}")

    if not check.message:
        check.message = "ok"
    return check


def render_html(server: str, checks: list[ServiceCheck]) -> str:
    colors = {Status.OK: "#1b7f3b", Status.WARNING: "#b26a00", Status.CRITICAL: "#b42318", Status.ERROR: "#6b21a8"}
    counts = {s: sum(1 for c in checks if c.status is s) for s in Status}
    rows = "\n".join(
        "<tr>"
        f'<td style="color:{colors[c.status]};font-weight:600">{c.status.name}</td>'
        f'<td><a href="{html.escape(c.url)}">{html.escape(c.name)}</a></td>'
        f"<td>{html.escape(c.type)}</td>"
        f"<td>{html.escape(c.state)}</td>"
        f"<td>{'' if c.metadata_ms is None else round(c.metadata_ms)}</td>"
        f"<td>{'' if c.query_ms is None else round(c.query_ms)}</td>"
        f"<td>{'' if c.feature_count is None else c.feature_count}</td>"
        f"<td>{html.escape(c.message)}</td>"
        "</tr>"
        for c in checks
    )
    summary = " · ".join(f"{s.name}: {n}" for s, n in counts.items() if n)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Service health: {html.escape(server)}</title>
<style>
body{{font-family:Segoe UI,Roboto,Arial,sans-serif;margin:24px;color:#1f2933}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #d9e2ec;padding:6px 8px;text-align:left}}
th{{background:#f1f5f9}} td:nth-child(5),td:nth-child(6),td:nth-child(7){{text-align:right}}
</style></head><body>
<h1>ArcGIS Server service health</h1>
<p>{html.escape(server)} · {timestamp()} · {len(checks)} services · {summary}</p>
<table><thead><tr><th>Status</th><th>Service</th><th>Type</th><th>State</th>
<th>Metadata ms</th><th>Query ms</th><th>Features</th><th>Notes</th></tr></thead>
<tbody>
{rows}
</tbody></table></body></html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gis-service-health", description="Health-check ArcGIS Server services.")
    parser.add_argument("server", help="Server base URL, e.g. https://gis.example.gov/arcgis")
    parser.add_argument("--username", help="Generate a token for this user (password from GIS_PASSWORD or prompt)")
    parser.add_argument("--token-url", help="Token endpoint (default: <server>/tokens/generateToken)")
    parser.add_argument("--admin", action="store_true", help="Also read service state from the Admin API")
    parser.add_argument("--no-query", action="store_true", help="Skip the feature count query")
    parser.add_argument("--slow-ms", type=float, default=3000, help="WARNING above this response time (default 3000)")
    parser.add_argument("--include-system", action="store_true", help="Include the System and Utilities folders")
    parser.add_argument("--folder", action="append", help="Only check these folders (repeatable)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel requests (default 4; be gentle)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--ca-bundle", help="CA bundle for servers using an internal certificate authority")
    parser.add_argument("--csv")
    parser.add_argument("--json")
    parser.add_argument("--html")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


COLUMNS = ["status", "name", "type", "state", "metadata_ms", "query_ms", "feature_count", "message"]


def main(argv: list[str] | None = None, client: ArcGISClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    server = args.server.rstrip("/")
    if server.endswith("/rest/services"):
        server = server[: -len("/rest/services")]

    client = client or ArcGISClient(timeout=args.timeout, verify=args.ca_bundle or True)
    try:
        if args.username:
            password = resolve_password("GIS_PASSWORD", args.username)
            client.generate_token(args.token_url or f"{server}/tokens/generateToken", args.username, password)
        elif args.admin:
            log.error("--admin requires --username")
            return int(Status.ERROR)

        skip = set() if args.include_system else set(DEFAULT_SKIP_FOLDERS)
        services = list_services(client, server, skip)
    except Exception as error:  # noqa: BLE001
        log.error("Could not read the services directory: %s", error)
        return int(Status.ERROR)

    if args.folder:
        wanted = set(args.folder)
        services = [s for s in services if s["name"].split("/")[0] in wanted]
    if not services:
        log.warning("No services found")
        return int(Status.WARNING)

    log.info("Checking %d services on %s", len(services), server)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        checks = list(
            pool.map(
                lambda s: check_service(
                    client, server, s, slow_ms=args.slow_ms, query=not args.no_query, admin=args.admin
                ),
                services,
            )
        )

    checks.sort(key=lambda c: (-c.status, c.name))
    rows = [c.as_row() for c in checks]
    print(format_table(rows, COLUMNS))

    if args.csv:
        write_csv(args.csv, rows, COLUMNS + ["url"])
    if args.json:
        write_json(args.json, {"server": server, "generated": timestamp(), "services": rows})
    if args.html:
        from pathlib import Path

        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(render_html(server, checks), encoding="utf-8")
    for path in filter(None, (args.csv, args.json, args.html)):
        log.info("Wrote %s", path)

    overall = worst(c.status for c in checks)
    log.info("Overall: %s", overall.name)
    return int(overall)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
