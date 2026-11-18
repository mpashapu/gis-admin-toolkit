"""Check TLS certificate expiry and validity for a list of hosts.

Example:
    gis-ssl-check gis.example.gov portal.example.gov:7443 --warn-days 30
    gis-ssl-check --hosts-file examples/hosts.txt --csv reports/certs.csv

Exit code is the worst status found (0 OK, 1 WARNING, 2 CRITICAL, 3 ERROR),
so it can drive Task Scheduler / cron alerts directly.
"""

from __future__ import annotations

import argparse
import logging
import socket
import ssl
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

from . import __version__
from .common import Status, format_table, read_lines, setup_logging, utc_now, worst, write_csv, write_json

log = logging.getLogger(__name__)

# (der_bytes, verify_error or None, tls_version or None)
FetchResult = tuple[bytes, str | None, str | None]
Fetcher = Callable[[str, int, float], FetchResult]


@dataclass
class CertCheck:
    target: str
    host: str
    port: int
    status: Status = Status.ERROR
    days_left: int | None = None
    not_after: str = ""
    subject: str = ""
    issuer: str = ""
    sans: list[str] = field(default_factory=list)
    tls_version: str = ""
    message: str = ""

    def as_row(self) -> dict:
        row = asdict(self)
        row["status"] = self.status.name
        row["sans"] = " ".join(self.sans)
        row["days_left"] = "" if self.days_left is None else self.days_left
        return row


def parse_target(target: str, default_port: int = 443) -> tuple[str, int]:
    """Accept host, host:port, or a full https:// URL."""
    text = target.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if not parts.hostname:
        raise ValueError(f"Can't parse host from '{target}'")
    return parts.hostname, parts.port or default_port


def fetch_certificate(host: str, port: int, timeout: float) -> FetchResult:
    """Return the server's leaf certificate, verifying it if possible.

    If verification fails (expired, wrong name, untrusted chain), connect again
    without verification so the certificate details can still be reported.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                return tls.getpeercert(binary_form=True), None, tls.version()
    except ssl.SSLCertVerificationError as error:
        verify_error = error.verify_message or str(error)

    # Inspection only: this connection sends nothing and trusts nothing.
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with insecure.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True), verify_error, tls.version()


def _name(name: x509.Name) -> str:
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        values = name.get_attributes_for_oid(oid)
        if values:
            return str(values[0].value)
    return name.rfc4514_string()


def _sans(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return []
    return ext.value.get_values_for_type(x509.DNSName)


def evaluate(
    check: CertCheck,
    der: bytes,
    verify_error: str | None,
    tls_version: str | None,
    *,
    warn_days: int,
    critical_days: int,
    now: datetime | None = None,
) -> CertCheck:
    cert = x509.load_der_x509_certificate(der)
    not_after = cert.not_valid_after_utc
    now = now or utc_now()
    days_left = (not_after - now).days

    check.not_after = not_after.strftime("%Y-%m-%d")
    check.days_left = days_left
    check.subject = _name(cert.subject)
    check.issuer = _name(cert.issuer)
    check.sans = _sans(cert)
    check.tls_version = tls_version or ""

    if days_left < 0:
        check.status, check.message = Status.CRITICAL, f"expired {-days_left} days ago"
    elif verify_error:
        check.status, check.message = Status.CRITICAL, f"not trusted: {verify_error}"
    elif days_left <= critical_days:
        check.status, check.message = Status.CRITICAL, f"expires in {days_left} days"
    elif days_left <= warn_days:
        check.status, check.message = Status.WARNING, f"expires in {days_left} days"
    else:
        check.status, check.message = Status.OK, "valid"

    if tls_version in {"TLSv1", "TLSv1.1"}:
        check.status = max(check.status, Status.WARNING)
        check.message += f"; outdated protocol {tls_version}"
    return check


def check_target(
    target: str,
    *,
    warn_days: int = 30,
    critical_days: int = 14,
    timeout: float = 10,
    fetcher: Fetcher = fetch_certificate,
) -> CertCheck:
    try:
        host, port = parse_target(target)
    except ValueError as error:
        return CertCheck(target=target, host="", port=0, message=str(error))

    check = CertCheck(target=target, host=host, port=port)
    try:
        der, verify_error, tls_version = fetcher(host, port, timeout)
    except (OSError, ssl.SSLError) as error:
        check.message = f"connection failed: {error}"
        return check
    return evaluate(check, der, verify_error, tls_version, warn_days=warn_days, critical_days=critical_days)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gis-ssl-check", description="Check TLS certificate expiry and trust.")
    parser.add_argument("targets", nargs="*", help="host, host:port or https:// URL")
    parser.add_argument("--hosts-file", help="Text file with one target per line (# for comments)")
    parser.add_argument("--warn-days", type=int, default=30, help="WARNING at or below this many days (default 30)")
    parser.add_argument("--critical-days", type=int, default=14, help="CRITICAL at or below (default 14)")
    parser.add_argument("--timeout", type=float, default=10, help="Connection timeout in seconds (default 10)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel checks (default 8)")
    parser.add_argument("--csv", help="Write results to this CSV file")
    parser.add_argument("--json", help="Write results to this JSON file")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


COLUMNS = ["status", "host", "port", "days_left", "not_after", "issuer", "tls_version", "message"]


def main(argv: list[str] | None = None, fetcher: Fetcher = fetch_certificate) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    targets = list(args.targets)
    if args.hosts_file:
        targets += read_lines(args.hosts_file)
    if not targets:
        log.error("No targets. Pass hosts as arguments or use --hosts-file.")
        return int(Status.ERROR)
    if args.critical_days > args.warn_days:
        log.error("--critical-days must not exceed --warn-days")
        return int(Status.ERROR)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        checks = list(
            pool.map(
                lambda t: check_target(
                    t,
                    warn_days=args.warn_days,
                    critical_days=args.critical_days,
                    timeout=args.timeout,
                    fetcher=fetcher,
                ),
                targets,
            )
        )

    # Most urgent first
    checks.sort(key=lambda c: (-c.status, c.days_left if c.days_left is not None else -1))
    rows = [c.as_row() for c in checks]
    print(format_table(rows, COLUMNS))

    if args.csv:
        write_csv(args.csv, rows, COLUMNS + ["subject", "sans", "target"])
        log.info("Wrote %s", args.csv)
    if args.json:
        write_json(args.json, {"generated": datetime.now(timezone.utc).isoformat(), "results": rows})
        log.info("Wrote %s", args.json)

    overall = worst(c.status for c in checks)
    log.info("Overall: %s (%d checked)", overall.name, len(checks))
    return int(overall)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
