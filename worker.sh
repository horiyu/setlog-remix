#!/bin/bash
# Drain queue/ one job at a time (server.py starts one of these per request; extra
# ones exit at the lock): render the video in its format (unless /preview already
# did), then post it (post.sh). Jobs move to done/, which keeps the newest 10.
# Redo one by hand:  python3 render.py job done/<job> && ./post.sh done/<job>
cd "$(dirname "$0")"
mkdir -p state queue done
exec 9> state/worker.lock
flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*"; }
{
while :; do
  job=$(ls queue 2>/dev/null | sort | head -1)
  [ -n "$job" ] || break
  log "=== $job"
  if [ -s "queue/$job/out.mp4" ] || python3 render.py job "queue/$job"; then
    if ./post.sh "queue/$job"; then log "posted $job"; else log "FAILED to post $job"; fi
  else
    log "FAILED to render $job"
  fi
  mv "queue/$job" "done/$job"
  ls -d done/*/ 2>/dev/null | sort | head -n -10 | xargs -r rm -rf
done
log "queue empty"
} >> state/worker.log 2>&1
