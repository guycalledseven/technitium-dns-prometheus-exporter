"""Collector tests with a mocked Technitium API.

Run directly (python tests/test_collector.py) or via pytest.
Needs the exporter's dependencies installed (prometheus_client, requests).
"""
import os
import sys

os.environ["TECHNITIUM_TOKEN"] = "testtoken"
os.environ["TECHNITIUM_NODES"] = "primary,node-01"

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "technitium_exporter")
)
import technitium_exporter as te  # noqa: E402

SESSION_INFO = {
    "status": "ok",
    "username": "readonly",
    "info": {
        "version": "15.3",
        "clusterInitialized": True,
        "clusterDomain": "example.com",
    },
}
HEALTH_OK = {"server": "primary", "status": "ok"}
HEALTH_FAIL = {"status": "error", "errorMessage": "resolution failed"}
STATS = {
    "status": "ok",
    "response": {
        "stats": {"totalQueries": 100, "totalServerFailure": 2, "zones": 5},
        "queryTypeChartData": {},
    },
}
EMPTY_OK = {"status": "ok", "response": {}}
REALTIME_TEXT = "uptime_seconds 1000\nqueries_total 100\nserver_failure_total 2\n"


def _mock_api(session_info):
    calls = []

    def fake_raw(self, endpoint, node, params=None):
        calls.append((endpoint, node))
        if endpoint == "/api/user/session/get":
            return session_info
        if endpoint == "/api/dnsClient/healthCheck":
            assert params["domain"] == "localhost" and params["type"] == "A"
            return HEALTH_OK if node == "primary" else HEALTH_FAIL
        if endpoint == "/api/dashboard/stats/get":
            return STATS
        return EMPTY_OK

    te.TechnitiumCollector._call_api_raw = fake_raw
    te.TechnitiumCollector._fetch_metrics_text = lambda self, node: REALTIME_TEXT
    return calls


def _collect():
    return {m.name: m for m in te.TechnitiumCollector().collect()}


def test_healthcheck_and_server_info_on_15_3():
    calls = _mock_api(SESSION_INFO)
    metrics = _collect()

    health = {s.labels["server"]: s.value for s in metrics["technitium_health"].samples}
    assert health == {"primary": 1.0, "node-01": 0.0}, health

    info = metrics["technitium_server"].samples
    assert len(info) == 1
    assert info[0].labels == {
        "version": "15.3",
        "cluster_initialized": "true",
        "cluster_domain": "example.com",
    }, info[0].labels

    # session/get is controller-level: exactly one call, no node param
    assert calls.count(("/api/user/session/get", None)) == 1
    assert ("/api/dnsClient/healthCheck", "primary") in calls
    assert ("/api/dnsClient/healthCheck", "node-01") in calls

    # pre-existing metrics unaffected
    up = {s.labels["server"]: s.value for s in metrics["technitium_up"].samples}
    assert up == {"primary": 1.0, "node-01": 1.0}, up
    realtime = {
        (s.labels["server"], s.labels["category"]): s.value
        for s in metrics["technitium_dns_realtime_queries"].samples
        if s.name.endswith("_total")
    }
    assert realtime[("primary", "all")] == 100.0


def test_healthcheck_skipped_below_15_3():
    old = dict(SESSION_INFO, info=dict(SESSION_INFO["info"], version="15.1"))
    calls = _mock_api(old)
    metrics = _collect()

    assert metrics["technitium_health"].samples == []
    assert all(c[0] != "/api/dnsClient/healthCheck" for c in calls)
    assert metrics["technitium_server"].samples[0].labels["version"] == "15.1"


def test_session_info_unavailable_degrades_gracefully():
    _mock_api({})
    metrics = _collect()

    assert metrics["technitium_server"].samples == []
    assert metrics["technitium_health"].samples == []
    assert len(metrics["technitium_up"].samples) == 2


def test_parse_version():
    parse = te.TechnitiumCollector._parse_version
    assert parse("15.3") == (15, 3)
    assert parse("15.3.1") == (15, 3)
    assert parse("15") == (15,)
    assert parse("") is None
    assert parse("beta") is None
    assert parse("15.3") >= te.HEALTHCHECK_MIN_VERSION
    assert parse("15.1") < te.HEALTHCHECK_MIN_VERSION
    assert parse("16.0") >= te.HEALTHCHECK_MIN_VERSION


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
