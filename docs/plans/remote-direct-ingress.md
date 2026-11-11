# Remote direct paths: strict ingress contract

Status: **Phase 0 component.** `vibe/direct_ingress.py` and its wire tests exist; nothing starts the ingress yet, so a released instance behaves exactly as before. Phase 1 adds the certificate lifecycle and the transport that feed it. Parent plan: [`remote-direct-paths.md`](remote-direct-paths.md), decision 6 and section 5.

## 1. What the ingress is for

A direct transport carries the browser's connection from the iOS App to the instance without Cloudflare. The Web UI server normally listens on loopback and treats a loopback client that names a loopback `Host` as the machine's owner. A naive bridge from the transport to that listener would hand the owner path to any device that can reach the transport: the client completes a legal `CONNECT` and TLS handshake for the instance hostname, then sends `Host: localhost`.

The ingress is the only door a direct connection may use. It makes three things true for every request it lets through:

1. the request names this instance's managed hostname at every layer (CONNECT authority, TLS SNI, HTTP `Host`);
2. the bytes go to one fixed listener in this process, whatever the client asked for;
3. the Web UI sees a remote request, with no client-supplied forwarding metadata, and applies the same session, role, CSRF, and Origin checks as on the tunnel.

It authenticates nobody. Reaching the ingress, on a LAN or anywhere else, grants nothing.

## 2. Path of one connection

```text
transport stream (LAN / IPv6 / relay)            untrusted bytes
  -> DirectIngress.handle_stream                 CONNECT for <hostname>:443 only
  -> loopback TLS listener (fixed, this process) SNI = <hostname> only, HTTP/1.1
  -> DirectIngressGuard                          Host = <hostname> only; metadata stripped
  -> Web UI application                          remote request; existing auth unchanged
```

`<hostname>` is the instance's managed hostname, the host of `remote_access.vibe_cloud.public_url`. Custom hostnames are not served by the ingress in this version.

## 3. Stream framing

Each transport stream carries one tunnelled TCP-like connection and starts with an HTTP/1.1 request head:

```text
CONNECT <hostname>:443 HTTP/1.1
Host: <hostname>:443

```

The instance answers `HTTP/1.1 200 Connection Established` and from then on relays bytes in both directions. The App writes this canonical head, whatever WebKit sent to its loopback listener, and reports the instance's answer back, so a refusal reaches WebKit as a failed proxy connection.

| Request | Answer |
| --- | --- |
| Any method but `CONNECT` | `405`, stream closed |
| Authority or `Host` other than `<hostname>:443` (another host, another port, no port, userinfo, an IP literal) | `403`, stream closed |
| HTTP/1.0, missing or repeated `Host`, a request body, a malformed or oversized head (4 KiB), bytes that are not HTTP | `400`, stream closed |
| Head not complete within 5 seconds | stream closed |
| Ingress stopped, or stream limit reached (256 in total, 64 per transport peer) | `503`, stream closed |

The instance never resolves or dials the requested authority. A refusal is sent before any TLS work.

Bytes the client sends right after the head (an early ClientHello) are relayed, not dropped. When the client finishes sending, responses still in flight are delivered before the tunnel ends; when the listener closes, the tunnel ends.

## 4. TLS listener

- Bound to `127.0.0.1` on a port the kernel picks at start; the relay connects to that address and no other.
- TLS 1.2 or later, ALPN `http/1.1` only. The Web UI server has no HTTP/2 authority handling, so HTTP/2 is not offered.
- A handshake whose SNI is missing or is not `<hostname>` fails with `unrecognized_name`. The certificate is selected per handshake, so `reload_certificate()` serves a renewed certificate to the next handshake without closing open connections.
- The private key file must be a regular file that only its owner can read (POSIX); otherwise the ingress refuses to start. This is the "same care as the tunnel credential" rule at the point of use; where the key is stored is Phase 1's certificate lifecycle.
- uvicorn's proxy-header handling is off on this listener. No forwarding header is trusted at any layer.

Another local process can connect to the listener. It gains nothing: it faces the same remote authentication as a public client, while the ordinary loopback listener already gives a local process more.

## 5. HTTP guard

`DirectIngressGuard` wraps the Web UI application on the TLS listener, for HTTP and WebSocket requests alike.

It refuses, without calling the application:

- a request with no `Host`, more than one, or one that is not `<hostname>` or `<hostname>:443` (HTTP `421`; a WebSocket handshake gets `403`);
- a request target that is not origin-form (absolute-form and `*` name an authority of their own);
- a request that did not arrive over TLS.

For the rest it:

- rewrites `Host` to the bare hostname, so the application sees one canonical value;
- removes `Forwarded`, every `X-Forwarded-*`, `X-Real-IP`, `X-Original-Forwarded-For`, `True-Client-IP`, `X-Client-IP`, `X-Cluster-Client-IP`, `CDN-Loop`, every `CF-*` header, and the test-only peer override. The ingress is the first hop, so each of these is a forgery;
- replaces the socket peer, which is always this process's own relay on loopback, with the host `direct-ingress`. This value is deliberately not an IP address. Every trust decision keyed on a loopback, private, setup-host, Docker, Tailscale, or trusted-proxy peer parses the peer as an address, so none can match it, including ones written later;
- sets the ASGI scope key `vibe_direct_ingress` to `{"peer": <transport peer label>}`. The label names the transport peer (for example its iroh endpoint ID). It is metadata for rate limiting and logs, never an authorization input.

The only trusted metadata the application receives is therefore: the scheme (from TLS itself), the canonical `Host`, the peer placeholder, and the peer label.

## 6. Classification in the Web UI

A direct-ingress request is never local. Three independent layers hold this; a test keeps one at a time and removes the other two:

1. the guard refuses any `Host` but the instance hostname, and a managed hostname always takes the remote path;
2. `_is_local_request`, `_is_loopback_origin_proxy_request`, and `_websocket_is_local_request` return false when the scope key is set;
3. the placeholder peer fails every loopback and private-peer check, for HTTP and WebSocket handlers.

With all three removed, `Host: localhost` through a valid CONNECT returns the owner's configuration; with any one present it does not.

Two further rules:

- **Hostname drift.** Outside every route, a direct-ingress request is refused (`421`; a WebSocket handshake gets `403`) unless its `Host` is still the instance's managed hostname and remote access is still enabled. The rule is an ASGI middleware, not a request hook, so it also covers WebSocket handlers and the routes exempt from host validation, such as `/api/session`, which would otherwise describe a local owner.
- **Rate limiting.** The unauthenticated sign-in budget is keyed `direct:<peer label>`, so one device cannot spend the budget of every direct client.

Everything else is unchanged and identical to the tunnel: recognized-host check, session cookie and current authorization, roles, CSRF double-submit, exact Origin, WebSocket Origin and authorization, SSE revalidation. Cookies set through the ingress are `Secure` because the listener is TLS.

## 7. What Phase 1 adds

- **Lifecycle.** Start the ingress when direct access is enabled and a valid certificate is present; stop it when either stops being true; call `reload_certificate()` after renewal. Until then the App uses Cloudflare.
- **Transport.** An adapter that implements `ByteStream` (`read`, `write`, `close_write`, `aclose`) over the transport's streams and calls `handle_stream(stream, peer=<endpoint id>)`.
- **Peer admission.** Close a transport connection whose peer is not an endpoint ID admitted through the authenticated rendezvous, before reading any stream. The ingress limits are a second line, not the first.
- **Diagnostics.** Endpoint state, stream counts, and refusal counters in the remote-access status, with no key material and no peer address.

## 8. Tests

`tests/test_direct_ingress.py` drives real listeners over real sockets: a TCP stand-in for the transport, the ingress TLS listener, and the ordinary loopback listener as the control. An in-process ASGI client cannot see uvicorn's own proxy-header handling or the socket peer.

| Invariant | Test |
| --- | --- |
| `Host: localhost` (and other loopback forms) inside a valid CONNECT and TLS session is refused; the same request on the ordinary listener is the owner | `test_local_host_inside_a_valid_connect_is_refused` |
| Each of the three layers alone keeps a loopback `Host` off the owner path; with none, the owner path is reached | `test_each_layer_alone_keeps_a_loopback_host_off_the_owner_path` |
| CONNECT is answered only for `<hostname>:443` | `test_connect_is_answered_only_for_this_instance_on_port_443`, `test_connect_that_never_completes_is_dropped` |
| TLS requires the instance SNI | `test_tls_handshake_fails_without_this_instances_sni` |
| Forwarding metadata is removed; only trusted metadata arrives | `test_guard_removes_forwarding_metadata_and_replaces_the_peer`, `test_tunnel_presents_trusted_metadata_only` |
| Forged forwarding headers do not change the answer (covers both proxy-trust layers) | `test_forged_forwarding_headers_do_not_change_the_answer` |
| Anonymous, signed-in, and forged-cookie requests get the tunnel's answers | `test_remote_authentication_matches_the_tunnel` |
| A loopback-only WebSocket route is closed to the ingress | `test_loopback_only_websocket_is_closed_to_the_ingress` |
| Hostname drift and disabled remote access, for HTTP and WebSocket routes | `test_ingress_stops_answering_once_its_hostname_is_not_the_instances`, `test_no_route_answers_for_a_stale_ingress_hostname` |
| WebSocket upgrade and unbuffered streaming pass through the tunnel | `test_tunnel_carries_websockets_and_unbuffered_streams` |
| Renewal, key permissions, limits, rate-limit key | the remaining tests in the file |

Not covered here and owned by Phase 1: the transport itself, behavior on a real network, and device tests with WebKit.
