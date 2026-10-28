#!/usr/bin/env bash
# Install the fleet controller and agents as systemd USER units.
#
# Renders the per-service EnvironmentFiles the units read
# (~/.config/fleet/controller.conf, ~/.config/fleet/agent-<name>.conf),
# installs the unit files into ~/.config/systemd/user/, reloads the
# user manager, and enables everything with --now.
#
# Usage:
#   fleet/install-services.sh --repo DIR --state OPS_STATE_DIR \
#       [--reap-s SECONDS] [--heartbeat-s SECONDS] \
#       [--agent NAME CONFIG STATE ALLOWLIST]...
#
#   --repo         this repository's root (goes on PYTHONPATH)
#   --state        the desk state dir created by `desk init`
#   --agent        one per box agent: its enrolled name, the enroll
#                  bundle to use as --config, its agent state dir,
#                  and its allowlist file (all absolute paths)
#
# Example (controller + this box's own agent):
#   fleet/install-services.sh --repo "$HOME/basicc2" \
#       --state "$HOME/fleet-ops" \
#       --agent thor "$HOME/fleet-ops/enroll/thor.json" \
#                    "$HOME/fleet-ops/agent-state" \
#                    "$HOME/fleet-ops/allowlist.json"
#
# NOTE: user units only run while a session exists unless linger is
# enabled. For boot survival:  loginctl enable-linger "$USER"
set -euo pipefail

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_HOME/systemd/user"
CONF_DIR="$CONFIG_HOME/fleet"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="" STATE="" REAP_S=45 HEARTBEAT_S=15
AGENTS=()   # groups of 4: name config state allowlist

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)        REPO="$2"; shift 2 ;;
        --state)       STATE="$2"; shift 2 ;;
        --reap-s)      REAP_S="$2"; shift 2 ;;
        --heartbeat-s) HEARTBEAT_S="$2"; shift 2 ;;
        --agent)       AGENTS+=("$2" "$3" "$4" "$5"); shift 5 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$REPO" ] || [ -z "$STATE" ]; then
    echo "usage: $0 --repo DIR --state OPS_STATE_DIR [--agent NAME CONFIG STATE ALLOWLIST]..." >&2
    exit 2
fi

mkdir -p "$UNIT_DIR" "$CONF_DIR"
echo "installing units into $UNIT_DIR"
install -m 0644 "$SCRIPT_DIR/systemd/fleet-controller.service" "$UNIT_DIR/"
install -m 0644 "$SCRIPT_DIR/systemd/fleet-agent@.service" "$UNIT_DIR/"

echo "rendering $CONF_DIR/controller.conf (STATE=$STATE REAP_S=$REAP_S)"
cat > "$CONF_DIR/controller.conf" <<EOF
STATE=$STATE
PYTHONPATH=$REPO
REAP_S=$REAP_S
EOF

systemctl --user daemon-reload
systemctl --user enable --now fleet-controller.service
echo "enabled + started: fleet-controller.service"

i=0
while [ $i -lt ${#AGENTS[@]} ]; do
    name="${AGENTS[$i]}"; cfg="${AGENTS[$((i+1))]}"
    astate="${AGENTS[$((i+2))]}"; allow="${AGENTS[$((i+3))]}"
    echo "rendering $CONF_DIR/agent-$name.conf (CONFIG=$cfg STATE=$astate ALLOWLIST=$allow)"
    cat > "$CONF_DIR/agent-$name.conf" <<EOF
CONFIG=$cfg
STATE=$astate
ALLOWLIST=$allow
PYTHONPATH=$REPO
HEARTBEAT_S=$HEARTBEAT_S
EOF
    systemctl --user enable --now "fleet-agent@$name.service"
    echo "enabled + started: fleet-agent@$name.service"
    i=$((i+4))
done

echo
echo "done. Watch with:  journalctl --user -u fleet-controller -u 'fleet-agent@*' -f"
echo "For boot survival (units start at boot, before any login):"
echo "  loginctl enable-linger $USER"
