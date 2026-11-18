"""Fakes so the suite runs without a real ArcGIS server or network."""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class FakeHTTPResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Routes requests by URL. A route value can be a dict (JSON body), a
    FakeHTTPResponse, or a callable(params) returning either."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []
        self.headers: dict = {}

    def _handle(self, method, url, params):
        self.calls.append({"method": method, "url": url, "params": dict(params or {})})
        if url not in self.routes:
            return FakeHTTPResponse(status_code=404)
        route = self.routes[url]
        result = route(params or {}) if callable(route) else route
        return result if isinstance(result, FakeHTTPResponse) else FakeHTTPResponse(result)

    def get(self, url, params=None, **_):
        return self._handle("GET", url, params)

    def post(self, url, data=None, **_):
        return self._handle("POST", url, data)


@pytest.fixture
def make_cert():
    """Build a DER certificate with chosen validity and names."""

    def _make(
        days_valid: int = 90,
        common_name: str = "gis.example.gov",
        sans=("gis.example.gov",),
        issuer_cn: str = "Example CA",
        key=None,
    ):
        key = key or ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        not_after = now + dt.timedelta(days=days_valid, hours=12)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(min(now - dt.timedelta(days=1), not_after - dt.timedelta(days=1)))
            .not_valid_after(not_after)
        )
        if sans:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), False)
        cert = builder.sign(key, hashes.SHA256())
        return cert.public_bytes(serialization.Encoding.DER), cert, key

    return _make
