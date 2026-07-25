from __future__ import annotations

from urllib.parse import urlparse

from vibe import remote_access


def csrf_headers(client, base_url: str = "http://localhost") -> dict[str, str]:
    response = client.get("/api/csrf-token", base_url=base_url)
    assert response.status_code == 200
    token = response.get_json()["csrf_token"]
    hostname = urlparse(base_url).hostname or "localhost"
    client.set_cookie("vibe_csrf_token", token, domain=hostname)
    if hostname == "localhost":
        client.set_cookie("vibe_csrf_token", token, domain="testserver")
    return {
        "Origin": base_url,
        "X-Vibe-CSRF-Token": token,
    }


def remote_session_cookie(
    config,
    email: str,
    subject: str,
    *,
    role: str = "owner",
    access_source: str = "owner",
    organization_id: str | None = None,
    organization_member_id: str | None = None,
    organization_role: str | None = None,
    group_ids: list[str] | None = None,
) -> str:
    session_claims: dict[str, object] = {
        "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
        "vibe_instance_role": role,
        "vibe_instance_access_source": access_source,
    }
    if organization_id is not None:
        session_claims["vibe_organization_id"] = organization_id
    if organization_member_id is not None:
        session_claims["vibe_organization_member_id"] = organization_member_id
    if organization_role is not None:
        session_claims["vibe_organization_role"] = organization_role
    if group_ids is not None:
        session_claims["vibe_group_ids"] = group_ids
    return remote_access.make_session_cookie(
        config,
        email,
        subject,
        session_claims=session_claims,
    )
