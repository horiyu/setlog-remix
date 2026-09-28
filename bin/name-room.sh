#!/bin/bash
# bin/name-room.sh <number> "<room name>": keep room <number> from bin/add-rooms.sh
# as rooms/<room name>.png, one of the rooms the iPhone Shortcut offers.
set -e
cd "$(dirname "$0")/.."
n=$1; name=$2
[ -n "$n" ] && [ -n "$name" ] || { echo "usage: $0 <number> \"<room name>\"" >&2; exit 1; }
case $name in */*|.*) echo "the name cannot contain / or start with ." >&2; exit 1 ;; esac
[ -f "state/rooms-new/$n.png" ] || { echo "no room $n: run bin/add-rooms.sh first" >&2; exit 1; }
cp "state/rooms-new/$n.png" "rooms/$name.png"
echo "added: $name"
python3 rooms.py check | grep -v "^[0-9]* rooms, 0 " || true
