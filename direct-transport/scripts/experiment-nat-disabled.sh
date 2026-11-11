#!/usr/bin/env bash
# Disposable Actions experiment only. No files it builds are uploaded/published.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && $(uname -s) == Linux ]] || {
  echo 'This experiment runs only on a disposable Linux Actions runner.' >&2; exit 1;
}
root=$(cd "$(dirname "$0")/.." && pwd)
work="$RUNNER_TEMP/nat-disabled-experiment"
mkdir -p "$work/evidence"
cp -R "$root" "$work/server"
cd "$work/server"
# First build an unmodified client executable. It will not be rebuilt after the
# source replacement; its bytes/hash establish stock-client interoperability.
cargo test --locked --test transport --no-run --message-format=json > "$work/stock-build.jsonl"
client=$(python3 - "$work/stock-build.jsonl" <<'PY'
import json,sys
for line in open(sys.argv[1]):
    item=json.loads(line)
    if item.get('reason')=='compiler-artifact' and item.get('target',{}).get('name')=='transport' and item.get('executable'):
        print(item['executable'])
PY
)
[[ -x "$client" ]]
cp "$client" "$work/stock-client-tests"
sha256sum "$work/stock-client-tests" > "$work/evidence/stock-client.sha256"
isolated_run() {
  sudo unshare --net -- bash -c '
    set -euo pipefail
    ip link set lo up
    ip link add avdt-exp type dummy
    ip addr add 192.168.245.1/24 dev avdt-exp
    ip -6 addr add 2001:db8:245::1/64 dev avdt-exp nodad
    ip link set avdt-exp up
    exec "$@"
  ' _ "$@"
}
# Baseline includes every authoritative-admission and stream test.
isolated_run env AVDT_TEST_BINARY="$PWD/target/debug/avibe-direct-transport" \
  "$work/stock-client-tests" --test-threads=1 --nocapture 2>&1 | tee "$work/evidence/stock-tests.log"

mkdir -p .cargo
cargo vendor --locked "$work/vendor" > .cargo/config.toml
vendor="$work/vendor/iroh"
[[ -d "$vendor" ]]
(cd "$vendor" && patch --fuzz=0 -p1 < "$root/experiments/iroh-1.4.1-allow-nat-zero.patch")
# Cargo's source-replacement checksums must describe the explicit patched file.
python3 - "$vendor" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); path=root/'.cargo-checksum.json'
data=json.loads(path.read_text()); file='src/endpoint/quic.rs'
data['files'][file]=hashlib.sha256((root/file).read_bytes()).hexdigest()
path.write_text(json.dumps(data))
path=pathlib.Path('src/main.rs'); source=path.read_text()
old='.max_remote_nat_traversal_addresses(8)'
assert source.count(old)==1
path.write_text(source.replace(old,'.max_remote_nat_traversal_addresses(0)'))
PY
# Separate target dir prevents reuse of any stock test executable. Only the
# server is compiled with the patch; no patched file enters the package job.
export CARGO_TARGET_DIR="$work/patched-target"
cargo build --locked --bin avibe-direct-transport
export AVDT_TEST_BINARY="$CARGO_TARGET_DIR/debug/avibe-direct-transport"
export AVDT_EXPECT_NAT_DISABLED=1
isolated_run env AVDT_TEST_BINARY="$AVDT_TEST_BINARY" AVDT_EXPECT_NAT_DISABLED=1 \
  "$work/stock-client-tests" --test-threads=1 --nocapture 2>&1 | tee "$work/evidence/patched-server-stock-client.log"
isolated_run env AVDT_TEST_BINARY="$AVDT_TEST_BINARY" AVDT_EXPECT_NAT_DISABLED=1 \
  "$work/stock-client-tests" --exact multiple_candidates_with_one_dead --ignored --test-threads=1 --nocapture \
  2>&1 | tee "$work/evidence/candidate-failover.log"
sha256sum --check "$work/evidence/stock-client.sha256"
cp "$root/experiments/iroh-1.4.1-allow-nat-zero.patch" "$work/evidence/"
echo 'Interoperability and candidate experiment completed. This job ships no binary.'
