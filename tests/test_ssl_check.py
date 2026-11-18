import socket
import ssl
import threading

import pytest
from cryptography.hazmat.primitives import serialization

from gisadmin.common import Status
from gisadmin.ssl_check import CertCheck, check_target, evaluate, fetch_certificate, main, parse_target


@pytest.mark.parametrize(
    "target, expected",
    [
        ("gis.example.gov", ("gis.example.gov", 443)),
        ("portal.example.gov:7443", ("portal.example.gov", 7443)),
        ("https://gis.example.gov:6443/arcgis/admin", ("gis.example.gov", 6443)),
        ("  gis.example.gov  ", ("gis.example.gov", 443)),
    ],
)
def test_parse_target(target, expected):
    assert parse_target(target) == expected


def _evaluate(der, verify_error=None, tls="TLSv1.3"):
    return evaluate(CertCheck("t", "h", 443), der, verify_error, tls, warn_days=30, critical_days=14)


@pytest.mark.parametrize(
    "days, status",
    [
        (200, Status.OK),
        (31, Status.OK),
        (30, Status.WARNING),
        (15, Status.WARNING),
        (14, Status.CRITICAL),
        (0, Status.CRITICAL),
    ],
)
def test_thresholds(make_cert, days, status):
    der, _, _ = make_cert(days_valid=days)
    result = _evaluate(der)
    assert result.days_left == days
    assert result.status is status


def test_expired_certificate(make_cert):
    der, _, _ = make_cert(days_valid=-3)
    result = _evaluate(der, verify_error="certificate has expired")
    assert result.status is Status.CRITICAL
    assert result.message.startswith("expired")


def test_untrusted_certificate_is_critical_even_if_far_from_expiry(make_cert):
    der, _, _ = make_cert(days_valid=300)
    result = _evaluate(der, verify_error="Hostname mismatch")
    assert result.status is Status.CRITICAL
    assert "Hostname mismatch" in result.message


def test_reports_names_and_old_protocol(make_cert):
    der, _, _ = make_cert(days_valid=300, sans=("gis.example.gov", "www.gis.example.gov"))
    result = _evaluate(der, tls="TLSv1.1")
    assert result.subject == "gis.example.gov"
    assert result.issuer == "Example CA"
    assert result.sans == ["gis.example.gov", "www.gis.example.gov"]
    assert result.status is Status.WARNING and "outdated protocol" in result.message


def test_connection_failure_is_error():
    def refuse(host, port, timeout):
        raise ConnectionRefusedError("refused")

    result = check_target("down.example.gov", fetcher=refuse)
    assert result.status is Status.ERROR
    assert "connection failed" in result.message


def test_main_exit_code_is_worst_status(make_cert, tmp_path, capsys):
    certs = {"ok.example.gov": make_cert(days_valid=200)[0], "soon.example.gov": make_cert(days_valid=20)[0]}
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("# comment\nok.example.gov\n\nsoon.example.gov  # expiring\n")
    out_csv = tmp_path / "certs.csv"

    code = main(
        ["--hosts-file", str(hosts), "--csv", str(out_csv)], fetcher=lambda h, p, t: (certs[h], None, "TLSv1.3")
    )
    assert code == Status.WARNING
    table = capsys.readouterr().out
    assert table.index("soon.example.gov") < table.index("ok.example.gov")  # most urgent first
    assert "WARNING" in out_csv.read_text()


def test_main_requires_targets():
    assert main([]) == Status.ERROR


def test_fetch_certificate_against_real_local_tls_server(make_cert, tmp_path):
    """End-to-end over a real socket: an untrusted cert fails verification,
    and the details are still retrieved for the report."""
    der, cert, key = make_cert(days_valid=45, common_name="localhost", sans=("localhost",))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    def serve():
        for _ in range(2):  # verified attempt, then inspection attempt
            conn, _ = listener.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        fetched, verify_error, tls_version = fetch_certificate("localhost", port, timeout=5)
    finally:
        thread.join(timeout=5)
        listener.close()

    assert fetched == der
    # Untrusted issuer: OpenSSL reports e.g. "unable to get local issuer certificate"
    assert verify_error
    assert tls_version and tls_version.startswith("TLS")
