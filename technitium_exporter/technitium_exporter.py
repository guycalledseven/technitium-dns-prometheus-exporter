#!/usr/bin/env python3
import logging
import os
import time
from typing import Any, Dict, Iterator, Optional, List
import requests
import urllib3
from prometheus_client import core, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, InfoMetricFamily
from prometheus_client.registry import Collector

# ---------------------------------------------------------------------------
# Version — bump on every release, together with the "Updates in vX.Y.Z"
# section in README.md. Sent as the User-Agent to the Technitium API and
# printed at startup.
# ---------------------------------------------------------------------------

EXPORTER_VERSION = "2.2.0"

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

# CLUSTERING CONFIG
# Pass a comma-separated list of nodes: e.g. "primary,node-01,node-02"
# If empty, defaults to a single "local" scrape without the ?node= param.
raw_nodes = os.getenv("TECHNITIUM_NODES", "")
TECHNITIUM_NODES = (
    [n.strip() for n in raw_nodes.split(",")] if raw_nodes.strip() else []
)

# Fallback for single server setups using the old env var
SERVER_LABEL_DEFAULT = os.getenv("SERVER_LABEL", "technitium")

# Health check via /api/dnsClient/healthCheck (Technitium >= 15.3).
# Proves actual DNS resolution without creating query log entries.
# Requires the token account to hold the "DnsClient: View" permission.
TECHNITIUM_HEALTHCHECK = os.getenv("TECHNITIUM_HEALTHCHECK", "true").lower() == "true"
TECHNITIUM_HEALTHCHECK_DOMAIN = os.getenv("TECHNITIUM_HEALTHCHECK_DOMAIN", "localhost")
TECHNITIUM_HEALTHCHECK_TYPE = os.getenv("TECHNITIUM_HEALTHCHECK_TYPE", "A")

HEALTHCHECK_MIN_VERSION = (15, 3)

if not TECHNITIUM_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TechnitiumCollector(Collector):
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": f"TechnitiumPrometheusExporter/{EXPORTER_VERSION}"}
        )
        self.session.verify = TECHNITIUM_VERIFY_SSL
        self._healthcheck_skip_logged = False

    @staticmethod
    def _parse_version(version: str) -> Optional[tuple]:
        try:
            return tuple(int(p) for p in version.split(".")[:2])
        except (ValueError, AttributeError):
            return None

    def _call_api_raw(
        self,
        endpoint: str,
        node: Optional[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Returns the full JSON document. Some endpoints (session/get,
        # dnsClient/healthCheck) put their payload at the top level instead of
        # inside the usual "response" wrapper.
        url = f"{TECHNITIUM_BASE_URL}{endpoint}"
        default_params = {"token": TECHNITIUM_TOKEN}

        # If a node is specified (Clustering), add it to params
        if node:
            default_params["node"] = node

        if params:
            default_params.update(params)

        try:
            resp = self.session.get(url, params=default_params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            error_msg = (
                str(e).replace(TECHNITIUM_TOKEN, "REDACTED")
                if TECHNITIUM_TOKEN
                else str(e)
            )
            logger.error(
                f"Request Failed [{node or 'local'} - {endpoint}]: {error_msg}"
            )
            return {}

    def _call_api(
        self,
        endpoint: str,
        node: Optional[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        data = self._call_api_raw(endpoint, node, params)
        if not data:
            return {}
        if data.get("status") != "ok":
            logger.error(
                f"Error from node '{node or 'local'}': {data.get('errorMessage')}"
            )
            return {}
        return data.get("response", {})

    def _fetch_metrics_text(self, node: Optional[str]) -> str:
        url = f"{TECHNITIUM_BASE_URL}/api/dashboard/metrics/text"
        params = {"token": TECHNITIUM_TOKEN}
        if node:
            params["node"] = node

        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.text or ""
        except Exception as e:
            error_msg = (
                str(e).replace(TECHNITIUM_TOKEN, "REDACTED")
                if TECHNITIUM_TOKEN
                else str(e)
            )
            logger.error(
                f"Realtime metrics request failed [{node or 'local'}]: {error_msg}"
            )
            return ""

    @staticmethod
    def _parse_prometheus_text(text: str) -> Dict[str, float]:
        data: Dict[str, float] = {}
        if not text:
            return data
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    data[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        return data

    def collect(self) -> Iterator[core.Metric]:
        start_t = time.time()

        # Determine which nodes to scrape.
        # If TECHNITIUM_NODES is empty, we scrape "None" (local instance) and use SERVER_LABEL_DEFAULT
        targets = TECHNITIUM_NODES if TECHNITIUM_NODES else [None]

        # Prepare Metric Families (We create them once, then populate with samples from all nodes)
        g_up = GaugeMetricFamily(
            "technitium_up", "Technitium API reachable", labels=["server"]
        )
        g_health = GaugeMetricFamily(
            "technitium_health",
            "DNS resolution health check via /api/dnsClient/healthCheck "
            "(1 = server resolves queries; requires Technitium >= 15.3 and "
            "the DnsClient: View permission)",
            labels=["server"],
        )
        server_info = InfoMetricFamily(
            "technitium_server",
            "Technitium DNS server version and cluster info "
            "(controller's, from /api/user/session/get)",
        )
        stats_range_info = InfoMetricFamily(
            "technitium_stats_range",
            "DNS Server configured stats window range",
            labels=["server"],
        )

        # Dashboard Stats Metrics
        simple_metrics = {
            "technitium_dns_clients_window": ("totalClients", "Unique Clients"),
            "technitium_dns_zones": ("zones", "Total Zones"),
            "technitium_dns_cached_entries": ("cachedEntries", "Cache Entries"),
            "technitium_dns_allowed_zones": ("allowedZones", "Allowed Zones"),
            "technitium_dns_manual_blocked_zones": ("blockedZones", "Blocked Zones"),
            "technitium_dns_allowlist_zones": ("allowListZones", "Allow List Zones"),
            "technitium_dns_blocklist_zones": ("blockListZones", "Block List Zones"),
        }

        g_families = {}
        for m_name, (key, desc) in simple_metrics.items():
            g_families[m_name] = GaugeMetricFamily(
                m_name, f"From Dashboard Stats: {desc}", labels=["server"]
            )

        q_metric = GaugeMetricFamily(
            "technitium_dns_queries_window",
            "Queries by result category",
            labels=["server", "category"],
        )
        resp_metric = GaugeMetricFamily(
            "technitium_dns_response_type_total",
            "Response types",
            labels=["server", "type"],
        )
        type_metric = GaugeMetricFamily(
            "technitium_dns_query_type_total",
            "DNS query types",
            labels=["server", "qtype"],
        )
        proto_metric = GaugeMetricFamily(
            "technitium_dns_protocol_queries",
            "Queries by protocol",
            labels=["server", "protocol"],
        )

        z_info = InfoMetricFamily(
            "technitium_zone",
            "Zone detailed information",
            labels=["server", "zone", "type", "disabled", "internal"],
        )
        dhcp_lease_metric = GaugeMetricFamily(
            "technitium_dhcp_leases_total",
            "Active DHCP leases",
            labels=["server", "scope", "type"],
        )

        # Top Stats Metrics
        top_metrics = {
            "TopClients": GaugeMetricFamily(
                "technitium_dns_top_client_hits",
                "Hits for TopClients",
                labels=["server", "client_ip", "client_name"],
            ),
            "TopDomains": GaugeMetricFamily(
                "technitium_dns_top_domain_hits",
                "Hits for TopDomains",
                labels=["server", "domain"],
            ),
            "TopBlockedDomains": GaugeMetricFamily(
                "technitium_dns_top_blocked_domain_hits",
                "Hits for TopBlockedDomains",
                labels=["server", "domain"],
            ),
        }

        # Realtime (lifetime) Metrics
        realtime_uptime = GaugeMetricFamily(
            "technitium_realtime_uptime_seconds",
            "DNS Server uptime in seconds (lifetime)",
            labels=["server"],
        )
        realtime_start_time = GaugeMetricFamily(
            "technitium_realtime_start_time_seconds",
            "DNS Server start time since epoch in seconds (lifetime)",
            labels=["server"],
        )
        realtime_queries = CounterMetricFamily(
            "technitium_dns_realtime_queries_total",
            "DNS query lifetime counters by category",
            labels=["server", "category"],
        )
        realtime_clients = GaugeMetricFamily(
            "technitium_realtime_clients_total",
            "Total unique DNS clients seen since server start (lifetime)",
            labels=["server"],
        )
        realtime_queries_map = {
            "queries_total": "all",
            "no_error_total": "no_error",
            "server_failure_total": "servfail",
            "nx_domain_total": "nxdomain",
            "refused_total": "refused",
            "authoritative_total": "authoritative",
            "recursive_total": "recursive",
            "cached_total": "cached",
            "blocked_total": "blocked",
            "dropped_total": "dropped",
        }

        # Session info: server version + cluster details. One call per scrape,
        # answered by the controller (the endpoint has no node parameter).
        session_data = self._call_api_raw("/api/user/session/get", None)
        info = session_data.get("info") or {}
        version = self._parse_version(info.get("version", ""))
        if info:
            server_info.add_metric(
                [],
                {
                    "version": str(info.get("version", "")),
                    "cluster_initialized": str(
                        info.get("clusterInitialized", False)
                    ).lower(),
                    "cluster_domain": str(info.get("clusterDomain", "")),
                },
            )

        # Health check needs 15.3+; skip quietly (logged once) below that or
        # when the version can't be determined.
        healthcheck_active = (
            TECHNITIUM_HEALTHCHECK
            and version is not None
            and version >= HEALTHCHECK_MIN_VERSION
        )
        if TECHNITIUM_HEALTHCHECK and not healthcheck_active:
            if not self._healthcheck_skip_logged:
                logger.warning(
                    "Skipping health check metric: Technitium version is "
                    f"{info.get('version') or 'unknown'}, need >= "
                    f"{'.'.join(map(str, HEALTHCHECK_MIN_VERSION))}."
                )
                self._healthcheck_skip_logged = True

        # Loop through every node (or the single local instance)
        for node in targets:
            # If node is None, use default label. If node is string, use it as the label.
            server_label = node if node else SERVER_LABEL_DEFAULT

            # 0. Health Check (does not create query log entries)
            if healthcheck_active:
                hc = self._call_api_raw(
                    "/api/dnsClient/healthCheck",
                    node,
                    {
                        "domain": TECHNITIUM_HEALTHCHECK_DOMAIN,
                        "type": TECHNITIUM_HEALTHCHECK_TYPE,
                    },
                )
                healthy = hc.get("status") == "ok"
                if hc and not healthy:
                    logger.error(
                        f"Health check failed [{server_label}]: "
                        f"{hc.get('errorMessage')}"
                    )
                g_health.add_metric([server_label], 1.0 if healthy else 0.0)

            # 1. Dashboard Stats
            stats_data = self._call_api(
                "/api/dashboard/stats/get",
                node,
                {"type": TECHNITIUM_STATS_RANGE, "utc": "true"},
            )
            stats = stats_data.get("stats", {})

            # UP STATUS
            g_up.add_metric([server_label], 1.0 if stats else 0.0)
            stats_range_info.add_metric(
                [server_label], {"range": TECHNITIUM_STATS_RANGE}
            )

            if stats:
                for m_name, (key, _) in simple_metrics.items():
                    g_families[m_name].add_metric(
                        [server_label], float(stats.get(key, 0))
                    )

                # Query Counters
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
                for key, cat in q_map.items():
                    q_metric.add_metric([server_label, cat], float(stats.get(key, 0)))

                # Charts Processing Helper
                def process_chart(chart_key, metric_obj, label_key):
                    chart = stats_data.get(chart_key, {})
                    if chart and chart.get("datasets"):
                        labels = chart.get("labels", [])
                        data = chart["datasets"][0].get("data", [])
                        for l, v in zip(labels, data):
                            metric_obj.add_metric([server_label, l], float(v))

                process_chart("queryResponseChartData", resp_metric, "type")
                process_chart("queryTypeChartData", type_metric, "qtype")
                process_chart("protocolTypeChartData", proto_metric, "protocol")

            # 2. Zone Health
            all_zones: List[Dict[str, Any]] = []
            page = 1
            while page <= 1000:
                zones_data = self._call_api(
                    "/api/zones/list", node, {"pageNumber": page, "pageSize": 1000}
                )
                if not zones_data:
                    break
                all_zones.extend(zones_data.get("zones", []))
                total_pages = int(zones_data.get("totalPages", 1) or 1)
                if page >= total_pages:
                    break
                page += 1
            for zone in all_zones:
                z_info.add_metric(
                    [
                        server_label,
                        zone.get("name", ""),
                        zone.get("type", ""),
                        str(zone.get("disabled", False)).lower(),
                        str(zone.get("internal", False)).lower(),
                    ],
                    {"serial": str(zone.get("soaSerial", 0))},
                )

            # 3. DHCP
            dhcp_data = self._call_api("/api/dhcp/leases/list", node)
            leases = dhcp_data.get("leases", [])
            if leases:
                counts = {}  # (scope, type) -> count
                for lease in leases:
                    k = (lease.get("scope", "unknown"), lease.get("type", "Unknown"))
                    counts[k] = counts.get(k, 0) + 1
                for (scope, ltype), count in counts.items():
                    dhcp_lease_metric.add_metric(
                        [server_label, scope, ltype], float(count)
                    )

            # 4. Top Stats
            for stats_type, json_key in [
                ("TopClients", "topClients"),
                ("TopDomains", "topDomains"),
                ("TopBlockedDomains", "topBlockedDomains"),
            ]:
                try:
                    data = self._call_api(
                        "/api/dashboard/stats/getTop",
                        node,
                        {"statsType": stats_type, "limit": TECHNITIUM_TOP_LIMIT},
                    )
                    for item in data.get(json_key, []):
                        if stats_type == "TopClients":
                            top_metrics[stats_type].add_metric(
                                [
                                    server_label,
                                    item.get("name", ""),
                                    item.get("domain", ""),
                                ],
                                float(item.get("hits", 0)),
                            )
                        else:
                            top_metrics[stats_type].add_metric(
                                [server_label, item.get("name", "")],
                                float(item.get("hits", 0)),
                            )
                except:
                    pass

            # 5. Realtime (lifetime) Metrics
            metrics_text = self._fetch_metrics_text(node)
            if metrics_text:
                parsed = self._parse_prometheus_text(metrics_text)
                if "uptime_seconds" in parsed:
                    realtime_uptime.add_metric(
                        [server_label], parsed["uptime_seconds"]
                    )
                if "start_time" in parsed:
                    realtime_start_time.add_metric(
                        [server_label], parsed["start_time"] / 1000.0
                    )
                for raw_key, cat in realtime_queries_map.items():
                    if raw_key in parsed:
                        realtime_queries.add_metric(
                            [server_label, cat], parsed[raw_key]
                        )
                if "clients_total" in parsed:
                    realtime_clients.add_metric(
                        [server_label], parsed["clients_total"]
                    )

        # Yield all collected metrics
        yield g_up
        yield g_health
        yield server_info
        yield stats_range_info
        for m in g_families.values():
            yield m
        yield q_metric
        yield resp_metric
        yield type_metric
        yield proto_metric
        yield z_info
        yield dhcp_lease_metric
        for m in top_metrics.values():
            yield m
        yield realtime_uptime
        yield realtime_start_time
        yield realtime_queries
        yield realtime_clients

        # Scrape Duration
        g_dur = GaugeMetricFamily(
            "technitium_scrape_duration_seconds", "Exporter scrape duration", labels=[]
        )
        g_dur.add_metric([], time.time() - start_t)
        yield g_dur


if __name__ == "__main__":
    if not TECHNITIUM_TOKEN:
        logger.error("TECHNITIUM_TOKEN is required.")
        exit(1)

    if TECHNITIUM_NODES:
        logger.info(
            f"Starting Cluster Exporter v{EXPORTER_VERSION} on port {EXPORTER_PORT}. Monitoring nodes: {TECHNITIUM_NODES}"
        )
    else:
        logger.info(
            f"Starting Single-Server Exporter v{EXPORTER_VERSION} on port {EXPORTER_PORT}. Label: {SERVER_LABEL_DEFAULT}"
        )

    core.REGISTRY.register(TechnitiumCollector())
    start_http_server(EXPORTER_PORT)
    while True:
        time.sleep(1)
