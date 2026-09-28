#!/bin/bash
# post.sh <job dir>: post one rendered video from your own setlog account.
# The job dir holds out.mp4 (render.py's 1710x962 video) and job.json (caption,
# rooms, dry; server.py writes it). Boots the AVD named in settings.conf with the
# video wired to its camera, records a Log in setlog, pastes the caption, picks the
# rooms, sends, waits for the upload, and powers the emulator off. DRY_RUN=1 (or
# "dry": true in job.json) stops at the send screen and cancels instead of sending.
set -e
cd "$(dirname "$0")"
. ./env.sh                             # settings.conf: AVD, DEFAULT_ROOM, EXTRA_LOCKS, LEAD, TAP_*
job=$1
[ -d "$job" ] || { echo "usage: post.sh <job dir>" >&2; exit 1; }
log() { echo "$(date '+%F %T') $*"; }

TAP_RECORD=${TAP_RECORD:-"540 1836"}   # shutter, portrait coordinates
TAP_SEND=${TAP_SEND:-"984 206"}        # the send arrow
TAP_CANCEL="95 207"                    # the X on the send screen

video="$job/out.mp4"
[ -s "$video" ] || { echo "no out.mp4 in $job (render it: python3 render.py job $job)" >&2; exit 1; }
field() { "$SETLOG_PYTHON" -c 'import json,sys; v=json.load(open(sys.argv[1],encoding="utf-8")).get(sys.argv[2]); print("\n".join(v) if isinstance(v,list) else ("1" if v is True else "" if v in (None,False) else v))' "$job/job.json" "$1"; }
caption=$(field caption | head -1)
rooms=$(field rooms | grep . || true)                    # one room per line
rooms=${rooms:-$DEFAULT_ROOM}
room=$(printf '%s' "$rooms" | paste -sd, -)              # for messages
[ "$(field dry)" = 1 ] && DRY_RUN=1

# 1. The camera video. The emulator's back camera reads a 1710x1280 frame and setlog
# keeps its top 16:9 band (1710x962), which is exactly what render.py made. The
# emulator starts playing when setlog opens the camera and the shutter is pressed about
# 5 s later, so the first frame is held for LEAD seconds and the Log begins at the
# video's first frame. setlog may open the front camera, which mirrors: keep a
# pre-flipped copy for it.
ffmpeg -y -loglevel error -i "$video" -an -vf "tpad=start_duration=${LEAD:-5}:start_mode=clone,pad=1710:1280:0:0:color=0x0e0e10,format=yuv420p" \
  -c:v libx264 -preset medium -crf 16 -g 15 state/card.next.mp4
mv -f state/card.next.mp4 state/card.mp4
ffmpeg -y -loglevel error -i state/card.mp4 -vf hflip -c:v libx264 -preset medium -crf 16 -g 15 state/card_front.next.mp4
mv -f state/card_front.next.mp4 state/card_front.mp4

# 2. One emulator at a time: wait for every other program that drives one (EXTRA_LOCKS).
fd=20
for lock in ${EXTRA_LOCKS:-}; do
  [ -d "$(dirname "$lock")" ] || continue
  eval "exec $fd> \"\$lock\""
  flock -w 900 $fd || { echo "$lock is busy" >&2; exit 1; }
  fd=$((fd + 1))
done
# The lock's previous holder powers its emulator off on exit; give that a minute.
for _ in $(seq 1 60); do
  adb devices | grep -q '^emulator-' || break
  sleep 1
done
adb devices | grep -q '^emulator-' && { echo "an emulator is already running" >&2; exit 1; }

emu_pid=
cleanup() {
  set +e
  [ -f state/clip.pid ] && { kill "$(cat state/clip.pid)" 2>/dev/null; rm -f state/clip.pid; }
  if [ -n "$emu_pid" ]; then
    adb emu sensor set acceleration 0:9.81:0.8 >/dev/null 2>&1
    bin/stop-emu.sh "$emu_pid"
    log "emulator stopped"
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP

log "booting $AVD for $(basename "$job") (room=$room)"
emu_pid=$(bin/boot-emu.sh "$AVD") || { echo "boot failed" >&2; exit 1; }

# 3. Caption onto the clipboard (Japanese cannot go through `input text`).
if [ -n "$caption" ]; then
  setsid nohup "$SETLOG_PYTHON" lib/clip.py "$caption" > state/clip.log 2>&1 < /dev/null 9>&- &
  echo $! > state/clip.pid
  sleep 3
fi

# 4. setlog: open, dismiss the backup dialog if it shows, tilt to landscape, record.
adb emu sensor set acceleration 0:9.81:0.8 >/dev/null
adb shell am force-stop com.newchat.setlog
sleep 1
adb shell monkey -p com.newchat.setlog -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 6
if adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; then
  xy=$(adb shell cat /sdcard/ui.xml 2>/dev/null | "$SETLOG_PYTHON" -c '
import sys,re
m=re.search(r"resource-id=\"android:id/button2\"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"",sys.stdin.read())
if m: a,b,c,d=map(int,m.groups()); print((a+c)//2,(b+d)//2)')
  [ -n "$xy" ] && { adb shell input tap $xy; sleep 2; }
  adb shell rm -f /sdcard/ui.xml
fi
adb emu sensor set acceleration -9.81:0:0 >/dev/null
sleep 5
adb shell input tap $TAP_RECORD
# The countdown (3 or 5 s, whatever the timer button is set to) then the clip: wait
# for the send screen, recognisable by the green disc of the send arrow (its centre is the black arrow itself).
sent_screen=0
for _ in $(seq 1 30); do
  sleep 1
  if adb exec-out screencap -p | "$SETLOG_PYTHON" -c '
import sys,io
from PIL import Image
r,g,b=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert("RGB").getpixel((960,180))
sys.exit(0 if g>80 and r<60 and b<90 else 1)'; then sent_screen=1; break; fi
done
[ $sent_screen = 1 ] || { adb exec-out screencap -p > state/last-fail.png; echo "send screen never came (see state/last-fail.png)" >&2; exit 1; }
sleep 1

# 5. Caption, then the room (by its avatar), then send.
if [ -n "$caption" ]; then
  adb shell input keyevent 279          # paste into the focused caption field
  sleep 1
fi
adb shell input keyevent 4              # hide the keyboard: all rows become visible
sleep 1
# Tick each room: find its row by avatar, tap the checkbox, confirm it turned green.
# The rows do not move when one is ticked, so one screenshot serves all of them.
adb exec-out screencap -p > state/last-send.png
if [ -n "${CAPTURE_ROOMS:-}" ]; then   # bin/add-rooms.sh: cut the rows out, send nothing
  "$SETLOG_PYTHON" ./rooms.py crop state/last-send.png "$CAPTURE_ROOMS"
  for _ in 1 2 3 4 5; do               # scroll for rooms below the fold
    adb shell input swipe 540 2100 540 1100 500; sleep 1
    adb exec-out screencap -p > state/last-send.png
    before=$(ls "$CAPTURE_ROOMS"/*-row.png 2>/dev/null | wc -l)
    "$SETLOG_PYTHON" ./rooms.py crop state/last-send.png "$CAPTURE_ROOMS" >/dev/null
    [ "$(ls "$CAPTURE_ROOMS"/*-row.png | wc -l)" -gt "$before" ] || break
  done
  "$SETLOG_PYTHON" ./rooms.py sheet "$CAPTURE_ROOMS" >/dev/null
  adb shell input tap $TAP_CANCEL; sleep 1
  exit 0
fi
picked=
mapfile -t room_list <<< "$rooms"      # not a read loop: adb shell would eat the rest of stdin
for r in "${room_list[@]}"; do
  [ -n "$r" ] || continue
  xy=$("$SETLOG_PYTHON" ./find_room.py state/last-send.png "$r") || {
    adb shell input swipe 540 2000 540 1000 400; sleep 1
    adb exec-out screencap -p > state/last-send.png
    xy=$("$SETLOG_PYTHON" ./find_room.py state/last-send.png "$r")
  } || { adb shell input tap $TAP_CANCEL; exit 1; }
  adb shell input tap $xy
  sleep 1
  adb exec-out screencap -p > state/last-room.png
  "$SETLOG_PYTHON" - state/last-room.png $xy "$r" <<'PY' || { adb shell input tap $TAP_CANCEL; exit 1; }
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
x, y = int(sys.argv[2]), int(sys.argv[3])
p = im.getpixel((x, y))
if max(p) - min(p) < 12:
    sys.exit(f"{sys.argv[4]!r} was not picked at {x},{y} (saw {p}): see {sys.argv[1]}")
PY
  picked="$picked $xy;"
done
xy=$picked
if [ "${DRY_RUN:-0}" = 1 ]; then
  if [ -n "${DRY_RECORD:-}" ]; then   # debugging: film the send screen's looping preview
    adb shell screenrecord --time-limit 10 /sdcard/preview.mp4
    adb pull /sdcard/preview.mp4 "$DRY_RECORD" >/dev/null && adb shell rm -f /sdcard/preview.mp4
  fi
  log "dry run: cancelling at the send screen (room picked at $xy)"
  adb shell input tap $TAP_CANCEL; sleep 1
  exit 0
fi
adb shell input tap $TAP_SEND

# 6. Wait for the upload: "sent" shows at once, the video goes up afterwards.
tx() { adb shell cat /proc/net/dev 2>/dev/null | awk '/eth0|wlan0/{t+=$10} END{print t+0}'; }
tx_start=$(tx); tx_prev=$tx_start; quiet=0; settled=0
for _ in $(seq 1 80); do
  sleep 3
  tx_now=$(tx)
  if [ $(( tx_now - tx_prev )) -lt 20000 ]; then quiet=$(( quiet + 1 )); else quiet=0; fi
  tx_prev=$tx_now
  [ $(( tx_now - tx_start )) -gt 150000 ] && [ $quiet -ge 3 ] && { settled=1; break; }
done
uploaded=$(( tx_prev - tx_start ))
adb exec-out screencap -p > state/last-sent.png
ok=true; [ $settled = 1 ] || ok=false
"$SETLOG_PYTHON" -c 'import json,sys,time,datetime
print(json.dumps({"time":datetime.datetime.now().isoformat(timespec="seconds"),
                  "epoch":int(time.time()),"job":sys.argv[1],"room":sys.argv[2],"caption":sys.argv[3],
                  "uploaded_bytes":int(sys.argv[4]),"ok":sys.argv[5]=="true"},
                 ensure_ascii=False))' "$(basename "$job")" "$room" "$caption" "$uploaded" "$ok" >> state/posts.jsonl
if [ "$ok" = true ]; then
  log "sent to $room: $caption"
else
  echo "upload not seen to finish (~$uploaded bytes): check state/last-send.png" >&2
  exit 1
fi
