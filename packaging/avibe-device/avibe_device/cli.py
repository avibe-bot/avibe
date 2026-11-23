"""Explicit local install/pair/enable/serve lifecycle; no implicit service start."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import signal
from pathlib import Path

from .pairing import Pairing


async def serve(pairing, *, bind, port, inactivity, threshold, stopped=None,
                provision_file=None, provision_seconds=120):
    """One scope owns listeners and sockets; stopping preserves pairing."""
    from aiohttp import web
    from .server import create_app
    import fcntl

    # One optional process per pairing directory, even if a second launch names
    # another port. The kernel releases ownership on a process crash.
    lock = os.open(pairing.root / "serve.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)
        raise ValueError("device_already_running") from None

    signals = []
    runner = None
    provision = None
    loop = asyncio.get_running_loop()
    try:
        install_signals = stopped is None
        stopped = stopped or asyncio.Event()
        for sig in ((signal.SIGINT, signal.SIGTERM) if install_signals else ()):
            loop.add_signal_handler(sig, stopped.set)
            signals.append(sig)
        if provision_file is not None:
            from .provision import ProvisionWindow

            provision = ProvisionWindow(pairing, provision_file, bind=bind, port=port, seconds=provision_seconds)
        runner = web.AppRunner(create_app(pairing, inactivity=inactivity, threshold=threshold, provision=provision),
                               access_log=None, shutdown_timeout=5)
        await runner.setup()
        await web.TCPSite(runner, bind, port).start()
        while pairing.status()["enabled"] and not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if provision is not None:
                provision.close()
            for sig in signals:
                loop.remove_signal_handler(sig)
            os.close(lock)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="avibe-device")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".avibe-device")
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--device-id", required=True)
    pair.add_argument("--websocket-url", required=True)
    pair.add_argument("--provision-file", type=Path, required=True)
    pair.add_argument("--name", default="Vibey")
    for name in ("revoke", "enable", "disable", "stop", "status", "doctor"):
        commands.add_parser(name)
    history_parser = commands.add_parser("history")
    history_parser.add_argument("--conversation-id")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--bind", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--inactivity", type=float, default=20)
    serve_parser.add_argument("--vad-threshold", type=int, default=600)
    serve_parser.add_argument("--provision-file", type=Path)
    serve_parser.add_argument("--provision-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    pairing = Pairing(args.state_dir)
    try:
        if args.command == "pair":
            from .provision import websocket_address

            websocket_address(args.websocket_url)
            # Refuse overwriting a provisioning artifact; tokens are never stdout/log output.
            fd = os.open(args.provision_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                token = pairing.pair(args.device_id, name=args.name)
                with os.fdopen(fd, "w") as output:
                    fd = -1
                    json.dump({"websocket": {"url": args.websocket_url, "token": token, "version": 2}}, output)
            except Exception:
                if fd >= 0:
                    os.close(fd)
                args.provision_file.unlink(missing_ok=True)
                raise
        elif args.command == "revoke":
            pairing.revoke()
        elif args.command in {"enable", "disable", "stop"}:
            pairing.enable(args.command == "enable")
        elif args.command == "history":
            from .host import Host

            print(json.dumps(asyncio.run(Host().call("history", conversation_id=args.conversation_id))))
            return 0
        elif args.command == "doctor":
            from .host import Host

            checks = {**pairing.status(), "codec_available": False, "host_available": False}
            try:
                from .codec import Codec

                Codec()
                checks["codec_available"] = True
            except (ImportError, OSError):
                pass
            try:
                checks.update(asyncio.run(Host().call("status")))
                checks["host_available"] = True
            except ValueError:
                pass
            print(json.dumps(checks))
            return 0
        elif args.command == "serve":
            address = ipaddress.ip_address(args.bind)
            if address.version != 4 or not (address.is_loopback or address.is_private) or address.is_unspecified:
                raise ValueError("explicit_lan_address_required")
            if not 0 < args.inactivity <= 25 or not 1 <= args.vad_threshold <= 32767:
                raise ValueError("invalid_capture_options")
            if not pairing.status()["enabled"]:
                raise ValueError("device_disabled")
            from .codec import Codec

            Codec()  # Fail dependency checks before opening a listener.
            asyncio.run(serve(pairing, bind=args.bind, port=args.port, inactivity=args.inactivity,
                              threshold=args.vad_threshold, provision_file=args.provision_file,
                              provision_seconds=args.provision_seconds))
            return 0
        print(json.dumps({"ok": True, **pairing.status()}))
        return 0
    except (ValueError, OSError, ImportError):
        print(json.dumps({"ok": False, "code": "device_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
