import json

from conftest import FakeSession

from gisadmin.arcgis import ArcGISClient
from gisadmin.common import Status
from gisadmin.service_health import check_service, list_services, main, render_html

SERVER = "https://gis.example.gov/arcgis"
REST = f"{SERVER}/rest/services"


def routes(admin_states=None):
    r = {
        REST: {"folders": ["Public", "System"], "services": [{"name": "Basemap", "type": "MapServer"}]},
        f"{REST}/Public": {
            "services": [
                {"name": "Public/Parcels", "type": "FeatureServer"},
                {"name": "Public/Broken", "type": "MapServer"},
                {"name": "Public/Locator", "type": "GeocodeServer"},
            ]
        },
        f"{REST}/System": {"services": [{"name": "System/PublishingTools", "type": "GPServer"}]},
        f"{REST}/Basemap/MapServer": {"layers": []},
        f"{REST}/Public/Parcels/FeatureServer": {"layers": [{"id": 0, "name": "Parcels"}]},
        f"{REST}/Public/Parcels/FeatureServer/0/query": lambda p: {"count": 1234},
        f"{REST}/Public/Broken/MapServer": {"layers": [{"id": 3}]},
        f"{REST}/Public/Broken/MapServer/3/query": {"error": {"code": 500, "message": "Error performing query"}},
        f"{REST}/Public/Locator/GeocodeServer": {"addressFields": []},
        f"{SERVER}/tokens/generateToken": {"token": "t0k3n"},
    }
    for name, state in (admin_states or {}).items():
        r[f"{SERVER}/admin/services/{name}/status"] = {"configuredState": "STARTED", "realTimeState": state}
    return r


def test_list_services_skips_system_folders():
    client = ArcGISClient(session=FakeSession(routes()))
    names = [s["name"] for s in list_services(client, SERVER, {"System", "Utilities"})]
    assert names == ["Basemap", "Public/Parcels", "Public/Broken", "Public/Locator"]


def test_healthy_feature_service_reports_count():
    client = ArcGISClient(session=FakeSession(routes()))
    check = check_service(
        client, SERVER, {"name": "Public/Parcels", "type": "FeatureServer"}, slow_ms=5000, query=True, admin=False
    )
    assert check.status is Status.OK
    assert check.feature_count == 1234
    assert check.message == "ok"


def test_metadata_ok_but_query_broken_is_critical():
    client = ArcGISClient(session=FakeSession(routes()))
    check = check_service(
        client, SERVER, {"name": "Public/Broken", "type": "MapServer"}, slow_ms=5000, query=True, admin=False
    )
    assert check.status is Status.CRITICAL
    assert "query on layer 3 failed" in check.message


def test_slow_response_is_warning():
    client = ArcGISClient(session=FakeSession(routes()))
    check = check_service(
        client, SERVER, {"name": "Public/Locator", "type": "GeocodeServer"}, slow_ms=-1, query=True, admin=False
    )
    assert check.status is Status.WARNING and "slow metadata" in check.message


def test_stopped_service_from_admin_api():
    session = FakeSession(routes({"Public/Parcels.FeatureServer": "STOPPED"}))
    client = ArcGISClient(session=session, token="t")
    check = check_service(
        client, SERVER, {"name": "Public/Parcels", "type": "FeatureServer"}, slow_ms=5000, query=True, admin=True
    )
    assert check.status is Status.CRITICAL and check.state == "STOPPED"
    # Didn't bother querying a stopped service
    assert not any(c["url"].endswith("/query") for c in session.calls)


def test_main_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GIS_PASSWORD", "pw")
    states = {
        "Basemap.MapServer": "STARTED",
        "Public/Parcels.FeatureServer": "STARTED",
        "Public/Broken.MapServer": "STARTED",
        "Public/Locator.GeocodeServer": "STARTED",
    }
    session = FakeSession(routes(states))
    out = tmp_path / "health"
    code = main(
        [
            SERVER + "/rest/services/",
            "--username",
            "admin",
            "--admin",
            "--csv",
            f"{out}.csv",
            "--json",
            f"{out}.json",
            "--html",
            f"{out}.html",
        ],
        client=ArcGISClient(session=session),
    )

    assert code == Status.CRITICAL
    first_line_after_header = capsys.readouterr().out.splitlines()[2]
    assert first_line_after_header.startswith("CRITICAL")  # worst first
    data = json.loads((tmp_path / "health.json").read_text())
    assert len(data["services"]) == 4
    assert "Public/Broken" in (tmp_path / "health.html").read_text()
    assert all(c["method"] == "POST" for c in session.calls)  # token never in a URL


def test_admin_requires_username():
    assert main([SERVER, "--admin"], client=ArcGISClient(session=FakeSession(routes()))) == Status.ERROR


def test_unreachable_directory_is_error():
    assert main([SERVER], client=ArcGISClient(session=FakeSession({}))) == Status.ERROR


def test_html_escapes_service_names():
    from gisadmin.service_health import ServiceCheck

    page = render_html(SERVER, [ServiceCheck(name="<script>", type="MapServer", url="x")])
    assert "<script>" not in page.split("<tbody>")[1]
