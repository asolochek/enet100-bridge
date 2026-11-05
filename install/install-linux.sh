#!/bin/sh
# Install, update or remove enet100-bridge as a systemd service.
#
#   sudo ./install/install-linux.sh                 # install or update
#   sudo ./install/install-linux.sh --host 10.0.0.50
#   sudo ./install/install-linux.sh --uninstall [--purge]
#
# Installs into /opt/enet100-bridge (virtualenv), config in
# /etc/enet100-bridge/config.toml, unit enet100-bridge.service.
# If rpcbind already owns port 111, the bridge registers with it instead.
set -eu

PREFIX=/opt/enet100-bridge
CONFDIR=/etc/enet100-bridge
UNIT=/etc/systemd/system/enet100-bridge.service
REPO=$(cd "$(dirname "$0")/.." && pwd)
HOST=""
UNINSTALL=0
PURGE=0
PYTHON=${PYTHON:-python3}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        --python) PYTHON="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

if [ "$UNINSTALL" -eq 1 ]; then
    systemctl disable --now enet100-bridge.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    [ "$PURGE" -eq 1 ] && rm -rf "$CONFDIR"
    echo "uninstalled"
    exit 0
fi

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Python 3.11 or newer is required (set PYTHON=/path/to/python3)" >&2; exit 1; }

systemctl stop enet100-bridge.service 2>/dev/null || true
[ -x "$PREFIX/venv/bin/python" ] || "$PYTHON" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$REPO"
echo "installed into $PREFIX/venv"

mkdir -p "$CONFDIR"
CONF="$CONFDIR/config.toml"
BRIDGE="$PREFIX/venv/bin/enet100-bridge"
if [ -n "$HOST" ]; then
    if [ -f "$CONF" ]; then
        echo "keeping existing $CONF (run '$BRIDGE configure' to change it)"
    else
        "$BRIDGE" configure --config "$CONF" --add "$HOST"
    fi
else
    RUN=1
    if [ -f "$CONF" ]; then
        printf 'A config already exists (%s). Review or change it now? (y/N): ' "$CONF"
        read -r ANS
        case "$ANS" in [yY]*) ;; *) RUN=0 ;; esac
    fi
    # Interactive: discovery, pick list, naming, bus scan.
    [ "$RUN" -eq 1 ] && { "$BRIDGE" configure --config "$CONF" || true; }
    [ -f "$CONF" ] || { echo "no config was saved; rerun, or pass --host <ip>" >&2; exit 1; }
fi

cat > "$UNIT" <<EOF
[Unit]
Description=VXI-11 bridge for NI GPIB-ENET/100
After=network-online.target rpcbind.service
Wants=network-online.target

[Service]
ExecStart=$PREFIX/venv/bin/enet100-bridge run --config $CONFDIR/config.toml
# SIGTERM makes the bridge close its ENET/100 sessions cleanly.
KillSignal=SIGTERM
TimeoutStopSec=30
Restart=on-failure
RestartSec=10
# Binding port 111 needs this when not running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now enet100-bridge.service
sleep 2
systemctl --no-pager status enet100-bridge.service || true
echo
echo "Instruments on the box are now TCPIP::$(hostname)::gpib0,<address>::INSTR"
echo "NI-488.2 hosts can add it in NI MAX (Add GPIB Ethernet Device, address $(hostname)); it shows as an NI GPIB-ENET/1000."
