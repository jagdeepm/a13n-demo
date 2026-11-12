"""Simulated production fleet with one planted incident.

Story: checkout-api v2.4.1 shipped 40 min ago and shrank the DB connection pool 50 -> 5.
Symptoms: checkout p99 latency + 5xx spike, payments-db fine, "pool exhausted" in logs.
Correct fix: roll back checkout-api to v2.4.0 (scaling only half-helps, restart does nothing lasting).

Tools are plain typed Python functions: the signature + docstring is the schema the model sees.
"""

from typing import Literal

Service = Literal["checkout-api", "payments-db", "auth-svc", "edge-gateway"]

# ponytail: in-process dict, resets on restart. Swap these bodies for kubectl/Prometheus/Loki calls to go real.
FLEET: dict[str, dict] = {
    "checkout-api": {"version": "v2.4.1", "replicas": 3, "p99_ms": 2400, "err_rate": 0.18, "cpu": 0.35, "healthy": False},
    "payments-db": {"version": "pg-16.4", "replicas": 1, "p99_ms": 12, "err_rate": 0.0, "cpu": 0.22, "healthy": True},
    "auth-svc": {"version": "v1.9.0", "replicas": 2, "p99_ms": 45, "err_rate": 0.001, "cpu": 0.18, "healthy": True},
    "edge-gateway": {"version": "v3.1.2", "replicas": 4, "p99_ms": 2450, "err_rate": 0.09, "cpu": 0.30, "healthy": False},
}
DEPLOYS = [
    {"service": "auth-svc", "version": "v1.9.0", "minutes_ago": 2900, "author": "dana", "change": "bump jwt lib"},
    {"service": "checkout-api", "version": "v2.4.0", "minutes_ago": 1440, "author": "lee", "change": "add promo codes"},
    {"service": "checkout-api", "version": "v2.4.1", "minutes_ago": 40, "author": "sam",
     "change": "refactor db config loader (DB_POOL_SIZE default 50 -> 5)"},
]
_LOGS = {
    "checkout-api": [
        "WARN  db: connection pool exhausted (size=5, waiting=212), request queued",
        "ERROR http: POST /checkout 503 after 30001ms: timeout acquiring db connection",
        "WARN  db: connection pool exhausted (size=5, waiting=198), request queued",
        "INFO  http: GET /healthz 200 2ms",
        "ERROR http: POST /checkout 503 after 30000ms: timeout acquiring db connection",
    ],
    "payments-db": ["LOG  checkpoint complete", "LOG  connections: 9 active / 200 max"],
    "auth-svc": ["INFO token issued", "INFO token issued"],
    "edge-gateway": ["WARN upstream checkout-api slow: 2.4s", "ERROR upstream checkout-api 503"],
}
_HEALTHY_LOGS = ["INFO  http: POST /checkout 200 85ms", "INFO  db: pool ok (size=50, in_use=14)"]


def _fix_checkout() -> None:
    FLEET["checkout-api"].update(p99_ms=110, err_rate=0.002, healthy=True)
    FLEET["edge-gateway"].update(p99_ms=140, err_rate=0.002, healthy=True)
    _LOGS["checkout-api"] = _HEALTHY_LOGS


# ---- read-only diagnostics (always allowed) ----

def fleet_status() -> dict:
    """Return health, version, replicas, p99 latency (ms), error rate and CPU for every service."""
    return FLEET


def recent_deploys(service: Service | None = None) -> list[dict]:
    """List recent deploys, newest last. Optionally filter by service."""
    return [d for d in DEPLOYS if service in (None, d["service"])]


def tail_logs(service: Service, lines: int = 5) -> list[str]:
    """Return the last `lines` log lines for a service."""
    return _LOGS[service][-lines:]


# ---- remediation (gated: tool/remediation/* = "ask") ----

def rollback(service: Service, to_version: str) -> str:
    """Roll a service back to a previous version. Production-impacting."""
    known = {d["version"] for d in DEPLOYS if d["service"] == service}
    if to_version not in known:
        return f"refused: {to_version} was never deployed for {service}"
    FLEET[service]["version"] = to_version
    DEPLOYS.append({"service": service, "version": to_version, "minutes_ago": 0, "author": "sre-agent", "change": "rollback"})
    if service == "checkout-api" and to_version == "v2.4.0":
        _fix_checkout()
    return f"{service} rolled back to {to_version}. Re-check fleet_status to verify."


def scale(service: Service, replicas: int) -> str:
    """Change replica count for a service. Production-impacting."""
    if not 1 <= replicas <= 20:
        return "refused: replicas must be 1..20"
    FLEET[service]["replicas"] = replicas
    if service == "checkout-api" and FLEET[service]["version"] == "v2.4.1":
        FLEET[service]["p99_ms"] = int(2400 * 3 / replicas)  # more pods = more pools, still bad
        FLEET[service]["err_rate"] = round(0.18 * 3 / replicas, 3)
    return f"{service} scaled to {replicas} replicas."


def restart(service: Service) -> str:
    """Rolling-restart a service. Production-impacting."""
    return f"{service} restarted. (Pool config unchanged.)"
