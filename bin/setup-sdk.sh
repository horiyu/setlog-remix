#!/bin/bash
# Put a JDK and the Android SDK this project needs into jdk/ and sdk/ (about 2.5 GB).
# Nothing is installed system-wide and no sudo is used. Already there = skipped.
# To use an SDK or JDK you already have instead, symlink it:  ln -s ~/Android/Sdk sdk
set -e
cd "$(dirname "$0")/.."
IMG="system-images;android-35;google_apis_playstore;x86_64"
ARCH=$(uname -m)
[ "$ARCH" = x86_64 ] || { echo "the emulator image here is x86_64; this machine is $ARCH" >&2; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# JDK 17 (Eclipse Temurin), unless java 17+ is on PATH already.
javaver() { "$1" -version 2>&1 | awk -F'"' '/version/ {split($2, v, "."); print (v[1] == 1 ? v[2] : v[1])}'; }
if [ -x jdk/bin/java ]; then
  echo "jdk/: already there"
elif command -v java >/dev/null && [ "$(javaver java)" -ge 17 ] 2>/dev/null; then
  echo "java $(javaver java) on PATH will be used"
else
  echo "downloading JDK 17 (Temurin) ..."
  curl -fL --progress-bar -o "$tmp/jdk.tar.gz" \
    "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"
  mkdir -p jdk && tar -xzf "$tmp/jdk.tar.gz" -C jdk --strip-components=1
fi
. ./env.sh

# Android command-line tools: the current "cmdline-tools;latest" from Google's repository index.
if [ ! -x sdk/cmdline-tools/latest/bin/sdkmanager ]; then
  echo "downloading Android command-line tools ..."
  read -r zip sha < <(curl -fsL https://dl.google.com/android/repository/repository2-3.xml | python3 -c '
import sys, re
x = sys.stdin.read()
seg = x[x.find("path=\"cmdline-tools;latest\""):][:4000]
m = re.search(r"<checksum type=\"sha1\">([0-9a-f]+)</checksum>\s*<url>(commandlinetools-linux-[0-9]+_latest\.zip)</url>", seg)
print(m.group(2), m.group(1))')
  curl -fL --progress-bar -o "$tmp/tools.zip" "https://dl.google.com/android/repository/$zip"
  echo "$sha  $tmp/tools.zip" | sha1sum -c --quiet
  mkdir -p sdk/cmdline-tools
  python3 -c "import zipfile, sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$tmp/tools.zip" "$tmp/x"
  rm -rf sdk/cmdline-tools/latest && mv "$tmp/x/cmdline-tools" sdk/cmdline-tools/latest
  chmod +x sdk/cmdline-tools/latest/bin/*
fi

# Licences are Google's; show them once and ask, instead of accepting on your behalf.
if [ ! -f sdk/licenses/android-sdk-license ]; then
  echo "The Android SDK licences follow. Answer y to accept them (sdkmanager asks)."
  sdkmanager --sdk_root="$PWD/sdk" --licenses
fi
echo "installing platform-tools, emulator and $IMG (this is the large part) ..."
sdkmanager --sdk_root="$PWD/sdk" "platform-tools" "emulator" "$IMG"
echo
echo "done. Next: bin/doctor.sh, then bin/make-avd.sh"
