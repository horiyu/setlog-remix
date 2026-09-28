#!/bin/bash
# Stop the running emulator without losing what was just written. `adb emu kill`
# drops the VM on the spot, and a setlog install or login made in the last few
# seconds never reaches the disk image; a real power-off flushes it first.
# Usage: stop-emu.sh [emulator pid]
cd "$(dirname "$0")/.."
. ./env.sh
pid=${1:-$(pgrep -f 'qemu-system-x86_64 -avd' | head -1)}
adb shell sync >/dev/null 2>&1
adb shell reboot -p >/dev/null 2>&1
for _ in $(seq 1 90); do
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    sleep 3                            # its lock files go a moment after the process
    exit 0
  fi
  sleep 1
done
adb emu kill >/dev/null 2>&1           # it did not power off by itself
[ -n "$pid" ] && kill "$pid" 2>/dev/null
exit 0
