from __future__ import annotations

from vibe import cloudflare_network


def _catalog():
    return {
        "SIN": {"colo": "SIN", "location": "Singapore, Singapore", "country": "Singapore"},
        "NRT": {"colo": "NRT", "location": "Tokyo, Japan", "country": "Japan"},
        "KIX": {"colo": "KIX", "location": "Osaka, Japan", "country": "Japan"},
    }


def test_ra_tq_025_parses_official_cloudflare_location_components() -> None:
    catalog = cloudflare_network.parse_location_components(
        {
            "components": [
                {"name": "Singapore, Singapore - (SIN)"},
                {"name": "Adelaide, SA, Australia - (ADL)"},
                {"name": "Cloudflare Sites and Services"},
                {"name": "invalid - (too-long)"},
            ]
        }
    )

    assert catalog == {
        "SIN": {"colo": "SIN", "location": "Singapore, Singapore", "country": "Singapore"},
        "ADL": {"colo": "ADL", "location": "Adelaide, SA, Australia", "country": "Australia"},
    }


def test_ra_tq_025_parses_cloudflare_node_and_ray_codes() -> None:
    assert cloudflare_network.colo_from_edge_location("sin09") == "SIN"
    assert cloudflare_network.colo_from_edge_location("sin") is None
    assert cloudflare_network.parse_cf_ray_colo("9f1234567890abcd-SIN") == "SIN"
    assert cloudflare_network.parse_cf_ray_colo("test-ray") is None


def test_ra_tq_025_tunnel_diag_keeps_only_bounded_public_edge_ips() -> None:
    payload = {
        "connections": [
            {"edgeAddress": "198.41.192.47"},
            {"edgeAddress": "198.41.192.47"},
            {"edgeAddress": "127.0.0.1"},
            {"edgeAddress": "2606:4700::1"},
            {"edgeAddress": "not-an-ip"},
        ]
    }

    assert cloudflare_network.parse_tunnel_diag(payload) == ["198.41.192.47", "2606:4700::1"]


def test_ra_tq_026_route_assessment_is_conservative() -> None:
    catalog = _catalog()

    assert cloudflare_network.assess_route("SIN", ["SIN", "SIN"], catalog) == "same_metro"
    assert cloudflare_network.assess_route("NRT", ["NRT", "KIX"], catalog) == "same_country"
    assert cloudflare_network.assess_route("SIN", ["SIN", "NRT"], catalog) == "cross_country"
    assert cloudflare_network.assess_route("SIN", ["NRT", "LAX"], catalog) == "cross_country"
    assert cloudflare_network.assess_route(None, ["SIN"], catalog) == "unknown"
    assert cloudflare_network.assess_route("SIN", ["SIN", "LAX"], catalog) == "unknown"


def test_ra_tq_025_network_path_combines_nodes_ips_and_location(monkeypatch) -> None:
    monkeypatch.setattr(cloudflare_network, "location_catalog", lambda: (_catalog(), False))
    monkeypatch.setattr(
        cloudflare_network,
        "tunnel_edge_ips",
        lambda metrics_url: ["198.41.192.47", "198.41.200.53"],
    )

    snapshot = cloudflare_network.network_path_snapshot(
        ["sin09", "sin12"],
        "http://127.0.0.1:60553",
        client_colo="SIN",
        client_access="remote",
    )

    assert snapshot["provider"] == "Cloudflare"
    assert snapshot["asn"] == 13335
    assert snapshot["client_ingress"]["location"] == "Singapore, Singapore"
    assert snapshot["client_access"] == "remote"
    assert snapshot["connector"]["locations"][0]["id"] == "sin09"
    assert snapshot["connector"]["edge_ips"] == ["198.41.192.47", "198.41.200.53"]
    assert snapshot["route"]["assessment"] == "same_metro"


def test_ra_tq_025_diag_rejects_non_loopback_metrics_url(monkeypatch) -> None:
    called = False

    def unexpected_get(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("request should not be made")

    monkeypatch.setattr(cloudflare_network.requests, "get", unexpected_get)

    assert cloudflare_network.tunnel_edge_ips("http://203.0.113.10:9090") == []
    assert called is False
