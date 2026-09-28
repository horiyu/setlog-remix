#!/bin/bash
# Collect your setlog rooms for posting:  bin/add-rooms.sh
# Boots the AVD, films a blank second, and at setlog's send screen cuts every room row
# out (it sends nothing and cancels). Then look at state/rooms-new/sheet.png and name
# the rooms you want to post to:  bin/name-room.sh <number> "<room name>"
set -e
cd "$(dirname "$0")/.."
. ./env.sh
mkdir -p state
exec 9> state/worker.lock
flock -w 900 9 || { echo "a post is in progress; try again later" >&2; exit 1; }
job=state/rooms-job
rm -rf "$job" state/rooms-new
mkdir -p "$job"
# a test pattern: setlog fails to capture a plain single-colour feed
ffmpeg -loglevel error -f lavfi -i testsrc2=s=1710x962:d=3 -pix_fmt yuv420p "$job/out.mp4"
echo '{"caption": "", "rooms": []}' > "$job/job.json"
CAPTURE_ROOMS="$PWD/state/rooms-new" ./post.sh "$job"
rm -rf "$job"
echo
echo "Rooms found: open state/rooms-new/sheet.png, then name the ones you post to, e.g."
echo "  bin/name-room.sh 3 \"vlog\""
echo "The name is what you will pick on the iPhone Shortcut. Use the room's name as shown in setlog."
