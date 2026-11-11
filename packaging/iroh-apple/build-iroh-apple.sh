#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <iroh-ffi-checkout> <output-directory>" >&2
  exit 2
fi

SOURCE_DIR=$1
OUTPUT_DIR=$2
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$SCRIPT_DIR/versions.env"

[[ -d "$SOURCE_DIR" ]] || { echo "iroh-ffi checkout does not exist: $SOURCE_DIR" >&2; exit 1; }
mkdir -p "$OUTPUT_DIR"

if ! git -C "$SOURCE_DIR" apply --check "$SCRIPT_DIR/iroh-ffi-no-private-apple-datapath.patch"; then
  echo "the pinned iroh-ffi checkout no longer matches the feature patch" >&2
  exit 1
fi
git -C "$SOURCE_DIR" apply "$SCRIPT_DIR/iroh-ffi-no-private-apple-datapath.patch"

EXPECTED_DEP='iroh = { version = "1.0.0", default-features = false, features = ["metrics", "tls-ring"] }'
grep -Fqx "$EXPECTED_DEP" <(grep '^iroh = ' "$SOURCE_DIR/Cargo.toml") || {
  echo "patched iroh dependency is not the expected no-private-datapath form" >&2
  exit 1
}

export IPHONEOS_DEPLOYMENT_TARGET="$IOS_DEPLOYMENT_TARGET"
export CARGO_TARGET_DIR="$OUTPUT_DIR/cargo-target"
TARGET_DIR="$CARGO_TARGET_DIR"

prepare_vendored_dependencies() {
  local vendor_dir="$OUTPUT_DIR/vendor"
  local config_dir="$SOURCE_DIR/.cargo"
  local irpc_iroh_manifest
  rm -rf "$vendor_dir"
  mkdir -p "$config_dir"
  echo '==> vendor locked dependencies for the transitive irpc-iroh feature patch'
  cargo +"$RUST_VERSION" vendor --locked --versioned-dirs "$vendor_dir" > "$config_dir/config.toml"

  irpc_iroh_manifest=$(find "$vendor_dir" -path '*/irpc-iroh-*/Cargo.toml' -print -quit)
  [[ -n "$irpc_iroh_manifest" ]] || {
    echo "could not find vendored irpc-iroh Cargo.toml" >&2
    exit 1
  }
  python3 - "$irpc_iroh_manifest" <<'PY'
from hashlib import sha256
from pathlib import Path
import json
import sys

manifest = Path(sys.argv[1])
text = manifest.read_text()
old = '[dependencies.iroh]\nversion = "1"\n'
new = '[dependencies.iroh]\nversion = "1"\ndefault-features = false\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one irpc-iroh iroh dependency block, found {text.count(old)}")
manifest.write_text(text.replace(old, new))

checksum_path = manifest.parent / '.cargo-checksum.json'
checksum = json.loads(checksum_path.read_text())
checksum.setdefault('files', {})['Cargo.toml'] = sha256(manifest.read_bytes()).hexdigest()
checksum_path.write_text(json.dumps(checksum, sort_keys=True, separators=(',', ':')) + '\n')
PY
  grep -A2 '^\[dependencies\.iroh\]' "$irpc_iroh_manifest" | grep -Fqx 'default-features = false' || {
    echo "irpc-iroh still enables iroh default features" >&2
    exit 1
  }
}

cd "$SOURCE_DIR"
prepare_vendored_dependencies

assert_feature_graph() {
  local target=$1
  local graph_file="$OUTPUT_DIR/feature-graph-${target}.txt"
  echo "==> cargo feature graph for $target"
  cargo +"$RUST_VERSION" tree --locked --target "$target" --edges features -p iroh-ffi \
    | tee "$graph_file"

  if grep -En 'iroh feature "fast-apple-datapath"|noq feature "fast-apple-datapath"|noq-udp feature "fast-apple-datapath"|iroh feature "portmapper"' "$graph_file"; then
    echo "forbidden iroh/noq feature reached the $target graph" >&2
    exit 1
  fi
  local_discovery_pattern='(^|[^[:alnum:]])(address[-_ ]?lookup|dns[-_ ]?sd|dnssd|local[-_ ]?net(work)?|mdns|m[-_ ]?dns|multicast|service[-_ ]?discovery|zeroconf)([^[:alnum:]]|$)'
  if grep -Ein "$local_discovery_pattern" "$graph_file"; then
    echo "forbidden multicast/mDNS/local-network discovery reached the $target graph" >&2
    exit 1
  fi
  grep -Fq 'iroh feature "metrics"' "$graph_file" || {
    echo "iroh metrics feature is missing from the $target graph" >&2
    exit 1
  }
  grep -Fq 'iroh feature "tls-ring"' "$graph_file" || {
    echo "iroh tls-ring feature is missing from the $target graph" >&2
    exit 1
  }
}

cd "$SOURCE_DIR"
for target in aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios; do
  assert_feature_graph "$target"
done

RESOLVED_IROH_FEATURES_JSON=$(python3 - "$OUTPUT_DIR"/feature-graph-*.txt <<'PY'
import json
import re
import sys
from pathlib import Path

features = set()
for name in sys.argv[1:]:
    for line in Path(name).read_text().splitlines():
        match = re.search(r'(?:^|\s)iroh feature "([^"]+)"', line)
        if match:
            features.add(match.group(1))
if not features:
    raise SystemExit("could not extract resolved iroh features from feature graphs")
print(json.dumps(sorted(features)))
PY
)

echo "resolved iroh features: $RESOLVED_IROH_FEATURES_JSON"

[[ -n "${IROH_IOS_RUNTIME_VERSION:-}" ]] || {
  echo "IROH_IOS_RUNTIME_VERSION must be set by CI before building the qualified artifact" >&2
  exit 1
}

# The runtime test is intentionally below the previous 17.5 packaging floor. Keep this
# guard here so a future CI edit cannot silently run the test on a newer runtime.
python3 - "$IROH_IOS_RUNTIME_VERSION" <<'PY'
import sys

parts = tuple(int(part) for part in sys.argv[1].split('.'))
if parts < (17, 0) or parts >= (17, 5):
    raise SystemExit(
        f"runtime {sys.argv[1]} is outside the required iOS 17.0..<17.5 range"
    )
PY

echo '==> host build for UniFFI metadata'
cargo +"$RUST_VERSION" build --locked --lib

echo '==> iOS device build'
cargo +"$RUST_VERSION" build --locked --release --target aarch64-apple-ios --lib
echo '==> arm64 iOS simulator build'
cargo +"$RUST_VERSION" build --locked --release --target aarch64-apple-ios-sim --lib
echo '==> x86_64 iOS simulator build'
cargo +"$RUST_VERSION" build --locked --release --target x86_64-apple-ios --lib

INCLUDE_DIR="$OUTPUT_DIR/include"
HEADERS_DIR="$OUTPUT_DIR/apple-xcframework-headers"
rm -rf "$INCLUDE_DIR" "$HEADERS_DIR"
mkdir -p "$INCLUDE_DIR" "$HEADERS_DIR"

echo '==> generate Swift and C bindings'
cargo +"$RUST_VERSION" run --locked --bin uniffi-bindgen -- \
  generate --language swift --out-dir "$INCLUDE_DIR" \
  --library "$TARGET_DIR/debug/libiroh_ffi.dylib" --config uniffi.toml

cp "$INCLUDE_DIR/iroh_ffiFFI.h" "$HEADERS_DIR/iroh_ffiFFI.h"
cat > "$HEADERS_DIR/Export.h" <<'EOF'
#include "iroh_ffiFFI.h"
EOF
cat > "$HEADERS_DIR/module.modulemap" <<'EOF'
module Iroh {
    umbrella header "Export.h"
    export *
    module * { export * }
}
EOF

SIMULATOR_LIB="$OUTPUT_DIR/libiroh_ffi-ios-simulator.a"
lipo -create \
  "$TARGET_DIR/aarch64-apple-ios-sim/release/libiroh_ffi.a" \
  "$TARGET_DIR/x86_64-apple-ios/release/libiroh_ffi.a" \
  -output "$SIMULATOR_LIB"

XCFRAMEWORK="$OUTPUT_DIR/Iroh.xcframework"
rm -rf "$XCFRAMEWORK"
echo '==> assemble iOS-only xcframework'
xcodebuild -create-xcframework \
  -library "$TARGET_DIR/aarch64-apple-ios/release/libiroh_ffi.a" \
  -headers "$HEADERS_DIR" \
  -library "$SIMULATOR_LIB" \
  -headers "$HEADERS_DIR" \
  -output "$XCFRAMEWORK"

# Package.swift normally keys local-source use on its macOS slice. For this
# gate the intentionally iOS-only xcframework is the local binary target, and
# the package's iOS platform declaration must match this qualification floor.
python3 - "$SOURCE_DIR/Package.swift" "$IOS_DEPLOYMENT_TARGET" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
target = sys.argv[2]
text = path.read_text()
old_binary = 'Iroh.xcframework/macos-arm64/libiroh_ffi.a'
new_binary = 'Iroh.xcframework/ios-arm64/libiroh_ffi.a'
if text.count(old_binary) != 1:
    raise SystemExit(f"expected one local xcframework probe, found {text.count(old_binary)}")
text = text.replace(old_binary, new_binary)
old_platform = '.iOS("17.5")'
new_platform = f'.iOS("{target}")'
if text.count(old_platform) != 1:
    raise SystemExit(f"expected one iOS package platform declaration, found {text.count(old_platform)}")
path.write_text(text.replace(old_platform, new_platform))
PY
rm -rf "$SOURCE_DIR/Iroh.xcframework"
cp -R "$XCFRAMEWORK" "$SOURCE_DIR/Iroh.xcframework"

SWIFT_BINDING="$INCLUDE_DIR/iroh_ffi.swift"
SWIFT_INTERFACE="$INCLUDE_DIR/IrohLib.swift"
sed 's/iroh_ffiFFI/Iroh/g' "$SWIFT_BINDING" > "$SWIFT_INTERFACE"
cp "$SWIFT_INTERFACE" "$SOURCE_DIR/IrohLib/Sources/IrohLib/IrohLib.swift"
cp "$SWIFT_INTERFACE" "$OUTPUT_DIR/IrohLib.swift"

build_swift_for() {
  local destination=$1
  local sdk=$2
  local derived_data=$3
  echo "==> xcodebuild IrohLib for $destination"
  rm -rf "$derived_data"
  arch -arm64 xcodebuild build \
    -scheme IrohLib \
    -destination "$destination" \
    -sdk "$sdk" \
    -derivedDataPath "$derived_data" \
    -configuration Release \
    IPHONEOS_DEPLOYMENT_TARGET="$IOS_DEPLOYMENT_TARGET" \
    CODE_SIGNING_ALLOWED=NO \
    CODE_SIGNING_REQUIRED=NO \
    DEVELOPMENT_TEAM= \
    | tee "$OUTPUT_DIR/xcodebuild-${sdk}.log"
}

cd "$SOURCE_DIR"
build_swift_for 'generic/platform=iOS' iphoneos "$OUTPUT_DIR/derived-ios"
build_swift_for 'generic/platform=iOS Simulator' iphonesimulator "$OUTPUT_DIR/derived-ios-simulator"

bash "$SCRIPT_DIR/check-private-apple-datapath.sh" \
  "$XCFRAMEWORK" \
  "$OUTPUT_DIR/derived-ios/Build/Products/Release-iphoneos/IrohLib.o" \
  "$OUTPUT_DIR/derived-ios-simulator/Build/Products/Release-iphonesimulator/IrohLib.o" \
  | tee "$OUTPUT_DIR/patched-check.log"

ditto -c -k --sequesterRsrc --keepParent "$XCFRAMEWORK" \
  "$OUTPUT_DIR/IrohLib.xcframework.zip"
shasum -a 256 "$OUTPUT_DIR/IrohLib.xcframework.zip" \
  | tee "$OUTPUT_DIR/IrohLib.xcframework.zip.sha256"

RUSTC_VERSION=$(rustc +"$RUST_VERSION" --version)
XCODE_VERSION=$(xcodebuild -version | paste -sd ' ' -)
IOS_SDK_VERSION=$(xcrun --sdk iphoneos --show-sdk-version)
SIMULATOR_SDK_VERSION=$(xcrun --sdk iphonesimulator --show-sdk-version)
cat > "$OUTPUT_DIR/manifest.json" <<EOF
{
  "iroh_ffi_tag": "$IROH_FFI_TAG",
  "iroh_ffi_commit": "$IROH_FFI_COMMIT",
  "iroh_core_version": "$IROH_CORE_VERSION",
  "iroh_relay_version": "$IROH_RELAY_VERSION",
  "noq_version": "$NOQ_VERSION",
  "features": ["metrics", "tls-ring"],
  "resolved_iroh_features": $RESOLVED_IROH_FEATURES_JSON,
  "excluded_iroh_features": ["fast-apple-datapath", "portmapper"],
  "excluded_local_discovery": ["multicast", "mDNS", "local-network address lookup"],
  "rust_toolchain": "$RUST_VERSION",
  "rustc": "$RUSTC_VERSION",
  "xcode": "$XCODE_VERSION",
  "iphoneos_sdk": "$IOS_SDK_VERSION",
  "iphonesimulator_sdk": "$SIMULATOR_SDK_VERSION",
  "ios_deployment_target": "$IOS_DEPLOYMENT_TARGET",
  "ios_runtime_tested": "$IROH_IOS_RUNTIME_VERSION",
  "slices": ["ios-arm64", "ios-arm64_x86_64-simulator"],
  "swift_builds": ["generic/platform=iOS", "generic/platform=iOS Simulator"]
}
EOF

echo "built artifacts in $OUTPUT_DIR"
