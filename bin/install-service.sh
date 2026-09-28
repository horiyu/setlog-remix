#!/bin/bash
# Install the receiver as a per-user systemd socket (it only runs while a request is
# being handled), open it to your tailnet with `tailscale serve`, and print what the
# iPhone Shortcut needs. Run it again after moving this folder.
set -e
cd "$(dirname "$0")/.."
. ./env.sh
PORT=${PORT:-8091}; HTTPS_PORT=${HTTPS_PORT:-8451}
mkdir -p state
if [ ! -s state/token ]; then
  (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(24))" > state/token)
  echo "made a new Shortcut token in state/token"
fi

# The receiver runs under systemd, which may not know your desktop's DISPLAY: pin it.
if [ -n "${DISPLAY:-}" ] && grep -q '^DISPLAY_ID= ' settings.conf; then
  sed -i "s|^DISPLAY_ID= |DISPLAY_ID=$DISPLAY |" settings.conf
  echo "settings.conf: DISPLAY_ID=$DISPLAY"
fi

unit=~/.config/systemd/user
mkdir -p "$unit"
sed -e "s|__PORT__|$PORT|" systemd/setlog-remix.socket > "$unit/setlog-remix.socket"
sed -e "s|__DIR__|$PWD|g" systemd/setlog-remix.service > "$unit/setlog-remix.service"
systemctl --user daemon-reload
systemctl --user stop setlog-remix.service 2>/dev/null || true   # it holds the port; new code on next start
systemctl --user reset-failed setlog-remix.socket 2>/dev/null || true
systemctl --user enable setlog-remix.socket >/dev/null
systemctl --user restart setlog-remix.socket
sleep 1
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && echo "receiver: listening on 127.0.0.1:$PORT"

host=
if command -v tailscale >/dev/null; then
  tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$PORT" >/dev/null ||
    echo "tailscale serve failed: allow it with  sudo tailscale set --operator=\$USER  and run this again" >&2
  host=$(tailscale status --json | python3 -c 'import sys, json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
else
  echo "tailscale not found: install it on this PC and the iPhone, then run this again" >&2
fi
base="https://${host:-<machine>.<tailnet>.ts.net}:$HTTPS_PORT"
cat <<MSG

For the iPhone Shortcut (README, "iOS Shortcut"):
  base URL  $base
  token     $(cat state/token)
  test      open $base/health in Safari on the iPhone -> {"ok": true}
MSG
