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

echo "PC"
check "ffmpeg" "sudo apt install ffmpeg" command -v ffmpeg
check "Python: Pillow" "sudo apt install python3-pil" python3 -c "import PIL"
check "a Japanese font" "sudo apt install fonts-noto-cjk" sh -c "fc-match -f '%{file}' 'Noto Sans CJK JP' | grep -qi noto"
check "formats repository reachable" "check DEFAULT_REPO / GITHUB_TOKEN in settings.conf" python3 repo.py "${DEFAULT_REPO:-horiyu/setlog-formats}"
echo "Posting (setlog-post)"
check "setlog-post at $SETLOG_POST" "clone https://github.com/horiyu/setlog-post and set SETLOG_POST in settings.conf" test -x "$SETLOG_POST"
check "setlog-post has rooms" "set up setlog-post (its README), then bin/doctor.sh there" \
      sh -c "\"$SETLOG_POST\" rooms | grep -q '\"rooms\": \\[\"'"
echo "Receiver"
check "Shortcut token (state/token)" "bin/install-service.sh makes one" test -s state/token
check "receiver socket" "bin/install-service.sh" systemctl --user is-active --quiet setlog-remix.socket
check "receiver answers" "bin/install-service.sh" curl -fs "http://127.0.0.1:${PORT:-8091}/health"
check "tailscale" "install Tailscale and sign in on this PC and the iPhone (same tailnet)" command -v tailscale
check "tailscale serve on :8451" "bin/install-service.sh" sh -c "tailscale serve status 2>/dev/null | grep -q ':8451'"
echo
echo "$ok ok, $ng NG"
[ "$ng" -eq 0 ]
