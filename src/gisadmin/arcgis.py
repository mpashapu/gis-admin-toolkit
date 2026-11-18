"""Minimal read-only ArcGIS REST client shared by the service and portal tools."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class ArcGISError(RuntimeError):
    """An error returned by ArcGIS, which often arrives with HTTP 200."""

    def __init__(self, message: str, code: int | None = None, details: list | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or []

    def __str__(self) -> str:
        prefix = f"[{self.code}] " if self.code else ""
        extra = f" ({'; '.join(map(str, self.details))})" if self.details else ""
        return f"{prefix}{super().__str__()}{extra}"


@dataclass
class Response:
    data: dict
    elapsed_ms: float


def make_session(retries: int = 2) -> requests.Session:
    """Session that retries idempotent requests on connection errors and 502/503/504."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),  # every request this toolkit makes is read-only
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "gis-admin-toolkit"
    return session


class ArcGISClient:
    """Sends requests to ArcGIS Server / Portal REST endpoints.

    With a token, requests are sent as POST so the token stays out of URLs
    and therefore out of web server and proxy logs.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30,
        verify: bool | str = True,
        referer: str = "https://gis-admin-toolkit",
    ) -> None:
        self.token = token
        self.session = session or make_session()
        self.timeout = timeout
        self.verify = verify  # True, or a path to an internal CA bundle
        self.referer = referer

    def request(self, url: str, params: dict | None = None) -> Response:
        payload = {"f": "json", **(params or {})}
        headers = {"Referer": self.referer}
        started = time.perf_counter()
        if self.token:
            payload["token"] = self.token
            http = self.session.post(url, data=payload, headers=headers, timeout=self.timeout, verify=self.verify)
        else:
            http = self.session.get(url, params=payload, headers=headers, timeout=self.timeout, verify=self.verify)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if http.status_code >= 400:
            raise ArcGISError(f"HTTP {http.status_code} from {url}", code=http.status_code)
        try:
            data = http.json()
        except ValueError as error:
            raise ArcGISError(f"Response from {url} was not JSON (login page or proxy error?)") from error

        # ArcGIS reports most failures as HTTP 200 with an "error" object
        if isinstance(data, dict) and "error" in data:
            err = data["error"] or {}
            raise ArcGISError(err.get("message", "Unknown ArcGIS error"), err.get("code"), err.get("details"))
        return Response(data=data, elapsed_ms=elapsed_ms)

    def get_json(self, url: str, params: dict | None = None) -> dict:
        return self.request(url, params).data

    def generate_token(self, token_url: str, username: str, password: str, expiration_minutes: int = 60) -> str:
        """Get a token from ArcGIS Server (/arcgis/tokens/generateToken) or
        Portal/ArcGIS Online (/sharing/rest/generateToken)."""
        http = self.session.post(
            token_url,
            data={
                "username": username,
                "password": password,
                "client": "referer",
                "referer": self.referer,
                "expiration": expiration_minutes,
                "f": "json",
            },
            timeout=self.timeout,
            verify=self.verify,
        )
        if http.status_code >= 400:
            raise ArcGISError(f"HTTP {http.status_code} from token endpoint", code=http.status_code)
        data = http.json()
        if "error" in data:
            err = data["error"] or {}
            raise ArcGISError(err.get("message", "Token request failed"), err.get("code"), err.get("details"))
        token = data.get("token")
        if not token:
            raise ArcGISError("Token endpoint did not return a token")
        self.token = token
        log.debug("Token acquired, expires %s", data.get("expires"))
        return token
