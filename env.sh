# Source this: the settings, and where setlog-post is.
export REMIX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$REMIX_HOME/settings.conf" ] || cp "$REMIX_HOME/settings.conf.example" "$REMIX_HOME/settings.conf"
. "$REMIX_HOME/settings.conf"
SETLOG_POST=${SETLOG_POST:-~/dev/setlog-post/setlog-post}
export SETLOG_POST=${SETLOG_POST/#\~/$HOME}
# systemctl --user needs these; an SSH session may not have them.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
