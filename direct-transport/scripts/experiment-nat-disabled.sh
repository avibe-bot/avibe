#!/usr/bin/env bash
# Disposable Actions experiment only. No binary this script builds is uploaded.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && $(uname -s) == Linux ]] || {
  echo 'This experiment runs only on a disposable Linux Actions runner.' >&2; exit 1;
}
root=$(cd "$(dirname "$0")/.." && pwd)
work="$RUNNER_TEMP/nat-disabled-experiment"
mkdir -p "$work/evidence"
cp -R "$root" "$work/server"
cd "$work/server"
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
# Build a genuinely stock client before any vendoring/patching. Never rebuild
# this executable later: its checked hash proves independent interoperability.
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
cp target/debug/avibe-direct-transport "$work/stock-server"
sha256sum "$work/stock-client-tests" > "$work/evidence/stock-client.sha256"
isolated_run env AVDT_TEST_BINARY="$work/stock-server" \
  "$work/stock-client-tests" --test-threads=1 --nocapture 2>&1 | tee "$work/evidence/stock-tests.log"

mkdir -p .cargo
cargo vendor --locked "$work/vendor" > .cargo/config.toml
vendor="$work/vendor/iroh"
[[ -d "$vendor" ]]
(cd "$vendor" && patch --fuzz=0 -p1 < "$root/experiments/iroh-1.4.1-allow-nat-zero.patch")
python3 - "$vendor" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); path=root/'.cargo-checksum.json'
data=json.loads(path.read_text()); file='src/endpoint/quic.rs'
data['files'][file]=hashlib.sha256((root/file).read_bytes()).hexdigest()
path.write_text(json.dumps(data))
path=pathlib.Path('src/main.rs'); source=path.read_text()
old='.max_remote_nat_traversal_addresses(8)'; assert source.count(old)==1
path.write_text(source.replace(old,'.max_remote_nat_traversal_addresses(0)'))
PY
export CARGO_TARGET_DIR="$work/patched-target"
cargo build --locked --bin avibe-direct-transport
cp "$CARGO_TARGET_DIR/debug/avibe-direct-transport" "$work/nat0-server"
isolated_run env AVDT_TEST_BINARY="$work/nat0-server" AVDT_EXPECT_NAT_DISABLED=1 \
  "$work/stock-client-tests" --test-threads=1 --nocapture 2>&1 | tee "$work/evidence/patched-server-stock-client.log"
for test in multiple_candidates_with_one_dead active_path_blackhole_timeout; do
  isolated_run env AVDT_TEST_BINARY="$work/nat0-server" AVDT_EXPECT_NAT_DISABLED=1 \
    "$work/stock-client-tests" --exact "$test" --ignored --test-threads=1 --nocapture \
    2>&1 | tee "$work/evidence/$test.log"
  grep -q 'test result: ok. 1 passed; 0 failed' "$work/evidence/$test.log"
done
sha256sum --check "$work/evidence/stock-client.sha256"
cp "$root/experiments/iroh-1.4.1-allow-nat-zero.patch" "$work/evidence/"

# Unit-level encrypted hostile-frame proof, with NAT-enabled positive control.
# This is a separate source copy: no adversarial test hook reaches a server.
cp -R "$work/vendor/noq-proto" "$work/noq-proto-hostile"
(
  cd "$work/noq-proto-hostile"
  patch --fuzz=0 -p1 < "$root/experiments/noq-proto-1.4.0-hostile-reach-out.patch"
  CARGO_TARGET_DIR="$work/noq-test-target" cargo test --locked --lib -- \
    --exact --nocapture tests::reach_out_not_negotiated::hostile_reach_out_is_rejected_without_probe
) 2>&1 | tee "$work/evidence/hostile-reach-out.log"
grep -q 'test result: ok. 1 passed; 0 failed' "$work/evidence/hostile-reach-out.log"

# Instrumented client only: expose existing noq APIs in a disposable iroh copy.
# This is NOT the stock executable used for interoperability above. Its only
# probe target is a loopback UDP listener it owns, in a route-less namespace.
python3 - "$vendor" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); file='src/endpoint/connection.rs'; path=root/file
path.write_text(path.read_text()+'''
// CI-only instrumentation; never shipped or used by the stock client test.
impl Connection {
    #[doc(hidden)]
    pub fn avdt_experiment_request_probe(&self, target: std::net::SocketAddr) -> bool {
        assert!(target.ip().is_loopback());
        let Ok(addresses) = self.inner.get_local_nat_traversal_addresses() else { return false; };
        for address in addresses { let _ = self.inner.remove_nat_traversal_address(address); }
        if self.inner.add_nat_traversal_address(target).is_err() { return false; }
        self.inner.initiate_nat_traversal_round().is_ok()
    }
    #[doc(hidden)]
    pub fn avdt_experiment_remote_address_count(&self) -> usize {
        self.inner.get_remote_nat_traversal_addresses().map(|a| a.len()).unwrap_or(0)
    }
}
''')
checksum=root/'.cargo-checksum.json'; data=json.loads(checksum.read_text())
data['files'][file]=hashlib.sha256(path.read_bytes()).hexdigest(); checksum.write_text(json.dumps(data))
PY
mkdir -p examples
cp "$root/experiments/security_probe.rs" examples/security_probe.rs
cargo build --locked --example security_probe
probe_client="$CARGO_TARGET_DIR/debug/examples/security_probe"

# A deliberately delayed rejection is an experiment control, never a runtime
# setting. It compares scheduling-window effects without changing admission.
python3 - <<'PY'
from pathlib import Path
p=Path('src/admission.rs')
p.write_text(p.read_text()+'''
#[derive(Debug)]
pub struct DelayedAdmissions(pub Admissions);
impl EndpointHooks for DelayedAdmissions {
    async fn after_handshake(&self, connection: &Connection) -> AfterHandshakeOutcome {
        let result = self.0.after_handshake(connection).await;
        if matches!(result, AfterHandshakeOutcome::Reject { .. }) {
            time::sleep(Duration::from_secs(1)).await;
        }
        result
    }
}
''')
p=Path('src/main.rs'); s=p.read_text()
assert s.count('.hooks(control.admissions.clone())')==1
s=s.replace('.hooks(control.admissions.clone())','.hooks(admission::DelayedAdmissions(control.admissions.clone()))')
s=s.replace('.max_remote_nat_traversal_addresses(0)','.max_remote_nat_traversal_addresses(8)')
p.write_text(s)
PY
cargo build --locked --bin avibe-direct-transport
cp "$CARGO_TARGET_DIR/debug/avibe-direct-transport" "$work/stock-delay-server"
python3 - <<'PY'
from pathlib import Path
p=Path('src/main.rs'); s=p.read_text(); assert s.count('.max_remote_nat_traversal_addresses(8)')==1
p.write_text(s.replace('.max_remote_nat_traversal_addresses(8)','.max_remote_nat_traversal_addresses(0)'))
PY
cargo build --locked --bin avibe-direct-transport
cp "$CARGO_TARGET_DIR/debug/avibe-direct-transport" "$work/nat0-delay-server"
for variant in stock stock-delay nat0 nat0-delay; do
  for trial in 1 2 3 4 5; do
    isolated_run env AVDT_ISOLATED_EXPERIMENT=1 "$probe_client" "$work/$variant-server" "$variant" \
      >> "$work/evidence/pre-admission-probes.jsonl"
  done
done
python3 - "$work/evidence/pre-admission-probes.jsonl" <<'PY'
import json,sys
rows=[json.loads(line) for line in open(sys.argv[1])]
for row in rows:
    if row['variant'].startswith('nat0'):
        assert row['probe_packets']==0 and row['received_add_address']==0, row
control=[row for row in rows if row['variant']=='stock-delay']
assert any(row['received_add_address']>0 for row in control), 'candidate positive control failed'
assert any(row['probe_packets']>0 for row in control), 'probe positive control failed'
for variant in ('stock','stock-delay','nat0','nat0-delay'):
    group=[r for r in rows if r['variant']==variant]
    print(variant, 'trials',len(group),'ADD_ADDRESS',sum(r['received_add_address'] for r in group),
          'probe_packets',sum(r['probe_packets'] for r in group),'probe_bytes',sum(r['probe_udp_bytes'] for r in group))
PY

# The same stock client and address-change/capture proof against the NAT0server.
bash "$root/scripts/network-qualification/run-network-qualification.sh" \
  --sidecar "$work/nat0-server" --peer-exe "$work/stock-client-tests" \
  --out "$work/evidence/network-nat0" --app-payload from-app --instance-payload from-instance
echo 'Experiment completed. Only evidence files may be uploaded; no binary is approved for shipping.'
