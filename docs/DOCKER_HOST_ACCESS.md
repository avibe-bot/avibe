# Docker: opening Avibe from the Docker host

For operators who run Avibe in a container they build from this repository's
`Dockerfile`. Avibe does not publish a container image, and this is not one of
the documented ways to install it.

## Why a declaration is needed

One position opens Avibe without signing in: a connection that reaches the Web
UI directly over loopback, on the computer that runs it. Once an Avibe is bound
to an account, that is the only one
(`docs/plans/bound-network-position-trust.md`).

Inside a container, loopback is the container's own. A browser on the Docker
host reaches a published port through the bridge, and nothing in that
connection tells it apart from another machine whose traffic Docker forwards the
same way. Avibe therefore does not infer it. The operator states it.

## The declaration

Publish the port on the host's loopback address only, and say so in the
container's environment:

```bash
docker run \
  -p 127.0.0.1:5123:5123 \
  -e VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS=1 \
  -e VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST=127.0.0.1 \
  ...
```

| Variable | Meaning |
| --- | --- |
| `VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS` | `1`, `true`, `yes` or `on` turns the declaration on. |
| `VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST` | The host address the port is published on. It must be a loopback address (`127.0.0.1`, `::1` or `localhost`); with any other value, or none, the declaration does nothing. |
| `VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS` | Optional. Further bridge addresses that host traffic arrives from, comma-separated. The container's default gateway is always included. |

A request then gets local Workbench access when all of these hold:

- both required variables are set as above;
- the connection comes from the container's default gateway, or from an address
  listed in `VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS`;
- the address in the browser is `localhost`, `127.0.0.1` or `[::1]`, on any
  port;
- the request carries no forwarding header (`Forwarded`, `X-Forwarded-*`,
  `X-Real-IP` and the like). A reverse proxy in front of the port does not
  qualify.

This holds before and after the Avibe is bound to an account, and whether
remote access is on or off.

## What a false declaration costs

Avibe cannot see how the port is published. If the variables are set and the
port is in fact published on another address (for example `-p 5123:5123`, which
listens on every interface of the host), a device that reaches that port through
the bridge and sends a loopback address as the host can open the Workbench
without signing in: read and change settings, run Agents, open a terminal.
Setting the variables is the operator's statement that this cannot happen. The
same applies to anything on the Docker host that forwards other machines'
traffic to the published loopback port.

## What it does not give

Ordinary Workbench access only. Operations that require the computer itself
are refused through the bridge with `403 direct_loopback_required`, for example
adding the desktop app to this Avibe. Run those inside the container, where
loopback is direct (`docker exec`).

Without the declaration, a bound Avibe in a container opens without signing in
only from inside the container. Every other device signs in at the Avibe's own
avibe.bot address.
