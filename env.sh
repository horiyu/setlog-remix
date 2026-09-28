# Source this: paths for the Android tools, and the settings.
export REMIX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# jdk/ and sdk/ are what bin/setup-sdk.sh fills in (or symlinks to ones you already have).
# Without jdk/, the java on PATH is used.
[ -x "$REMIX_HOME/jdk/bin/java" ] && export JAVA_HOME="$REMIX_HOME/jdk"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT_OVERRIDE:-$REMIX_HOME/sdk}"
export ANDROID_HOME="$ANDROID_SDK_ROOT"
export ANDROID_AVD_HOME="$REMIX_HOME/avd"                         # the AVD signed in to your account
export PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"
export SETLOG_PYTHON="${SETLOG_PYTHON:-python3}"
[ -f "$REMIX_HOME/settings.conf" ] || cp "$REMIX_HOME/settings.conf.example" "$REMIX_HOME/settings.conf"
. "$REMIX_HOME/settings.conf"
export DISPLAY="${DISPLAY_ID:-${DISPLAY:-:0}}"
# systemctl --user needs these; an SSH session may not have them.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
