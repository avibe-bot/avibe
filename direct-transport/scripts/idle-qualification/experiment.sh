#!/usr/bin/env bash
# Evidence-only scratch runner; no binary from here is uploaded or packaged.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && $(uname -s) == Linux ]] || {
  echo 'Disposable Linux Actions runner only.' >&2; exit 1;
}
root=$(cd "$(dirname "$0")/../.." && pwd)
work="$RUNNER_TEMP/idle-experiment"
mkdir -p "$work/evidence"
cp -R "$root" "$work/source"
cd "$work/source"
cargo test --locked --test transport --no-run --message-format=json > "$work/client-build.jsonl"
client=$(python3 - "$work/client-build.jsonl" <<'PY'
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

# The unmodified/default client is already frozen. These edits affect only the
# disposable server build, never the repository or a shipping artifact.
python3 - "$IDLE_MS" "$KEEP_ALIVE_MS" <<'PY'
from pathlib import Path
import sys
idle,keep=map(int,sys.argv[1:])
assert (idle,keep) in ((8000,2500),(4000,1250))
p=Path('src/main.rs'); s=p.read_text(); old='.datagram_receive_buffer_size(None)'
assert s.count(old)==1
s=s.replace(old, old+f'''
        .max_idle_timeout(Some(VarInt::from_u32({idle}).into()))
        .keep_alive_interval(Duration::from_millis({keep}))
        .default_path_keep_alive_interval(Duration::from_millis({keep}))''')
p.write_text(s)
PY
if [[ "$VARIANT" == nat0 ]]; then
  mkdir -p .cargo
  cargo vendor --locked "$work/vendor" > .cargo/config.toml
  vendor="$work/vendor/iroh"
  (cd "$vendor" && git apply --check "$root/experiments/iroh-1.4.1-allow-nat-zero.patch" && git apply "$root/experiments/iroh-1.4.1-allow-nat-zero.patch")
  python3 - "$vendor" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); p=root/'.cargo-checksum.json'
data=json.loads(p.read_text()); file='src/endpoint/quic.rs'
data['files'][file]=hashlib.sha256((root/file).read_bytes()).hexdigest(); p.write_text(json.dumps(data))
p=pathlib.Path('src/main.rs'); s=p.read_text(); old='.max_remote_nat_traversal_addresses(8)'
assert s.count(old)==1; p.write_text(s.replace(old,'.max_remote_nat_traversal_addresses(0)'))
PY
else
  [[ "$VARIANT" == stock ]]
fi
cargo build --locked --bin avibe-direct-transport
cp target/debug/avibe-direct-transport "$work/server"
python3 "$root/scripts/idle-qualification/run.py" \
  --sidecar "$work/server" --peer-exe "$work/stock-client-tests" \
  --out "$work/evidence/qualification" --variant "$VARIANT-$IDLE_MS-$KEEP_ALIVE_MS"
sha256sum --check "$work/evidence/stock-client.sha256"
