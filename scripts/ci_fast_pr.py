"""Fast PR checks. Only positively identified leaf inputs take the one-runner route."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.ci_docs_only import Scope
from scripts.ci_scope import presentation_only

# No directory-wide exemption for runtime, tests, or UI components: an unknown
# consumer (especially auth/IPC, signing, migration or installer) keeps real CI.
PY_CASES = {
    'core/citations.py': ('tests/test_citations.py', 'tests/test_citation_consumers.py'),
    'tests/test_citations.py': ('tests/test_citations.py',),
    'tests/test_citation_consumers.py': ('tests/test_citation_consumers.py',),
}
UI_CASES = {
    'ui/src/lib/citations.ts': 'src/lib/citations.test.ts',
    'ui/src/lib/citations.test.ts': 'src/lib/citations.test.ts',
    'ui/src/components/SidebarResizer.tsx': 'src/components/SidebarResizer.test.tsx',
    'ui/src/components/SidebarResizer.test.tsx': 'src/components/SidebarResizer.test.tsx',
}
# Command examples and repository/installer guidance are executable inputs.
DOC_CONTRACTS = {'docs/CLI.md', 'docs/CLI_ZH.md', 'docs/COMMANDS.md', 'docs/COMMANDS_ZH.md', 'AGENTS.md', 'docs/testing/ios-direct-interop.md', '.github/PULL_REQUEST_TEMPLATE.md'}


def ordinary(scope: Scope) -> bool:
    return bool(scope.fast_eligible and scope.routable and scope.paths) and all(
        p in PY_CASES or p in UI_CASES or (presentation_only(p) and p not in DOC_CONTRACTS)
        for p in scope.paths
    )


def run(paths: list[str]) -> None:
    subprocess.run(['ruff', 'check', '.'], check=True)
    cases = {'tests/test_repository_guidance.py', 'tests/test_ci_unit_test_shards.py'}
    for path in paths:
        cases.update(PY_CASES.get(path, ()))
    subprocess.run([sys.executable, '-m', 'pytest', '-q', *sorted(cases)], check=True)
    if any(p.startswith('ui/') for p in paths):
        for cmd in (['npm', 'run', 'lint'], ['npm', 'run', 'typecheck:tests']):
            subprocess.run(cmd, cwd='ui', check=True)
        if any(p.startswith('ui/design/') for p in paths):
            subprocess.run(['npm', 'run', 'typecheck:design'], cwd='ui', check=True)
        ui_cases = {UI_CASES[p] for p in paths if p in UI_CASES} or {'src/lib/citations.test.ts'}
        subprocess.run(['npm', 'test', '--', *sorted(ui_cases)], cwd='ui', check=True)


if __name__ == '__main__':
    run(json.loads(Path(os.environ['RUNNER_TEMP'], 'fast-pr-paths.json').read_text()))
