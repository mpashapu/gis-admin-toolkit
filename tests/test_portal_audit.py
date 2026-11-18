import csv
from datetime import datetime, timedelta, timezone

from conftest import FakeSession

from gisadmin.arcgis import ArcGISClient
from gisadmin.common import Status
from gisadmin.portal_audit import audit_item, main, search_items, summarize

PORTAL = "https://gis.example.gov/portal"
SEARCH = f"{PORTAL}/sharing/rest/search"
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def ms(days_ago: int) -> int:
    return int((NOW - timedelta(days=days_ago)).timestamp() * 1000)


def item(i, **overrides):
    base = {
        "id": f"id{i}",
        "title": f"Item {i}",
        "type": "Feature Service",
        "owner": "gisadmin",
        "access": "org",
        "created": ms(30),
        "modified": ms(10),
        "numViews": 5,
        "size": 2_097_152,
        "snippet": "s",
        "description": "d",
        "tags": ["t"],
        "url": "https://gis.example.gov/x",
        "protected": True,
    }
    base.update(overrides)
    return base


def paged_search(all_items, page_size=100):
    def handler(params):
        start = int(params["start"])
        chunk = all_items[start - 1 : start - 1 + page_size]
        next_start = start + page_size if start - 1 + page_size < len(all_items) else -1
        return {"total": len(all_items), "start": start, "nextStart": next_start, "results": chunk}

    return handler


def test_search_pages_until_next_start_is_minus_one():
    items = [item(i) for i in range(205)]
    session = FakeSession({SEARCH: paged_search(items)})
    found = list(search_items(ArcGISClient(session=session), PORTAL, "orgid:abc"))
    assert len(found) == 205
    assert [c["params"]["start"] for c in session.calls] == [1, 101, 201]


def test_search_stops_if_server_does_not_advance():
    session = FakeSession({SEARCH: {"total": 500, "nextStart": 1, "results": [item(1)]}})
    assert len(list(search_items(ArcGISClient(session=session), PORTAL, "q"))) == 1


def test_clean_item_has_no_flags():
    assert audit_item(item(1), stale_days=365, unused_days=90, now=NOW).flags == []


def test_flags():
    raw = item(
        2,
        access="public",
        protected=False,
        modified=ms(400),
        created=ms(500),
        numViews=0,
        snippet="",
        url="http://old.example.gov/arcgis/rest/services/x/MapServer",
    )
    audited = audit_item(raw, stale_days=365, unused_days=90, now=NOW)
    assert audited.flags == ["PUBLIC", "NOT_PROTECTED", "STALE", "UNUSED", "NO_METADATA", "INSECURE_URL"]
    assert audited.days_since_modified == 400
    assert audited.size_mb == 2.0


def test_new_items_with_no_views_are_not_unused_yet():
    audited = audit_item(item(3, numViews=0, created=ms(5)), stale_days=365, unused_days=90, now=NOW)
    assert "UNUSED" not in audited.flags


def test_summary_highlights_public_stale_items():
    items = [
        audit_item(
            item(1, access="public", modified=ms(500), created=ms(600)), stale_days=365, unused_days=90, now=NOW
        ),
        audit_item(item(2), stale_days=365, unused_days=90, now=NOW),
    ]
    report = summarize(items, PORTAL, "orgid:abc")
    assert "## Public and stale (1): review first" in report
    assert "| public | 1 |" in report


def test_main_signs_in_finds_org_and_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("GIS_PASSWORD", "pw")
    items = [item(1), item(2, access="public", modified=ms(2000), created=ms(2000))]
    session = FakeSession(
        {
            f"{PORTAL}/sharing/rest/generateToken": {"token": "tok"},
            f"{PORTAL}/sharing/rest/portals/self": {"id": "ORG123"},
            SEARCH: paged_search(items),
        }
    )
    prefix = tmp_path / "audit"
    code = main(
        [PORTAL + "/sharing/rest", "--username", "admin", "-o", str(prefix)], client=ArcGISClient(session=session)
    )

    assert code == Status.WARNING  # public + stale content exists
    search_call = next(c for c in session.calls if c["url"] == SEARCH)
    assert search_call["params"]["q"] == "orgid:ORG123"
    with open(tmp_path / "audit.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 and "PUBLIC" in rows[1]["flags"]
    assert (tmp_path / "audit_summary.md").exists()


def test_anonymous_requires_query():
    assert main([PORTAL], client=ArcGISClient(session=FakeSession({}))) == Status.ERROR


def test_anonymous_with_query_works(tmp_path):
    session = FakeSession({SEARCH: paged_search([item(1)])})
    code = main([PORTAL, "--query", "owner:someone", "-o", str(tmp_path / "a")], client=ArcGISClient(session=session))
    assert code == Status.OK
    assert session.calls[0]["method"] == "GET"
