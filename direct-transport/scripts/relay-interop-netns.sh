#!/usr/bin/env bash
# Called under unshare --net: never install rules in the caller's network.
set -euo pipefail
[[ $(readlink /proc/self/ns/net) != "$(readlink /proc/1/ns/net)" ]]
[[ $(ip -o link show | awk -F': ' '{print $2}') = lo ]]
ip link set lo up
for fw in iptables ip6tables; do
  "$fw" -N LOOKUP
  "$fw" -A LOOKUP -j DROP
  for proto in tcp udp; do "$fw" -A OUTPUT -p "$proto" --dport 53 -j LOOKUP; done
  "$fw" -A OUTPUT -p udp -j DROP
  "$fw" -A OUTPUT -o lo -p tcp -j ACCEPT
  "$fw" -P OUTPUT DROP
done
# Only the firewall setup needs privilege. Neither the peers nor the fixture
# can change the namespace policy, and there is no external interface/route.
repo=$1; shift
sudo -u "${SUDO_USER:?}" env -i PATH=/usr/bin:/bin HOME="$PWD" PYTHONPATH="$repo" \
  "$@" "$repo/direct-transport/tests/support/relay_driver.py" "$TEST_EXE" "$RELAY_EXE"
for fw in iptables ip6tables; do
  "$fw" -nvxL LOOKUP | awk '$3=="DROP" { found=1; if ($1!=0) exit 1 } END { if (!found) exit 1 }'
done
