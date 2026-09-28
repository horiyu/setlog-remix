#!/bin/bash
# Drain queue/ one job at a time (server.py starts one of these per request; extra
# ones exit at the lock): render the video in its format (unless /preview already
# did), then hand it to setlog-post, which boots the emulator and posts it. Jobs move
# to done/, which keeps the newest 10. Redo one by hand:  ./worker.sh --job done/<job>
cd "$(dirname "$0")"
. ./env.sh
mkdir -p state queue done
log() { echo "$(date '+%F %T') $*"; }

# One job: render, then `setlog-post post` (it queues and returns).
one() {
  local job=$1 args
  [ -s "$job/out.mp4" ] || python3 render.py job "$job" || return 1
  mapfile -t args < <(python3 - "$job/job.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1], encoding="utf-8"))
out = ["--room", ",".join(j.get("rooms") or [])] if j.get("rooms") else []
out += ["--caption", j.get("caption", "").replace("\n", " ")]
if j.get("dry"):
    out.append("--dry")
print("\n".join(out))
PY
)
  "$SETLOG_POST" post "$job/out.mp4" "${args[@]}" --from setlog-remix
}

if [ "$1" = --job ]; then one "$2"; exit; fi

exec 9> state/worker.lock
flock -n 9 || exit 0
{
while :; do
  job=$(ls queue 2>/dev/null | sort | head -1)
  [ -n "$job" ] || break
  log "=== $job"
  if out=$(one "queue/$job" 2>&1); then log "handed to setlog-post: $out"; else log "FAILED $job: $out"; fi
  mv "queue/$job" "done/$job"
  ls -d done/*/ 2>/dev/null | sort | head -n -10 | xargs -r rm -rf
done
log "queue empty"
} >> state/worker.log 2>&1
