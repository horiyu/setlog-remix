#!/bin/bash
# Drain queue/ one job at a time (server.py starts one of these per request; extra
# ones exit at the lock): render the video in its format (unless /preview already
# did), then hand it to setlog-screen-record's queue, whose worker posts it. Jobs
# move to done/, which keeps the newest 10. Redo one by hand with
#   python3 render.py job done/<job> && ./worker.sh --handoff done/<job>
cd "$(dirname "$0")"
mkdir -p state queue done
[ -f settings.conf ] || cp settings.conf.example settings.conf
SR=$(sed -n 's/^SCREEN_RECORD=\([^#]*\).*/\1/p' settings.conf | tr -d ' "')
SR=${SR:-~/dev/setlog-screen-record}
SR=${SR/#\~/$HOME}
log() { echo "$(date '+%F %T') $*"; }

# Give a rendered job to setlog-screen-record: recording.mp4, caption.txt, room.txt (and dry).
handoff() {
  local job=$1 name dest
  name="$(basename "$job")-remix"
  dest="$SR/queue/$name"
  [ -d "$SR/queue" ] || mkdir -p "$SR/queue"
  mkdir -p "$dest.part"
  cp "$job/out.mp4" "$dest.part/recording.mp4"
  python3 - "$job/job.json" "$dest.part" <<'EOF'
import json, os, sys
job = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
open(os.path.join(out, "caption.txt"), "w", encoding="utf-8").write(job.get("caption", "") + "\n")
open(os.path.join(out, "room.txt"), "w", encoding="utf-8").write("\n".join(job["rooms"]) + "\n")
if job.get("dry"):
    open(os.path.join(out, "dry"), "w").close()
EOF
  # setlog-screen-record's worker picks up whatever is in its queue; appear there whole.
  mv "$dest.part" "$dest"
  setsid "$SR/worker.sh" < /dev/null > /dev/null 2>&1 &
  log "handed to $dest"
}

if [ "$1" = --handoff ]; then handoff "$2"; exit; fi

exec 9> state/worker.lock
flock -n 9 || exit 0
{
while :; do
  job=$(ls queue 2>/dev/null | sort | head -1)
  [ -n "$job" ] || break
  log "=== $job"
  if { [ -s "queue/$job/out.mp4" ] || python3 render.py job "queue/$job"; } && handoff "queue/$job"; then
    log "rendered $job"
  else
    log "FAILED $job"
  fi
  mv "queue/$job" "done/$job"
  ls -d done/*/ 2>/dev/null | sort | head -n -10 | xargs -r rm -rf
done
log "queue empty"
} >> state/worker.log 2>&1
