#!/bin/bash
# Create the emulator for your own setlog account and put setlog on it:  make-avd.sh [name]
# (default name: AVD in settings.conf). setlog comes from Google Play inside the emulator
# (sign in to a Google account there), or from APK files you put in apk/ (pulled from an
# Android device you own). Signing in to setlog is done by hand, once: sign in with the
# same method as on your phone, then
# "Transfer from another device" and approve it on the phone. Never "Create a new
# encryption key" for an account you already use: older Logs may become unreadable.
set -e
cd "$(dirname "$0")/.."
. ./env.sh
name=$(printf '%s' "${1:-$AVD}" | tr -cd 'A-Za-z0-9_-')
[ -n "$name" ] || { echo "usage: $0 [name]" >&2; exit 1; }
IMG="system-images;android-35;google_apis_playstore;x86_64"
mkdir -p "$ANDROID_AVD_HOME"

if ! emulator -list-avds | grep -qx "$name"; then
  echo no | avdmanager create avd -n "$name" -k "$IMG" -d pixel_7 --force >/dev/null
  # setlog films through the front camera too; without this it records a black clip.
  sed -i 's/^hw\.camera\.front *= *none$/hw.camera.front=emulated/; s/^hw\.camera\.front=none$/hw.camera.front=emulated/' "$ANDROID_AVD_HOME/$name.avd/config.ini"
  echo "AVD $name created"
fi
adb devices | grep -q '^emulator-' && { echo "an emulator is already running; stop it first" >&2; exit 1; }
BOOT_WAIT=600 bin/boot-emu.sh "$name" >/dev/null || exit 1
installed() { adb shell pm list packages 2>/dev/null | grep -q com.newchat.setlog; }
if installed; then
  echo "setlog already installed"
elif ls apk/*.apk >/dev/null 2>&1; then
  adb install-multiple -r -t apk/*.apk
  sleep 15; adb shell sync            # a fresh install is lost if the VM dies at once
  echo "setlog installed from apk/"
else
  adb shell am start -a android.intent.action.VIEW -d "market://details?id=com.newchat.setlog" >/dev/null
  echo "In the emulator window: sign in to Google Play, then install setlog (it is open there)."
  echo "Waiting for the install (up to 20 min) ..."
  for _ in $(seq 1 240); do installed && break; sleep 5; done
  installed || { echo "setlog was not installed; run bin/make-avd.sh again when ready" >&2; exit 1; }
  sleep 15; adb shell sync
  echo "setlog installed from Google Play"
fi
adb shell pm grant com.newchat.setlog android.permission.CAMERA 2>/dev/null || true
adb shell pm grant com.newchat.setlog android.permission.RECORD_AUDIO 2>/dev/null || true
adb shell pm grant com.newchat.setlog android.permission.POST_NOTIFICATIONS 2>/dev/null || true
adb shell monkey -p com.newchat.setlog -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
cat <<MSG

Emulator '$name' is up with setlog open. In its window:
  1. Sign in to the account you use on your phone. The surest way: add a backup email to
     that account in the phone app (Account) and use "Continue with email" here. Signing in
     another way can land you in a different, new account.
  2. At "Finish secure setup" choose "Transfer from another device", then on your phone open
     Account > Transfer to Another Device, type its 8-digit code here and approve on the phone.
     Do NOT choose "Create a new encryption key" for an account you already use.
  3. Stop the emulator with  bin/stop-emu.sh  (not adb emu kill: it loses the last writes).
MSG
