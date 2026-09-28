#!/bin/bash
# Check what setlog-remix needs, step by step. Changes nothing.
cd "$(dirname "$0")/.."
. ./env.sh 2>/dev/null
ok=0; ng=0
check() {   # check "label" "hint when it fails" command...
  local label=$1 hint=$2; shift 2
  if "$@" >/dev/null 2>&1; then echo "  ok  $label"; ok=$((ok + 1))
  else echo "  NG  $label"; [ -n "$hint" ] && echo "      -> $hint"; ng=$((ng + 1)); fi
}
py() { "$SETLOG_PYTHON" -c "import sys; sys.path.insert(0, 'vendor'); $1"; }

echo "PC"
check "KVM (/dev/kvm writable)" "enable virtualisation in the BIOS; sudo usermod -aG kvm \$USER, then log in again" test -w /dev/kvm
check "X display $DISPLAY" "set DISPLAY_ID in settings.conf to your desktop's display (echo \$DISPLAY in a desktop terminal)" \
      py "from Xlib import display; display.Display()"
check "ffmpeg" "sudo apt install ffmpeg" command -v ffmpeg
check "Python: Pillow" "sudo apt install python3-pil" py "import PIL"
check "Python: python-xlib" "sudo apt install python3-xlib" py "import Xlib"
echo "Android"
check "java" "bin/setup-sdk.sh" command -v java
check "emulator" "bin/setup-sdk.sh" command -v emulator
check "adb" "bin/setup-sdk.sh" command -v adb
check "AVD '$AVD'" "bin/make-avd.sh" sh -c "emulator -list-avds | grep -qx '$AVD'"
echo "Posting"
check "Shortcut token (state/token)" "bin/install-service.sh makes one" test -s state/token
check "at least one room (rooms/*.png)" "bin/add-rooms.sh, then bin/name-room.sh" sh -c 'ls rooms/*.png'
check "receiver socket" "bin/install-service.sh" systemctl --user is-active --quiet setlog-remix.socket
check "receiver answers" "bin/install-service.sh" curl -fs "http://127.0.0.1:${PORT:-8091}/health"
check "tailscale" "install Tailscale and sign in on this PC and the iPhone (same tailnet)" command -v tailscale
check "tailscale serve on :8451" "bin/install-service.sh" sh -c "tailscale serve status 2>/dev/null | grep -q ':8451'"
echo
echo "$ok ok, $ng NG"
[ "$ng" -eq 0 ]
