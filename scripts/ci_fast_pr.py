"""Fixed Linux PR portfolio plus the union of path-specific native/contract proofs."""
from __future__ import annotations

import re

from scripts.ci_docs_only import Scope, WINDOWS_CONTROL_IPC_PATHS
from scripts.ci_scope import DESKTOP_CONTRACTS, IOS_CONTRACTS

LINUX = frozenset(('ruff', 'unit-test-shards', 'ui-checks', 'ui-tests', 'show-router-integration',
                   'memory-insight-contract', 'npm-wrapper', 'migration-release-guard', 'build-linux-artifacts'))
PROOFS = {
    'identity': {'ui-browser-checks', 'windows-control-ipc'},
    'vault': {'ui-browser-checks', 'windows-control-ipc'},
    'install': {'install-upgrade-shards', 'windows-install-smoke-runners'},
    'migration': {'install-upgrade-shards'},  # Linux floor already exercises the previous published reader/writer.
    'engine-wire': {'model-hub-hosted-wire'},
    'desktop': {'desktop-shell'},
    'ios': {'ios-shell'},
    'shared': {'manifest-consistency'},  # The Linux floor also builds the wheel and UIs.
}
EXTRA = frozenset().union(*PROOFS.values())
IDENTITY = re.compile(r'auth|sign.?in|log.?in|log.?out|sign.?out|admission|onboard|pairing|binding|passkey|credential|remote.?access|device.?(?:claim|trust)|access.?token', re.I)
SHARED = re.compile(r'(^|/)([^/]*\.lock|package-lock\.json|pnpm-lock\.yaml|requirements[^/]*\.txt|pyproject\.toml|package\.json|cargo\.toml|go\.mod|go\.sum|package\.resolved|tsconfig[^/]*\.json|vite\.config\.[^/]+|conftest\.py|\.pre-commit-config\.yaml|dockerfile[^/]*)$', re.I)


def path_groups(path: str) -> set[str]:
    p = path.lower()
    groups = set()
    if p == 'vibe/model_hub_runtime/cliproxyapi_manifest.json':
        groups.add('engine-wire')
    if p in {'scripts/desktop_release.py', 'scripts/check-restricted-entitlement-refusal.sh'}:
        groups.add('desktop')
    if p.startswith('packaging/iroh-apple/'):
        groups.add('ios')
    if IDENTITY.search(p) or p in WINDOWS_CONTROL_IPC_PATHS or p in {
        'core/control_ipc.py', 'core/private_fs.py', 'tests/test_control_ipc.py',
    }:
        groups.add('identity')
    if any(s in p for s in ('vault', 'sandbox')):
        groups.add('vault')
    if any(s in p for s in ('install', 'upgrade', 'bootstrap', 'distribution')) or p.startswith(('packaging/', 'npm/')):
        groups.add('install')
    if 'alembic' in p or 'migrat' in p or p == 'storage/compatibility.py':
        groups.add('migration')
    if path.startswith(DESKTOP_CONTRACTS) or any(s in p for s in ('updater', 'auto_update', 'signing', 'codesign', 'notariz')):
        groups.add('desktop')
    if path.startswith(IOS_CONTRACTS) or p.endswith('.swift'):
        groups.add('ios')
    if SHARED.search(p) or p.startswith(('.github/', 'patches/', 'scripts/ci_')) or 'release' in p or p in ('hatch_build.py', 'hatch_exact_peer.py'):
        groups.add('shared')
    return groups


def selected(scope: Scope) -> tuple[set[str], set[str]]:
    paths = scope.verified_paths or (scope.paths if scope.routable else ())
    if not paths:
        raise ValueError('No verified PR change range; cannot select sensitive proofs')
    groups = set().union(*(path_groups(p) for p in paths))
    return groups, set(LINUX).union(*(PROOFS[g] for g in groups))


def verify(gate: str, needs: dict) -> None:
    import json

    changes = needs['changes']
    if changes['result'] != 'success':
        raise ValueError('Classification did not succeed')
    outputs = changes['outputs']
    if outputs['portfolio'] not in ('true', 'false'):
        raise ValueError('Missing portfolio disposition')
    routed = json.loads(outputs['dispatch_jobs'])
    jobs = LINUX if gate == 'fast-gate' else EXTRA
    legacy = {'unit-tests', 'install-upgrade-regression', 'windows-install-smoke'} if gate == 'sensitive-proof' else set()
    if set(needs) != {'changes', *jobs, *legacy}:
        raise ValueError('Missing or unexpected gate dependencies')
    for job in legacy:
        expected = 'skipped' if outputs['portfolio'] == 'true' else 'success'
        if needs[job]['result'] != expected:
            raise ValueError(f'{job} must be {expected}')
    for job in sorted(jobs):
        expected = {'run': 'success', 'skip': 'skipped'}[routed[job]]
        if gate == 'fast-gate' and outputs['portfolio'] == 'true' and expected != 'success':
            raise ValueError(f'Linux floor producer {job} was not selected')
        if needs[job]['result'] != expected:
            raise ValueError(f"{job}: {needs[job]['result']} (expected {expected})")
