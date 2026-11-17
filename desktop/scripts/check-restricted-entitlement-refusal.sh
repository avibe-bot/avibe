#!/bin/sh
# Prove that this Mac refuses, at exec, code that claims the Developer ID app's
# restricted entitlements without a provisioning profile to authorize them.
#
# The release launch smoke test only shows that macOS accepted the signed app's
# embedded profile if the machine running it refuses such code, as a user's Mac
# does. A trivial program, ad-hoc signed with entitlements/app-developer-id.plist
# and no profile, must be killed (status 137). Any other outcome fails. Needs no
# credentials; pull request CI runs it on the same hosted image as releases.
set -eu

desktop=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/avibe-entitlement-control.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

control="$work/control"
printf 'int main(void) { return 0; }\n' | xcrun clang -x c -o "$control" -
codesign --force --sign - --options runtime \
  --entitlements "$desktop/entitlements/app-developer-id.plist" "$control"

status=0
"$control" || status=$?
if [ "$status" -ne 137 ]; then
  echo "This Mac did not refuse an unauthorized restricted entitlement (status $status);" \
    "a launch smoke test here cannot prove the provisioning profile" >&2
  exit 1
fi
echo "Unauthorized restricted entitlements are refused at exec (status 137)."
