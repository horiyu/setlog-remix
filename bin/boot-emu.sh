#!/bin/bash
# boot-emu.sh [avd]: start that emulator (default: AVD in settings.conf) with our camera videos, wait until Android
# is up, and print the emulator's pid. Retries once: right after a previous instance
# powered off, its lock can linger for a moment and the next start exits at once.
cd "$(dirname "$0")/.."
. ./env.sh
avd=${1:-$AVD}
[ -n "$avd" ] || { echo "usage: boot-emu.sh [avd]" >&2; exit 1; }
# After a crash the emulator opens a "send crash report?" dialog on the next start and
# waits for a click nobody will give. Decline once, in the file it reads for that.
mkdir -p ~/.android
[ -f ~/.android/analytics.settings ] ||
  printf '{"hasOptedIn":false,"debugDisablePublishing":true}\n' > ~/.android/analytics.settings
mkdir -p state
for v in card card_front; do          # the camera needs a file to open, even a blank one
  [ -s "state/$v.mp4" ] || ffmpeg -loglevel error -f lavfi -i color=c=0x0e0e10:s=1710x1280:d=1 \
    -pix_fmt yuv420p "state/$v.mp4"
done
card=$PWD/state/card.mp4              # post.sh writes the rendered video here
card_front=$PWD/state/card_front.mp4
for attempt in 1 2; do
  # 8>&- 9>&-: never hand our callers' lock fds to a process that outlives them.
  # -crash-report-mode never: after a crash the emulator otherwise waits on a "send the
  # report?" dialog that nobody is there to answer.
  emulator -avd "$avd" -no-snapshot -no-audio -no-boot-anim -gpu swiftshader_indirect \
    -crash-report-mode never -no-metrics \
    -camera-back "videofile:$card" \
    -camera-front "videofile:$card_front" \
    > "/tmp/setlog-remix-emu.log" 2>&1 < /dev/null 8>&- 9>&- &
  pid=$!
  for _ in $(seq 1 "${BOOT_WAIT:-150}"); do   # seconds; a brand-new AVD boots slower
    kill -0 "$pid" 2>/dev/null || break
    if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then
      sleep 5                          # let the launcher settle before anything taps
      echo "$pid"
      exit 0
    fi
    sleep 1
  done
  kill "$pid" 2>/dev/null
  sleep 5
done
echo "emulator $avd did not boot; see /tmp/setlog-remix-emu.log" >&2
grep -q "could not connect to display" /tmp/setlog-remix-emu.log 2>/dev/null &&
  echo "  -> no X display at $DISPLAY: set DISPLAY_ID in settings.conf (bin/doctor.sh checks it)" >&2
exit 1
