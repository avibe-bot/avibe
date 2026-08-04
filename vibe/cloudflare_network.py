"""Local-only Cloudflare Tunnel network path diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import logging
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from config import paths
from vibe import runtime


logger = logging.getLogger(__name__)

CLOUDFLARE_ASN = 13335
CLOUDFLARE_STATUS_COMPONENTS_URL = "https://www.cloudflarestatus.com/api/v2/components.json"
LOCATION_CATALOG_TTL_SECONDS = 7 * 24 * 60 * 60
LOCATION_CATALOG_RETRY_SECONDS = 10 * 60
DIAG_CACHE_TTL_SECONDS = 15
DIAG_TIMEOUT_SECONDS = 0.5
MAX_EDGE_CONNECTIONS = 8

_COMPONENT_LOCATION_RE = re.compile(r"^(.{1,160}) - \(([A-Z0-9]{3})\)$")
_EDGE_LOCATION_RE = re.compile(r"^([A-Za-z]{3})[0-9]+$")
_CF_RAY_RE = re.compile(r"^[0-9A-Fa-f]{8,32}-([A-Za-z0-9]{3})$")

_CATALOG_LOCK = threading.Lock()
_CATALOG: dict[str, dict[str, str]] | None = None
_CATALOG_LOADED_PATH: Path | None = None
_CATALOG_UPDATED_AT = 0.0
_CATALOG_LAST_ATTEMPT = 0.0
_CATALOG_REFRESH_THREAD: threading.Thread | None = None

_DIAG_LOCK = threading.Lock()
_DIAG_CACHE: dict[str, tuple[float, list[str]]] = {}


def _utc_timestamp(now: float | None = None) -> str:
    value = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _catalog_cache_path() -> Path:
    return paths.get_runtime_dir() / "cloudflare-location-catalog.json"


def parse_location_components(payload: Any) -> dict[str, dict[str, str]]:
    """Parse the bounded Cloudflare status location catalog."""

    if not isinstance(payload, dict) or not isinstance(payload.get("components"), list):
        return {}
    catalog: dict[str, dict[str, str]] = {}
    for component in payload["components"][:1000]:
        if not isinstance(component, dict):
            continue
        name = component.get("name")
        if not isinstance(name, str):
            continue
        match = _COMPONENT_LOCATION_RE.fullmatch(name.strip())
        if match is None:
            continue
        location, colo = match.groups()
        country = location.rsplit(",", 1)[-1].strip()
        if not country or len(country) > 80:
            continue
        catalog[colo] = {
            "colo": colo,
            "location": location.strip(),
            "country": country,
        }
    return catalog


def parse_cf_ray_colo(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    match = _CF_RAY_RE.fullmatch(value.strip())
    return match.group(1).upper() if match is not None else None


def colo_from_edge_location(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    match = _EDGE_LOCATION_RE.fullmatch(value.strip())
    return match.group(1).upper() if match is not None else None


def _read_cached_catalog(path: Path) -> tuple[dict[str, dict[str, str]], float]:
    payload = runtime.read_json(path)
    if not isinstance(payload, dict):
        return {}, 0.0
    catalog = payload.get("locations")
    updated_at = payload.get("updated_at_epoch")
    if not isinstance(catalog, dict) or not isinstance(updated_at, (int, float)):
        return {}, 0.0
    validated = parse_location_components(
        {
            "components": [
                {"name": f"{value.get('location', '')} - ({key})"}
                for key, value in list(catalog.items())[:1000]
                if isinstance(key, str) and isinstance(value, dict)
            ]
        }
    )
    return validated, float(updated_at)


def _refresh_location_catalog(path: Path) -> None:
    global _CATALOG, _CATALOG_UPDATED_AT, _CATALOG_REFRESH_THREAD
    try:
        response = requests.get(CLOUDFLARE_STATUS_COMPONENTS_URL, timeout=5.0)
        response.raise_for_status()
        catalog = parse_location_components(response.json())
        if not catalog:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_json(
            path,
            {
                "schema_version": 1,
                "updated_at": _utc_timestamp(),
                "updated_at_epoch": time.time(),
                "locations": catalog,
            },
        )
        with _CATALOG_LOCK:
            if _CATALOG_LOADED_PATH == path:
                _CATALOG = catalog
                _CATALOG_UPDATED_AT = time.time()
    except Exception:
        logger.debug("Could not refresh Cloudflare location catalog", exc_info=True)
    finally:
        with _CATALOG_LOCK:
            if _CATALOG_REFRESH_THREAD is threading.current_thread():
                _CATALOG_REFRESH_THREAD = None


def location_catalog(*, now: float | None = None) -> tuple[dict[str, dict[str, str]], bool]:
    """Return cached locations immediately and refresh stale data in the background."""

    global _CATALOG, _CATALOG_LOADED_PATH, _CATALOG_UPDATED_AT
    global _CATALOG_LAST_ATTEMPT, _CATALOG_REFRESH_THREAD

    current_time = time.time() if now is None else now
    path = _catalog_cache_path()
    with _CATALOG_LOCK:
        if _CATALOG_LOADED_PATH != path:
            _CATALOG, _CATALOG_UPDATED_AT = _read_cached_catalog(path)
            _CATALOG_LOADED_PATH = path
            _CATALOG_LAST_ATTEMPT = 0.0
            _CATALOG_REFRESH_THREAD = None

        stale = not _CATALOG or current_time - _CATALOG_UPDATED_AT >= LOCATION_CATALOG_TTL_SECONDS
        retry_ready = current_time - _CATALOG_LAST_ATTEMPT >= LOCATION_CATALOG_RETRY_SECONDS
        if stale and retry_ready and _CATALOG_REFRESH_THREAD is None:
            _CATALOG_LAST_ATTEMPT = current_time
            thread = threading.Thread(
                target=_refresh_location_catalog,
                args=(path,),
                name="vibe-cloudflare-location-catalog",
                daemon=True,
            )
            _CATALOG_REFRESH_THREAD = thread
            thread.start()
        return dict(_CATALOG or {}), stale


def _local_metrics_base_url(metrics_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(metrics_url)
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        host = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return None
    if not host.is_loopback or port is None:
        return None
    display_host = f"[{host}]" if host.version == 6 else str(host)
    return f"http://{display_host}:{port}"


def parse_tunnel_diag(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("connections"), list):
        return []
    addresses: list[str] = []
    for connection in payload["connections"][:MAX_EDGE_CONNECTIONS]:
        if not isinstance(connection, dict):
            continue
        value = connection.get("edgeAddress")
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        normalized = str(address)
        if not address.is_global or normalized in addresses:
            continue
        addresses.append(normalized)
    return addresses


def tunnel_edge_ips(metrics_url: str | None, *, now: float | None = None) -> list[str]:
    if not isinstance(metrics_url, str):
        return []
    base_url = _local_metrics_base_url(metrics_url)
    if base_url is None:
        return []
    current_time = time.monotonic() if now is None else now
    with _DIAG_LOCK:
        expired = [
            key
            for key, (sampled_at, _) in _DIAG_CACHE.items()
            if current_time - sampled_at >= DIAG_CACHE_TTL_SECONDS
        ]
        for key in expired:
            _DIAG_CACHE.pop(key, None)
        cached = _DIAG_CACHE.get(base_url)
        if cached is not None and current_time - cached[0] < DIAG_CACHE_TTL_SECONDS:
            return list(cached[1])
    try:
        response = requests.get(f"{base_url}/diag/tunnel", timeout=DIAG_TIMEOUT_SECONDS)
        response.raise_for_status()
        addresses = parse_tunnel_diag(response.json())
    except Exception:
        addresses = []
    with _DIAG_LOCK:
        _DIAG_CACHE[base_url] = (current_time, addresses)
    return list(addresses)


def _location_payload(colo: str, catalog: dict[str, dict[str, str]]) -> dict[str, str]:
    location = catalog.get(colo)
    if location is None:
        return {"colo": colo}
    return dict(location)


def assess_route(
    client_colo: str | None,
    connector_colos: list[str],
    catalog: dict[str, dict[str, str]],
) -> str:
    if client_colo is None or not connector_colos:
        return "unknown"
    if all(colo == client_colo for colo in connector_colos):
        return "same_metro"
    client_country = (catalog.get(client_colo) or {}).get("country")
    connector_countries = [(catalog.get(colo) or {}).get("country") for colo in connector_colos]
    if not client_country or any(not country for country in connector_countries):
        return "unknown"
    if all(country == client_country for country in connector_countries):
        return "same_country"
    return "cross_country"


def network_path_snapshot(
    edge_locations: list[str],
    metrics_url: str | None,
    *,
    client_colo: str | None = None,
    client_access: str = "local",
) -> dict[str, Any]:
    catalog, locations_pending = location_catalog()
    connector_locations: list[dict[str, str]] = []
    connector_colos: list[str] = []
    seen_ids: set[str] = set()
    for raw_location in edge_locations[:MAX_EDGE_CONNECTIONS]:
        if not isinstance(raw_location, str):
            continue
        edge_id = raw_location.strip().lower()
        colo = colo_from_edge_location(edge_id)
        if colo is None or edge_id in seen_ids:
            continue
        seen_ids.add(edge_id)
        connector_colos.append(colo)
        connector_locations.append({"id": edge_id, **_location_payload(colo, catalog)})

    normalized_client = client_colo.upper() if isinstance(client_colo, str) else None
    return {
        "schema_version": 1,
        "provider": "Cloudflare",
        "asn": CLOUDFLARE_ASN,
        "sampled_at": _utc_timestamp(),
        "locations_pending": locations_pending,
        "client_access": "remote" if client_access == "remote" else "local",
        "client_ingress": _location_payload(normalized_client, catalog) if normalized_client else None,
        "connector": {
            "locations": connector_locations,
            "edge_ips": tunnel_edge_ips(metrics_url),
        },
        "route": {
            "assessment": assess_route(normalized_client, connector_colos, catalog),
        },
    }
