import pytest
from conftest import FakeHTTPResponse, FakeSession

from gisadmin.arcgis import ArcGISClient, ArcGISError

URL = "https://gis.example.gov/arcgis/rest/services"


def test_get_without_token_adds_f_json():
    session = FakeSession({URL: {"services": []}})
    ArcGISClient(session=session).get_json(URL)
    assert session.calls[0] == {"method": "GET", "url": URL, "params": {"f": "json"}}


def test_token_is_sent_in_post_body_not_url():
    session = FakeSession({URL: {"services": []}})
    ArcGISClient(session=session, token="secret").get_json(URL)
    call = session.calls[0]
    assert call["method"] == "POST" and call["url"] == URL
    assert call["params"]["token"] == "secret"


def test_error_object_with_http_200_raises():
    session = FakeSession({URL: {"error": {"code": 498, "message": "Invalid token.", "details": ["expired"]}}})
    with pytest.raises(ArcGISError) as info:
        ArcGISClient(session=session).get_json(URL)
    assert info.value.code == 498
    assert str(info.value) == "[498] Invalid token. (expired)"


def test_non_json_response_raises_helpful_error():
    session = FakeSession({URL: FakeHTTPResponse(payload=None, text="<html>Sign in</html>")})
    with pytest.raises(ArcGISError, match="not JSON"):
        ArcGISClient(session=session).get_json(URL)


def test_http_error_raises():
    session = FakeSession({URL: FakeHTTPResponse(status_code=503)})
    with pytest.raises(ArcGISError, match="HTTP 503"):
        ArcGISClient(session=session).get_json(URL)


def test_generate_token_success_and_failure():
    token_url = "https://gis.example.gov/arcgis/tokens/generateToken"
    session = FakeSession({token_url: {"token": "abc", "expires": 1}})
    client = ArcGISClient(session=session)
    assert client.generate_token(token_url, "admin", "pw") == "abc"
    assert client.token == "abc"
    sent = session.calls[0]["params"]
    assert sent["username"] == "admin" and sent["client"] == "referer" and sent["f"] == "json"

    bad = FakeSession({token_url: {"error": {"code": 400, "message": "Invalid username or password."}}})
    with pytest.raises(ArcGISError, match="Invalid username"):
        ArcGISClient(session=bad).generate_token(token_url, "admin", "wrong")
