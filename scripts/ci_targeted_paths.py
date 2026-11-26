"""Proposed PR-only named proof boundaries. Unlisted verified paths use Linux CI.

Each key is a proof family, not a glob over all files for a platform. A new
Alembic revision is executable schema code (including a newly added revision).
"""
from __future__ import annotations

from pathlib import PurePosixPath

BOUNDARIES = {
    # ios-shell stages iroh, builds the mobile UI (including tsc -b), verifies
    # license notices and consumes XCTest fixtures outside the Xcode project.
    'ios-app': frozenset({
        '.github/workflows/ios-shell.yml', 'Dockerfile', 'scripts/cargo_notices.py',
        'assets/mascot/cloud-tuanzi-v2.png', 'vibe/message_types.json',
        'ui/package.json', 'ui/package-lock.json', 'ui/mobile.html', 'ui/agentation.d.ts',
        'ui/tsconfig.json', 'ui/tsconfig.app.json', 'ui/tsconfig.node.json',
        'ui/vite.config.ts', 'ui/vite.desktop.config.ts', 'ui/vite.mobile.config.ts',
    }),
    # The wire comparison and its exception label exist only on pull requests.
    'engine-wire': frozenset({'vibe/model_hub_runtime/cliproxyapi_manifest.json'}),
    'admission': frozenset({'vibe/binding_admission.py', 'vibe/personal_claims.py', 'core/agent_auth_service.py'}),
    'vault-policy': frozenset({'storage/vault_policy.py', 'storage/vault_service.py', 'storage/vault_protected.py', 'storage/vault_protected_hd.py', 'storage/vault_crypto.py', 'vibe/avault_authorization.py'}),
    'vault-window-macos': frozenset({'desktop/src-tauri/src/macos_vault_window.rs'}),
    'sign-context': frozenset({'storage/vault_signatures.py', 'storage/vault_signing_input.py', 'storage/vault_bitcoin.py', 'storage/vault_hd.py', 'storage/vault_solana.py'}),
    'sign-macos': frozenset({'desktop/scripts/macos_signing.py', 'desktop/scripts/check-restricted-entitlement-refusal.sh'}),
    'sign-windows': frozenset({'scripts/certum_signing.py', 'desktop/scripts/signpath_test_signing.py'}),
    'updater-ui': frozenset({'ui/src/components/update/versions.ts', 'ui/src/components/VersionBadge.tsx'}),
    'updater-native': frozenset({'desktop/src-tauri/src/updater.rs', 'desktop/src-tauri/src/updater/control.rs', 'desktop/src-tauri/src/updater/activity.rs', 'desktop/runtime-host/src/instance_client/instance_update.rs', 'desktop/runtime-host/src/instance_client/coordinator/local_upgrade.rs', 'desktop/runtime-host/src/instance_client/coordinator/updates.rs'}),
    'upgrade-runtime': frozenset({'vibe/upgrade.py', 'vibe/install_generations.py', 'vibe/upgrade_interruptions.py', 'core/update_checker.py'}),
    'install-posix': frozenset({'install.sh'}),
    'install-windows': frozenset({'install.ps1'}),
    'migration': frozenset({'storage/alembic/env.py', 'storage/compatibility.py'}),
}


def matched_proofs(path: str) -> set[str]:
    result = {name for name, files in BOUNDARIES.items() if path in files}
    if path.startswith(('ios/', 'ui/src/', 'ui/scripts/', 'packaging/iroh-apple/', 'vibe/data/',
                        'tests/fixtures/push/', 'tests/fixtures/live_activity/')):
        result.add('ios-app')
    p = PurePosixPath(path)
    if p.parent.as_posix() == 'storage/alembic/versions' and p.suffix == '.py':
        result.add('migration')
    return result
