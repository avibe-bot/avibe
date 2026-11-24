"""Explicit local install/pair/enable/serve lifecycle; no implicit service start."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
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
    from . import lifecycle
    from .host import Host

    # Serialize startup with CLI routing. The command handler never acquires
    # lifecycle.lock: its caller owns it until acknowledgement and process exit.
    with lifecycle.locked(pairing):
        lock = lifecycle.claim(pairing)
        if lock is None:
            raise ValueError("device_already_running")
        try:
            owner = lifecycle.register(lock)
        except BaseException:
            os.close(lock)
            raise

    signals = []
    runner = None
    control = None
    handlers = set()
    command = asyncio.get_running_loop().create_future()
    acknowledged = asyncio.get_running_loop().create_future()
    control_path = pairing.root / "control.sock"
    provision = None
    loop = asyncio.get_running_loop()
    try:
        snapshot = pairing.read()
        if not snapshot.get("enabled"):
            raise ValueError("device_disabled")
        generation = snapshot.get("generation", 0)
        install_signals = stopped is None
        stopped = stopped or asyncio.Event()
        for sig in ((signal.SIGINT, signal.SIGTERM) if install_signals else ()):
            loop.add_signal_handler(sig, stopped.set)
            signals.append(sig)
        if provision_file is not None:
            from .provision import ProvisionWindow

            provision = ProvisionWindow(pairing, provision_file, bind=bind, port=port, seconds=provision_seconds,
                                        generation=generation)
        async def handle_control(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                data = await asyncio.wait_for(reader.readline(), 2)
                request = json.loads(data)
                if (request.get("nonce") != owner["nonce"] or command.done()
                        or request.get("command") not in {"stop", "disable", "revoke"}):
                    return
                command.set_result(request["command"])
                ok = await asyncio.shield(acknowledged)
                writer.write(json.dumps({"ok": ok}).encode() + b"\n")
                await writer.drain()
            except (ValueError, OSError, asyncio.TimeoutError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                handlers.discard(task)

        control_path.unlink(missing_ok=True)
        control = await asyncio.start_unix_server(handle_control, path=str(control_path), limit=1024)
        os.chmod(control_path, 0o600)
        # A previous process may have lost a successful open response. Pairing
        # and diagnostics remain available if the host is temporarily absent.
        try:
            await asyncio.wait_for(Host().call("close_device", device_id=snapshot["device_id"]), 2)
        except (ValueError, asyncio.TimeoutError):
            pass
        runner = web.AppRunner(create_app(pairing, inactivity=inactivity, threshold=threshold, provision=provision,
                                          generation=generation),
                               access_log=None, shutdown_timeout=5)
        await runner.setup()
        await web.TCPSite(runner, bind, port).start()
        while pairing.active(generation) and not stopped.is_set() and not command.done():
            try:
                await asyncio.wait_for(stopped.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            if runner is not None:
                # Admission closes first; shutdown joins every connection and
                # its request writer tasks before a new generation is visible.
                await runner.cleanup()
            if command.done():
                lifecycle.commit(pairing, command.result())
                acknowledged.set_result(True)
        finally:
            if provision is not None:
                provision.close()
            if not acknowledged.done():
                acknowledged.set_result(False)
            if control is not None:
                control.close()
                await control.wait_closed()
            if handlers:
                await asyncio.gather(*tuple(handlers), return_exceptions=True)
            control_path.unlink(missing_ok=True)
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
            pairing.pair(args.device_id, name=args.name, provision_file=args.provision_file,
                         websocket_url=args.websocket_url)
        elif args.command in {"revoke", "enable", "disable", "stop"}:
            from .lifecycle import transition

            transition(pairing, args.command)
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
