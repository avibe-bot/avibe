# Remote direct paths: rendezvous contract

Status: **Phase 0 contract and endpoint.** The route and its state exist; no transport publishes an endpoint yet, so every instance answers `unavailable` and clients stay on Cloudflare. Phase 1 adds the transport. Parent plan: [`remote-direct-paths.md`](remote-direct-paths.md), decisions 3 and 8 and section 4.

## 1. Purpose

Before the iOS App can dial an instance directly, it must learn the instance's transport endpoint ID and the addresses it can be reached on, from a source it already trusts. That source is the instance itself, over the existing authenticated origin `https://<instance>-app.avibe.bot` through Cloudflare Tunnel. The instance generates an ephemeral endpoint key at each service start and publishes only the public half here. avibe.bot never stores an instance's LAN addresses.

The answer is public endpoint information for one signed-in session. It is not a credential. A client that connects with it still passes the strict ingress ([`remote-direct-ingress.md`](remote-direct-ingress.md)) and the ordinary remote authentication for every request.

## 2. Request

```http
POST /api/direct/rendezvous
Origin: https://<instance hostname>
Cookie: <remote session>; vibe_csrf_token=<token>
X-Vibe-CSRF-Token: <token>
Content-Type: application/json

{"version": 1, "client_endpoint_id": "<64 lowercase hex characters>"}
```

- `client_endpoint_id` is the App's own iroh endpoint ID (its Ed25519 public key in iroh's canonical lowercase hex). The App generates a fresh endpoint key per launch and sends only the ID.
- The caller must hold a current remote session for this instance. Any instance role may ask (Viewer and above): the direct path is transport, not a permission.
- It is a `POST` because it records the client's admission (section 4), so the usual Origin and CSRF checks apply.

| Condition | Answer |
| --- | --- |
| No session, or an invalid one | `401 remote_access_login_required` (the existing gate) |
| Access revoked | `403 remote_access_revoked` (the existing gate) |
| A local or setup-host request with no remote session | `403 remote_session_required` |
| Missing or wrong Origin or CSRF token | `403` (the existing gate) |
| Wrong `version`, or `client_endpoint_id` not 64 lowercase hex characters | `400 invalid_request` |

## 3. Response

Always `200` with `Cache-Control: no-store` once the request is valid.

### Ready

```json
{
  "version": 1,
  "status": "ready",
  "instance_id": "inst_…",
  "hostname": "alex-app.avibe.bot",
  "generation": "5f1c9a0b2d3e4f60:3",
  "issued_at": 1800000000,
  "expires_at": 1800000300,
  "alpn": "avibe-direct/1",
  "endpoint": {
    "id": "<64 lowercase hex characters>",
    "addresses": [
      {"kind": "lan", "address": "192.168.1.20:51234"},
      {"kind": "ipv6", "address": "[240e:b86:551:7700::1]:51234"}
    ],
    "relay": null
  },
  "client": {"endpoint_id": "<the request's client_endpoint_id>", "admitted_until": 1800000300}
}
```

| Field | Meaning |
| --- | --- |
| `instance_id`, `hostname` | The instance that answered and the one hostname the direct path serves. |
| `generation` | Opaque. Changes when the instance endpoint key or its address set changes, and never repeats across service restarts. |
| `issued_at`, `expires_at` | Unix seconds. The answer is valid for 300 seconds. |
| `alpn` | The application protocol the client must offer when it dials. |
| `endpoint.id` | The instance's endpoint ID. The client dials this identity and no other. |
| `endpoint.addresses` | Socket addresses to try. `lan` is a private IPv4 or unique-local IPv6 address, reachable from the same network. `ipv6` is a global IPv6 address. Order carries no preference; iroh chooses the path. |
| `endpoint.relay` | `null` until Phase 2, then the relay URL and a short-lived capability. |
| `client` | The admission recorded for the caller's endpoint. |

Loopback, link-local, unspecified, multicast, shared (CGNAT), documentation, IPv4-mapped, and public IPv4 addresses are never listed: nothing in Phase 1 can make a path to them work. At most eight addresses are listed.

### Unavailable

```json
{
  "version": 1,
  "status": "unavailable",
  "reason": "not_running",
  "instance_id": "inst_…",
  "hostname": "alex-app.avibe.bot",
  "retry_after_seconds": 60
}
```

| `reason` | Meaning |
| --- | --- |
| `not_running` | No transport endpoint. Every instance answers this until Phase 1. |
| `no_reachable_address` | The endpoint exists but has no address a client may be told about. |
| `hostname_not_direct` | The session is on a custom hostname. The direct path serves only the managed hostname. |
| other values | Reserved for Phase 1 (for example a disabled setting or a missing certificate). A client treats every unknown reason the same way. |

The client keeps using Cloudflare and may ask again after `retry_after_seconds`.

## 4. Admission

A ready answer admits `client_endpoint_id` to the transport until `admitted_until`. Phase 1's transport closes a connection whose peer endpoint ID is not admitted, before reading any stream. So a device that has never signed in cannot reach even the ingress.

- Admission is bound to the session's subject, held in memory only, and bounded: eight client endpoints per subject, 256 in total, oldest dropped first.
- It ends at `admitted_until`, when the instance endpoint key changes, and when the service restarts. A client that keeps using the direct path asks again before expiry.
- Admission is not authorization. A user whose access is revoked may keep an admitted transport peer for up to 300 seconds, and every request inside it is still refused by the remote session check.
- Same-origin page content, such as a Show Page a signed-in user opens, can call the endpoint with that user's session. It learns what that user may learn and can admit an endpoint ID of its choosing; the admitted peer still meets the ingress and the session check.

## 5. What the client must do

1. Ask through Cloudflare first. The direct path does not exist until a ready answer arrives. Later refreshes may use either path; both reach the same instance behind the same public certificate.
2. Make the request from native code, over the account's own same-origin transport, and use only the answer that request returned. Do not accept an answer handed over by page content: Show Pages are same-origin with the Workbench and can reach the native bridge, so an Agent-authored page could supply a forged one.
3. Accept a ready answer only if `version` is one it supports, `instance_id` and `hostname` match the instance the user selected, and `expires_at` is in the future.
4. Probe only addresses it can check for itself: private IPv4 or unique-local IPv6 for `lan`, global unicast IPv6 for `ipv6`, at most eight per answer. The App holds the user's Local Network grant; an answer must not be able to point it at anything else.
5. Dial `endpoint.id` with `alpn` at those addresses, using no public or multicast discovery. Reject a peer with any other identity.
6. Tag every probe and transport decision with the account, instance, and `generation` it belongs to. Drop state tagged with a generation the latest answer no longer carries, and never let a late result for an old account, instance, or generation install a route.
7. Ask again on expiry, on a network change, and when the direct path fails; fall back to Cloudflare at once in the meantime.
8. Ignore fields and address kinds it does not know.
9. Never treat the answer, an address in it, or a successful transport handshake as a login or as proof of anything about the user.

## 6. What the answer never contains

No endpoint private key, TLS private key, tunnel token, instance secret, session secret, cookie, or CSRF token. The transport hands the rendezvous an endpoint ID and bound socket addresses and nothing else, so there is no secret in reach to leak. Addresses are returned to signed-in users of this instance only and are not written to logs.

## 7. Phase 1 and Phase 2

- **Phase 1** publishes the endpoint (`DirectRendezvous.publish`) when the transport starts and whenever its addresses change, withdraws it with a reason when the transport stops, and enforces admission (`is_admitted`).
- **Phase 2** fills `endpoint.relay` with the relay URL and a short-lived, instance-scoped capability, and may add address kinds. Both are additive under `version: 1`.

## 8. Tests

`tests/test_direct_rendezvous.py`:

| Invariant | Test |
| --- | --- |
| Only LAN and global IPv6 socket addresses become candidates | `test_only_lan_and_global_ipv6_socket_addresses_are_candidates` |
| The ready answer's exact shape; the client is admitted | `test_offer_describes_the_endpoint_and_admits_the_client`, `test_signed_in_viewer_gets_the_endpoint_and_is_admitted` |
| `generation` changes exactly when the endpoint or its addresses change; a new endpoint key ends admissions | `test_generation_changes_exactly_when_the_endpoint_or_its_addresses_change` |
| Unavailable answers and their reasons | `test_without_a_dialable_endpoint_the_client_is_told_to_stay_on_the_tunnel`, `test_before_a_transport_exists_the_answer_is_unavailable`, `test_session_on_a_custom_hostname_stays_on_the_tunnel` |
| Admission expires and is bounded | `test_admission_expires_and_is_bounded_per_subject` |
| Anonymous and local callers learn nothing; CSRF is required; malformed requests are refused | `test_anonymous_and_local_callers_learn_nothing`, `test_request_without_the_csrf_token_is_refused`, `test_malformed_request_is_refused` |
| Any signed-in role may ask | `test_any_signed_in_remote_role_may_ask` and the route inventory in `tests/test_member_management_parity.py` |
