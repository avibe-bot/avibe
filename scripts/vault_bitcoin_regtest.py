#!/usr/bin/env python3
"""Offline Bitcoin Core acceptance of actual avault signatures; public test keys only.

Runs its own no-network regtest node and kills only the recorded child PID. No real
wallet, normal HOME, credentials, live Avibe API or external blockchain is accessed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def tagged(tag: str, data: bytes) -> bytes:
    prefix = sha(tag.encode())
    return sha(prefix + prefix + data)


def compact(n: int) -> bytes:
    if n < 253:
        return bytes([n])
    if n <= 65535:
        return b'\xfd' + struct.pack('<H', n)
    return b'\xfe' + struct.pack('<I', n)


def varbytes(data: bytes) -> bytes:
    return compact(len(data)) + data


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--bitcoin-dir', required=True, type=Path)
    parser.add_argument('--avault', required=True, type=Path)
    parser.add_argument('--avibe-source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--avault-only', action='store_true', help='Bypass Avibe only to diagnose the Rust signer in isolation')
    parser.add_argument('--baseline', action='store_true', help='Demonstrate the released raw-signing mismatch without requiring the new scheme')
    args = parser.parse_args()
    args.bitcoin_dir = args.bitcoin_dir.resolve()
    args.avault = args.avault.resolve()
    args.avibe_source = args.avibe_source.resolve()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bridge = Path(__file__).with_name('vault_bitcoin_cli_bridge.py').resolve()
    for f in (args.bitcoin_dir/'bitcoind', args.bitcoin_dir/'bitcoin-cli', args.avault):
        if not f.is_file():
            raise SystemExit(f'Missing test executable: {f}')
    sys.path.insert(0, str(args.avibe_source.resolve()))
    from storage.vault_addresses import derive_addresses

    with tempfile.TemporaryDirectory(prefix='taproot-regtest-', dir=args.output.parent) as directory:
        root = Path(directory).resolve()
        home = root/'home'
        home.mkdir(mode=0o700)
        state = root/'bitcoin'
        state.mkdir(mode=0o700)
        store = root/'avault'
        store.mkdir(mode=0o700)
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SYSTEMROOT', 'WINDIR'}}
        env.update(HOME=str(home), AVIBE_HOME=str(root/'avibe'), AVAULT_HOME=str(store), AVAULT_STORE='file',
                   XDG_CONFIG_HOME=str(home/'config'), XDG_DATA_HOME=str(home/'data'),
                   XDG_CACHE_HOME=str(home/'cache'), TMPDIR=str(root/'tmp'))
        (root/'tmp').mkdir(mode=0o700)
        signer_label = 'avault' if args.avault_only else 'avibe_cli_avault'
        rpcport = free_port()
        common = [f'-datadir={state}', '-regtest', f'-rpcport={rpcport}']
        cli = [str(args.bitcoin_dir/'bitcoin-cli'), *common]
        results = []

        def rpc(method: str, *values):
            cmd = [*cli, method, *[json.dumps(v) if not isinstance(v, str) else v for v in values]]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
            if proc.returncode:
                raise RuntimeError(f'isolated Bitcoin RPC {method}: {proc.stderr.strip()}')
            try:
                return json.loads(proc.stdout)
            except ValueError:
                return proc.stdout.strip()

        def avault(command: list[str], data: bytes):
            proc = subprocess.run([str(args.avault), '--store', 'file', *command], input=data,
                                  capture_output=True, env=env, timeout=30)
            if proc.returncode:
                raise RuntimeError(f'isolated avault {command[0]}: {proc.stderr.decode().strip()}')
            return json.loads(proc.stdout)

        with (root/'bitcoind.log').open('wb') as log:
            node = subprocess.Popen([str(args.bitcoin_dir/'bitcoind'), *common, '-server', '-networkactive=0',
                                     '-listen=0', '-discover=0', '-dnsseed=0', '-connect=0', '-persistmempool=0',
                                     '-fallbackfee=0.0002'], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if node.poll() is not None:
                        raise RuntimeError('test-owned bitcoind exited before RPC ready')
                    try:
                        info = rpc('getblockchaininfo')
                        assert info['chain'] == 'regtest'
                        break
                    except RuntimeError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('test-owned Bitcoin RPC did not become ready')
                rpc('createwallet', 'isolated-test-wallet')
                sink = rpc('getnewaddress', 'sink', 'bech32')
                cases = []
                for key_number, address_kind in ((k, a) for k in (1, 6) for a in ('tr', 'rawtr', 'wpkh')):
                    # Publicly known synthetic scalars, intentionally not real wallet material.
                    key = key_number.to_bytes(32, 'big')
                    public = ec.derive_private_key(key_number, ec.SECP256K1()).public_key()
                    compressed = public.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
                    desc = rpc('getdescriptorinfo', f'{address_kind}({compressed.hex()})')['descriptor']
                    receive = rpc('deriveaddresses', desc)[0]
                    # Convert independently encoded mainnet listed address to its witness program
                    # using Core's decodescript, rather than trusting production point math twice.
                    addr_info = rpc('validateaddress', receive)
                    script = bytes.fromhex(addr_info['scriptPubKey'])
                    mainnet = derive_addresses(compressed.hex())['btc_taproot' if address_kind != 'wpkh' else 'btc_segwit']
                    alphabet = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
                    symbols = [alphabet.index(c) for c in mainnet.split('1', 1)[1]][:-6]
                    assert symbols[0] == (0 if address_kind == 'wpkh' else 1)
                    acc = bits = 0
                    decoded = bytearray()
                    for val in symbols[1:]:
                        acc = (acc << 5) | val
                        bits += 5
                        while bits >= 8:
                            bits -= 8
                            decoded.append((acc >> bits) & 255)
                    if address_kind != 'rawtr':
                        assert bytes(decoded) == script[2:], 'Listed address differs from Bitcoin Core output key'
                    block_hash = rpc('generatetoaddress', 1, receive)[0]
                    coinbase = rpc('getblock', block_hash, 2)['tx'][0]
                    output = next(v for v in coinbase['vout'] if v['scriptPubKey']['hex'] == script.hex())
                    cases.append((key_number, address_kind, key, compressed, script, coinbase['txid'], output['n'], int(output['value'] * 100_000_000)))
                rpc('generatetoaddress', 101, sink)
                for key_number, address_kind, key, compressed, script, txid, vout, amount in cases:
                    sequence = 0xfffffffd
                    raw = rpc('createrawtransaction', [{'txid': txid, 'vout': vout, 'sequence': sequence}], [{sink: (amount - 10000)/100_000_000}], 0)
                    tx = rpc('decoderawtransaction', raw)
                    output = tx['vout'][0]
                    out_script = bytes.fromhex(output['scriptPubKey']['hex'])
                    txout = struct.pack('<Q', amount - 10000) + varbytes(out_script)
                    outpoint = bytes.fromhex(txid)[::-1] + struct.pack('<I', vout)
                    version = struct.pack('<i', tx['version'])
                    locktime = struct.pack('<I', tx['locktime'])
                    if address_kind == 'wpkh':
                        script_code = b'\x76\xa9\x14' + script[2:] + b'\x88\xac'
                        sighash = sha(sha(version + sha(sha(outpoint)) + sha(sha(struct.pack('<I', sequence))) + outpoint + varbytes(script_code) + struct.pack('<Q', amount) + struct.pack('<I', sequence) + sha(sha(txout)) + locktime + struct.pack('<I', 1)))
                        schemes = [('ecdsa-secp256k1-der', True)]
                    else:
                        sighash = tagged('TapSighash', b'\x00' + b'\x00' + version + locktime + sha(outpoint) + sha(struct.pack('<Q', amount)) + sha(varbytes(script)) + sha(struct.pack('<I', sequence)) + sha(txout) + b'\x00' + struct.pack('<I', 0))
                        schemes = [('schnorr-secp256k1-bip340', address_kind == 'rawtr'), ('schnorr-secp256k1-bip341-keypath', address_kind == 'tr')]
                    if args.baseline:
                        schemes = [(scheme, expected) for scheme, expected in schemes if scheme != 'schnorr-secp256k1-bip341-keypath']
                    name = f'PUBLIC_TEST_KEY_{key_number}'
                    envelope = avault(['seal', '--name', name], key)
                    txin = outpoint + b'\x00' + struct.pack('<I', sequence)

                    def witness_tx(sig: bytes) -> str:
                        witness = b'\x02' + varbytes(sig + b'\x01') + varbytes(compressed) if address_kind == 'wpkh' else b'\x01' + varbytes(sig)
                        return (version + b'\x00\x01\x01' + txin + b'\x01' + txout + witness + locktime).hex()

                    for scheme, expected in schemes:
                        request = {'name': name, 'key_envelope': envelope, 'digest': sighash.hex(), 'scheme': scheme}
                        if not args.avault_only:
                            request['public_key'] = compressed.hex()
                            proc = subprocess.run([sys.executable, str(bridge), str(args.avibe_source), str(args.avault)], input=json.dumps(request).encode(), capture_output=True, env=env, timeout=60)
                            if proc.returncode:
                                raise RuntimeError(f'Avibe test CLI bridge: {proc.stderr.decode()}')
                            signed = json.loads(proc.stdout)
                        else:
                            signed = avault(['sign'], json.dumps(request).encode())
                        result = rpc('testmempoolaccept', [witness_tx(bytes.fromhex(signed['signature']))])[0]
                        assert result['allowed'] is expected, (key_number, scheme, result)
                        results.append({'key_parity': 'odd' if compressed[0] == 3 else 'even', 'address_kind': address_kind, 'signer': signer_label, 'scheme': scheme, 'allowed': result['allowed'], 'reject_reason': result.get('reject-reason'), 'reject_details': result.get('reject-details'), 'listed_address_matches_core': address_kind != 'rawtr'})
                        if expected:
                            wrong = bytearray.fromhex(signed['signature'])
                            wrong[0] ^= 1
                            tampered = rpc('testmempoolaccept', [witness_tx(bytes(wrong))])[0]
                            assert tampered['allowed'] is False
                            results.append({'key_parity': 'odd' if compressed[0] == 3 else 'even', 'address_kind': address_kind, 'signer': signer_label, 'scheme': scheme, 'tampered_signature_rejected': True})
                report = {'bitcoin_core': rpc('getnetworkinfo')['subversion'], 'baseline': args.baseline, 'network': 'isolated regtest (no peer networking)', 'broadcast': False, 'tests': results}
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(json.dumps(report, indent=2))
            finally:
                if node.poll() is None:
                    node.terminate()
                    try:
                        node.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        node.kill()
                        node.wait(timeout=10)
                assert node.poll() is not None


if __name__ == '__main__':
    main()
