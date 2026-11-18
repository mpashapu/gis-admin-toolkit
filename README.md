# GIS Admin Toolkit

![CI](https://github.com/mpashapu/gis-admin-toolkit/actions/workflows/ci.yml/badge.svg)

Python command-line tools for the routine checks that keep an **ArcGIS
Enterprise** deployment healthy:

| Tool | What it checks |
|---|---|
| [`gis-ssl-check`](#gis-ssl-check) | TLS certificate expiry, trust and protocol for web servers, Portal and ArcGIS Server |
| [`gis-service-health`](#gis-service-health) | Every service in an ArcGIS Server directory: response time, a live data query, and (optionally) started/stopped state |
| [`gis-portal-audit`](#gis-portal-audit) | Portal or ArcGIS Online content: public sharing, stale and unused items, missing metadata, insecure URLs |

All three are **read-only**. Each one writes CSV/JSON/HTML or Markdown
reports and returns a [monitoring-friendly exit code](docs/scheduling.md), so
they can run unattended from Task Scheduler or cron.

## Install

Requires Python 3.10+.

```bash
pip install git+https://github.com/mpashapu/gis-admin-toolkit.git
```

Or from a clone: `pip install .`

## gis-ssl-check

Expired certificates are one of the most common causes of GIS outages, and
ArcGIS deployments usually have several (IIS/Web Adaptor, Portal on 7443,
Server on 6443, Data Store).

```bash
gis-ssl-check gis.example.gov portal.example.gov:7443 gis.example.gov:6443
gis-ssl-check --hosts-file examples/hosts.txt --warn-days 45 --csv reports/certs.csv
```

```
STATUS    HOST                PORT  DAYS_LEFT  NOT_AFTER   ISSUER           TLS_VERSION  MESSAGE
--------  ------------------  ----  ---------  ----------  ---------------  -----------  ------------------------------------
CRITICAL  gis.example.gov     6443  412        2028-01-02  gis.example.gov  TLSv1.2      not trusted: self-signed certificate
WARNING   portal.example.gov  7443  21         2026-12-07  Example CA       TLSv1.3      expires in 21 days
OK        gis.example.gov     443   180        2027-05-15  Example CA       TLSv1.3      valid
```
*(Example output with placeholder hosts.)*

- Results are sorted most urgent first
- **Untrusted certificates are CRITICAL** even when far from expiry
  (wrong hostname, self-signed, incomplete chain), and the tool still
  retrieves their details so you can see what's installed
- Flags servers still negotiating TLS 1.0/1.1
- Checks hosts in parallel

## gis-service-health

A service can answer metadata requests while its database connection is
broken, so this tool goes one step further: for map and feature services it
runs a `returnCountOnly` query on the first layer to prove the data is
reachable.

```bash
# Public services, no sign-in
gis-service-health https://gis.example.gov/arcgis --html reports/services.html

# Include started/stopped state from the Admin API
GIS_PASSWORD=... gis-service-health https://gis.example.gov/arcgis --username admin --admin

# Only some folders, stricter response time
gis-service-health https://gis.example.gov/arcgis --folder Public --folder Hosted --slow-ms 1500
```

For each service it reports status, state, metadata and query response times,
feature count and notes. Status rules:

- **CRITICAL**: service stopped, metadata request fails, or the data query fails
- **WARNING**: response slower than `--slow-ms`, or configured and running states differ
- `System` and `Utilities` folders are skipped unless `--include-system`

## gis-portal-audit

Content piles up in every portal. This produces a full inventory and flags
items to review:

| Flag | Meaning |
|---|---|
| `PUBLIC` | Shared with everyone. Confirm it should be |
| `NOT_PROTECTED` | Public item without delete protection |
| `STALE` | Not modified in `--stale-days` (default 365) |
| `UNUSED` | Zero views and older than `--unused-days` (default 90) |
| `NO_METADATA` | Missing summary, description or tags |
| `INSECURE_URL` | Service URL uses `http://` |

```bash
# Whole organization (signs in, finds your org ID)
gis-portal-audit https://gis.example.gov/portal --username admin -o reports/portal

# ArcGIS Online, one owner's content, no sign-in (public items only)
gis-portal-audit https://www.arcgis.com --query "owner:someone" -o reports/someone
```

Writes `reports/portal.csv` (every item with its flags) and
`reports/portal_summary.md` (counts by flag, sharing level, type and owner,
plus a "public and stale" list to review first).

## Design notes

- **Read-only by design.** No tool changes server state.
- **Passwords never go on the command line.** They come from `GIS_PASSWORD`
  or a prompt, so they stay out of shell history and process lists.
- **Tokens never go in URLs.** Authenticated requests are sent as POST so
  tokens don't end up in IIS or proxy logs.
- **ArcGIS errors are detected properly.** ArcGIS often returns failures as
  HTTP 200 with an `error` object; the shared client turns those into
  exceptions with the ArcGIS error code (for example `[498] Invalid token`).
- **Internal certificate authorities** are supported with `--ca-bundle`, rather
  than turning verification off.
- **Retries with backoff** for 502/503/504 and connection resets.
- **Polite defaults:** low concurrency for server checks.

## Project layout

```
src/gisadmin/
├── arcgis.py          Shared ArcGIS REST client (tokens, error handling, retries)
├── common.py          Status levels, exit codes, report writers
├── ssl_check.py       gis-ssl-check
├── service_health.py  gis-service-health
└── portal_audit.py    gis-portal-audit
tests/                 41 tests using fake servers, plus a real local TLS socket test
docs/scheduling.md     Windows Task Scheduler and cron setup
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## License

[MIT](LICENSE) © 2026 Malla Reddy Pashapu
