#!/usr/bin/env python3
import logging
import os
import time
from typing import Any, Dict, Iterator, Optional
import requests
import urllib3
from prometheus_client import core, start_http_server
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily
from prometheus_client.registry import Collector

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("technitium_exporter")

TECHNITIUM_BASE_URL = os.getenv("TECHNITIUM_BASE_URL", "http://technitium:5380").rstrip(
    "/"
)
TECHNITIUM_TOKEN = os.getenv("TECHNITIUM_TOKEN", "")
TECHNITIUM_STATS_RANGE = os.getenv("TECHNITIUM_STATS_RANGE", "LastHour")
TECHNITIUM_TOP_LIMIT = int(os.getenv("TECHNITIUM_TOP_LIMIT", "50"))
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "9105"))
TECHNITIUM_VERIFY_SSL = os.getenv("TECHNITIUM_VERIFY_SSL", "true").lower() == "true"

# Optional: For identifying this specific Technitium instance in Grafana
SERVER_LABEL = os.getenv("SERVER_LABEL", "technitium")
# Optional: If using Technitium Clustering, specify which node to query
TECHNITIUM_NODE = os.getenv("TECHNITIUM_NODE", "")

# Suppress insecure request warnings if SSL verify is disabled
if not TECHNITIUM_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Inherit from Collector to satisfy type-checker
class TechnitiumCollector(Collector):
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "TechnitiumPrometheusExporter/2.4"})
        # Apply SSL verification setting globally to the session
        self.session.verify = TECHNITIUM_VERIFY_SSL

    def _call_api(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generic helper to handle API calls and errors."""
        url = f"{TECHNITIUM_BASE_URL}{endpoint}"
        default_params = {"token": TECHNITIUM_TOKEN}

        # Add Node parameter if configured (for clustering)
        if TECHNITIUM_NODE:
            default_params["node"] = TECHNITIUM_NODE

        if params:
            default_params.update(params)

        try:
            resp = self.session.get(url, params=default_params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                logger.error("Technitium API returned non-ok status for %s", endpoint)
                return {}
            return data.get("response", {})
        except Exception as e:
            # Sanitize the token from the error message
            error_msg = str(e)
            if TECHNITIUM_TOKEN and len(TECHNITIUM_TOKEN) > 0:
                error_msg = error_msg.replace(TECHNITIUM_TOKEN, "REDACTED")

            logger.error("Request Failed [%s]: %s", endpoint, error_msg)
            return {}

    def collect(self) -> Iterator[core.Metric]:
        start_t = time.time()

        # -------------------------------------------------------------------
        # 1. Dashboard Stats
        # -------------------------------------------------------------------
        stats_data = self._call_api(
            "/api/dashboard/stats/get",
            {"type": TECHNITIUM_STATS_RANGE, "utc": "true"},
        )
        stats = stats_data.get("stats", {})

        # UP Metric
        g_up = GaugeMetricFamily(
            "technitium_up", "Technitium API reachable", labels=["server"]
        )
        g_up.add_metric([SERVER_LABEL], 1.0 if stats else 0.0)
        yield g_up

        if stats:
            # Simple Gauges
            simple_map = {
                "technitium_dns_clients_window": "totalClients",
                "technitium_dns_zones": "zones",
                "technitium_dns_cached_entries": "cachedEntries",
                "technitium_dns_allowed_zones": "allowedZones",
                "technitium_dns_blocked_zones": "blockedZones",
                "technitium_dns_allowlist_zones": "allowListZones",
                "technitium_dns_blocklist_zones": "blockListZones",
            }
            for metric, key in simple_map.items():
                g = GaugeMetricFamily(
                    metric, f"From Dashboard Stats: {key}", labels=["server"]
                )
                g.add_metric([SERVER_LABEL], float(stats.get(key, 0)))
                yield g

            # Query Counters (window-scoped snapshot)
            q_metric = GaugeMetricFamily(
                "technitium_dns_queries_window",
                "Queries by result category in the current stats window",
                labels=["server", "category"],
            )
            q_map = {
                "totalQueries": "all",
                "totalNoError": "no_error",
                "totalServerFailure": "servfail",
                "totalNxDomain": "nxdomain",
                "totalRefused": "refused",
                "totalAuthoritative": "authoritative",
                "totalRecursive": "recursive",
                "totalCached": "cached",
                "totalBlocked": "blocked",
                "totalDropped": "dropped",
            }
            for key, label in q_map.items():
                q_metric.add_metric([SERVER_LABEL, label], float(stats.get(key, 0)))
            yield q_metric

            # Charts: Response Type
            resp_chart = stats_data.get("queryResponseChartData", {})
            if resp_chart and resp_chart.get("datasets"):
                resp_metric = GaugeMetricFamily(
                    "technitium_dns_response_type_total",
                    "Response types in the current stats window",
                    labels=["server", "type"],
                )
                labels = resp_chart.get("labels", [])
                data = resp_chart["datasets"][0].get("data", [])
                for label, val in zip(labels, data):
                    resp_metric.add_metric([SERVER_LABEL, label], float(val))
                yield resp_metric

            # Charts: Query Type
            type_chart = stats_data.get("queryTypeChartData", {})
            if type_chart and type_chart.get("datasets"):
                type_metric = GaugeMetricFamily(
                    "technitium_dns_query_type_total",
                    "DNS query types in the current stats window",
                    labels=["server", "qtype"],
                )
                labels = type_chart.get("labels", [])
                data = type_chart["datasets"][0].get("data", [])
                for label, val in zip(labels, data):
                    type_metric.add_metric([SERVER_LABEL, label], float(val))
                yield type_metric

            # Charts: Protocol
            proto_chart = stats_data.get("protocolTypeChartData", {})
            if proto_chart and proto_chart.get("datasets"):
                proto_metric = GaugeMetricFamily(
                    "technitium_dns_protocol_queries",
                    "Queries by protocol in the current stats window",
                    labels=["server", "protocol"],
                )
                labels = proto_chart.get("labels", [])
                data = proto_chart["datasets"][0].get("data", [])
                for label, val in zip(labels, data):
                    proto_metric.add_metric([SERVER_LABEL, label], float(val))
                yield proto_metric

        # -------------------------------------------------------------------
        # 2. Zone Health
        # -------------------------------------------------------------------
        zones_data = self._call_api(
            "/api/zones/list", {"pageNumber": 1, "pageSize": 1000}
        )
        zones_list = zones_data.get("zones", [])
        if zones_list:
            z_info = InfoMetricFamily(
                "technitium_zone",
                "Zone detailed information",
                labels=["server", "zone", "type", "disabled", "internal"],
            )
            for zone in zones_list:
                z_info.add_metric(
                    [
                        SERVER_LABEL,
                        zone.get("name", "unknown"),
                        zone.get("type", "unknown"),
                        str(zone.get("disabled", False)).lower(),
                        str(zone.get("internal", False)).lower(),
                    ],
                    {"serial": str(zone.get("soaSerial", 0))},
                )
            yield z_info

        # -------------------------------------------------------------------
        # 3. DHCP Stats
        # -------------------------------------------------------------------
        dhcp_leases_data = self._call_api("/api/dhcp/leases/list")
        leases = dhcp_leases_data.get("leases", [])
        if leases:
            counts: Dict[str, Dict[str, int]] = {}
            for lease in leases:
                scope = lease.get("scope", "unknown")
                ltype = lease.get("type", "Unknown")
                if scope not in counts:
                    counts[scope] = {}
                counts[scope][ltype] = counts[scope].get(ltype, 0) + 1

            dhcp_lease_metric = GaugeMetricFamily(
                "technitium_dhcp_leases_total",
                "Number of DHCP leases by scope and type",
                labels=["server", "scope", "type"],
            )
            for scope, type_map in counts.items():
                for ltype, count in type_map.items():
                    dhcp_lease_metric.add_metric(
                        [SERVER_LABEL, scope, ltype],
                        float(count),
                    )
            yield dhcp_lease_metric

        # -------------------------------------------------------------------
        # 4. Top Stats
        # -------------------------------------------------------------------
        for stats_type, metric_name, labels, json_key in [
            (
                "TopClients",
                "technitium_dns_top_client_hits",
                ["server", "client_ip", "client_name"],
                "topClients",
            ),
            (
                "TopDomains",
                "technitium_dns_top_domain_hits",
                ["server", "domain"],
                "topDomains",
            ),
            (
                "TopBlockedDomains",
                "technitium_dns_top_blocked_domain_hits",
                ["server", "domain"],
                "topBlockedDomains",
            ),
        ]:
            try:
                data = self._call_api(
                    "/api/dashboard/stats/getTop",
                    {"statsType": stats_type, "limit": TECHNITIUM_TOP_LIMIT},
                )
                items = data.get(json_key, [])
                if items:
                    m = GaugeMetricFamily(
                        metric_name, f"Hits for {stats_type}", labels=labels
                    )
                    for item in items:
                        if stats_type == "TopClients":
                            l_vals = [
                                SERVER_LABEL,
                                item.get("name", ""),
                                item.get("domain", ""),
                            ]
                        else:
                            l_vals = [SERVER_LABEL, item.get("name", "")]
                        m.add_metric(l_vals, float(item.get("hits", 0)))
                    yield m
            except Exception as e:
                # Token redacting logic
                error_msg = str(e)
                if TECHNITIUM_TOKEN and len(TECHNITIUM_TOKEN) > 0:
                    error_msg = error_msg.replace(TECHNITIUM_TOKEN, "REDACTED")
                logger.error("Failed to scrape %s: %s", stats_type, error_msg)

        # -------------------------------------------------------------------
        # 5. Scrape Duration
        # -------------------------------------------------------------------
        g_dur = GaugeMetricFamily(
            "technitium_scrape_duration_seconds",
            "Exporter scrape duration",
            labels=["server"],
        )
        g_dur.add_metric([SERVER_LABEL], time.time() - start_t)
        yield g_dur


if __name__ == "__main__":
    if not TECHNITIUM_TOKEN:
        logger.error("TECHNITIUM_TOKEN is required.")
        exit(1)

    logger.info(
        "Starting Technitium exporter on port %d (Server Label: %s)",
        EXPORTER_PORT,
        SERVER_LABEL,
    )
    core.REGISTRY.register(TechnitiumCollector())
    start_http_server(EXPORTER_PORT)
    while True:
        time.sleep(1)
