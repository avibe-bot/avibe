"""Make space for a measured debug cache on the next natural master build."""
import json
import os
import re
import subprocess
from datetime import datetime

MASTER = 'refs/heads/master'
PREFIX = 'desktop-pr-debug-v1-'
PROFILE = rf'{PREFIX}(macOS-ARM64|Windows-X64)-[0-9a-f]{{64}}'
LIMIT = 550_000_000


def victim(rows, profile, size, started):
    """Return the oldest necessary, older, eligible generation, or no victim."""
    match = re.fullmatch(PROFILE, profile)
    if not match or not 0 < size <= LIMIT:
        return None
    platform = PREFIX + match[1] + '-'
    debug = [r for r in rows if r['ref'] == MASTER and r['key'].startswith(PREFIX)]
    own = sum(r['size_in_bytes'] for r in debug if r['key'].startswith(platform))
    other = sum(r['size_in_bytes'] for r in debug if not r['key'].startswith(platform))
    total = sum(r['size_in_bytes'] for r in rows)
    needed = max(own + size - LIMIT, total + size + max(0, LIMIT - other) + 800_000_000 - 10_000_000_000)
    if needed <= 0:
        return None
    candidates = []
    for r in debug:
        key = r['key']
        if not key.startswith(platform) or not re.fullmatch(PROFILE + r'-[0-9]+-[0-9]+', key):
            continue
        if datetime.fromisoformat(r['created_at']) >= started:
            # A new producer for our own profile already won; do not make another slot.
            if key.startswith(profile + '-'):
                return None
            continue
        if type(r['id']) is int and r['id'] > 0 and r['version'] and r['size_in_bytes'] > 0:
            candidates.append(r)
    if sum(r['size_in_bytes'] for r in candidates) < needed:
        return None  # Cannot fit even after retirement. Delete nothing.
    return min(candidates, key=lambda r: (datetime.fromisoformat(r['created_at']), r['id']))


def reconcile(profile, size, started, inventory, remove):
    while row := victim(inventory(), profile, size, started):
        again = victim(inventory(), profile, size, started)
        if not again or any(row[k] != again[k] for k in ('id', 'ref', 'key', 'version')):
            return
        if not remove(row['id']):
            return
        # If the service has not applied the deletion yet, stop rather than spinning.
        if any(r['id'] == row['id'] for r in inventory()):
            return


def main():
    repo = 'repos/' + os.environ['GITHUB_REPOSITORY']
    run, attempt = os.environ['SOURCE_RUN'], os.environ['SOURCE_ATTEMPT']
    def list_rows(path, field):
        pages = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp', repo + path]))
        return [row for page in pages for row in page[field]]

    def remove(cache_id):
        # A delayed completion must not evict entries for a newer master.
        tip = subprocess.check_output(['gh', 'api', repo + '/git/ref/heads/master', '--jq', '.object.sha'], text=True).strip()
        if tip != os.environ['SOURCE_SHA']:
            return False
        result = subprocess.run(['gh', 'api', '--include', '--method', 'DELETE', f'{repo}/actions/caches/{cache_id}'], capture_output=True)
        if result.returncode and re.match(rb'HTTP/\S+ 404(?:\s|$)', result.stdout):
            return False
        result.check_returncode()
        return True

    started = datetime.fromisoformat(os.environ['SOURCE_STARTED'])
    for item in list_rows(f'/actions/runs/{run}/artifacts?per_page=100', 'artifacts'):
        match = re.fullmatch(rf'debug-room-{int(attempt)}-({PROFILE})-([1-9][0-9]{{0,8}})', item['name'])
        if match and not item['expired']:
            reconcile(match[1], int(match[3]), started, lambda: list_rows('/actions/caches?per_page=100', 'actions_caches'), remove)


if __name__ == '__main__':
    main()
