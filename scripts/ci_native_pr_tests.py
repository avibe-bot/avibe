"""Run the fixed PR XCTest contracts plus whole classes from touched Swift test files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from scripts.ci_docs_only import classify

SCHEMES = {'AvibeTests': 'Avibe', 'AvibeSheetFixtureTests': 'AvibeSheetFixture'}
CONTRACTS = {
    'AvibeTests/NativeBadgeTests/testBadgeSetTakesExactlyOneCountNumber',
    'AvibeTests/NavigationPolicyTests/testConfiguredOriginRequiresHTTPSAndCanonicalizesToOrigin',
    'AvibeTests/SystemScheduleTests/testSharedContractVectorsCoverSwiftParsersAndEveryTerminalReceipt',
}


def declarations(source: Path) -> tuple[set[str], bool]:
    # Use Swift's parser, not the filename or a regex over comments/string literals.
    tree = subprocess.check_output(['xcrun', 'swiftc', '-frontend', '-dump-parse', str(source)], text=True)
    return parse_declarations(tree, source.read_text())


def parse_declarations(tree: str, source: str = '') -> tuple[set[str], bool]:
    classes = set()
    broader = False
    owner = None
    lines = source.splitlines()
    for line in tree.splitlines():
        indent = len(line) - len(line.lstrip())
        if owner and indent <= owner[0]:
            owner = None
        match = re.match(r'^\s*\((class_decl|extension_decl)\b', line)
        if match:
            name = re.search(r'\]\s+(?:unbound\s+)?"(\w+)"', line)
            if indent != 2 or not name:
                broader = True
                continue
            cls = name[1]
            owner = (indent, cls)
            loc = re.search(r' range=\[.*?:(\d+):\d+ - ', line)
            declaration = lines[int(loc[1])-1] if loc and int(loc[1]) <= len(lines) else ''
            if cls.endswith('Tests') or re.search(r'\bclass\s+' + re.escape(cls) + r'\s*:\s*(?:XCTest\.)?XCTestCase\b', declaration):
                classes.add(cls)
            elif match[1] == 'class_decl' and 'inherits=' in line and not re.search(r':\s*(?:@unchecked\s+)?(?:Sendable|NSObject)\s*\{', declaration):
                # An unresolved superclass can contribute inherited test methods.
                broader = True
        elif owner and indent == owner[0] + 2 and re.match(r'^\s*\(func_decl\b', line) and re.search(r'\]\s+"test\w*\(\)"', line):
            classes.add(owner[1])
    return classes, broader or not classes


def plan(paths, root: Path, inspect=declarations):
    if not paths:
        raise ValueError('No verified PR paths')
    required = set(CONTRACTS)
    whole_targets = set()
    for name in paths:
        path = Path(name)
        if len(path.parts) < 3 or path.parts[0] != 'ios' or path.parts[1] not in SCHEMES:
            continue
        source = root / path
        if not source.exists():  # Deleted; the surviving side of a rename is a separate verified path.
            continue
        target = path.parts[1]
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Not a regular test input: {path}')
        classes, broader = inspect(source) if source.suffix == '.swift' else (set(), True)
        required.update(f'{target}/{cls}' for cls in classes)
        if broader:
            whole_targets.add(target)
    selectors = required | whole_targets
    return required, sorted(s for s in selectors if not any(s.startswith(parent + '/') for parent in selectors))


def verify(target, required, passed, summary):
    expected = {s for s in required if s.startswith(target + '/')}
    count = summary.get('totalTestCount')
    if not isinstance(count, int) or count <= 0 or summary.get('passedTests') != count:
        raise ValueError(f'{target}: expected nonempty passing XCTest results: {summary}')
    missing = sorted(s for s in expected if s not in passed and not any(p.startswith(s + '/') for p in passed))
    if missing:
        raise ValueError(f'{target}: missing passing XCTest evidence for {missing}')


def main():
    env = os.environ
    scope = classify(env['GITHUB_EVENT_NAME'], json.loads(Path(env['GITHUB_EVENT_PATH']).read_text()),
                     env['GITHUB_SHA'], Path.cwd())
    required, selectors = plan(scope.verified_paths, Path.cwd())
    for target, scheme in SCHEMES.items():
        selected = [s for s in selectors if s.split('/')[0] == target]
        if not selected:
            continue
        temp = Path(env['RUNNER_TEMP'])
        result = temp / f'{target}.xcresult'
        if target == 'AvibeSheetFixtureTests':
            subprocess.run(['xcrun', 'simctl', 'privacy', env['SIMULATOR_ID'], 'grant', 'microphone', 'bot.avibe.ios.sheet-fixture'], check=True)
        cmd = ['xcodebuild', '-project', 'ios/Avibe.xcodeproj', '-scheme', scheme, '-configuration', 'Debug',
               '-destination', 'platform=iOS Simulator,id=' + env['SIMULATOR_ID'], '-derivedDataPath', str(temp / 'AvibeDerivedData'),
               '-resultBundlePath', str(result), '-parallel-testing-enabled', 'NO',
               *['-only-testing:' + s for s in selected], 'CODE_SIGNING_ALLOWED=NO', 'test']
        print(f'{scheme}: {selected}', flush=True)
        passed = set()
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                print(line, end='', flush=True)
                match = re.match(r"^Test Case '-\[(\w+)\.(\w+) (\w+)\]' passed \(", line)
                if match:
                    passed.add('/'.join(match.groups()))
            if proc.wait():
                raise SystemExit(f'{scheme}: xcodebuild failed')
        summary = json.loads(subprocess.check_output(['xcrun', 'xcresulttool', 'get', 'test-results', 'summary', '--path', result]))
        verify(target, required, passed, summary)


if __name__ == '__main__':
    main()
