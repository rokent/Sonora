#!/bin/sh
# Start Sonora as whoever owns its data folder.
#
# /data is often a folder on the host, created by the person installing (so
# owned by them) or by Docker on first run (so owned by root). The image's
# own user, sonora, can write to neither. So the container starts as root just
# long enough to look: a folder someone owns is used as theirs, and Sonora
# runs as that user; a folder root owns is handed to sonora. Root is dropped
# before Sonora starts either way.
set -e

# AirPlay receivers (backend/home/airplay.py): shairport-sync announces them
# through an avahi daemon on the system D-Bus and stops if none answers. A bus
# mounted from the host (with the host's avahi on it) is used as it is;
# otherwise the container runs its own bus and avahi, which share the host's
# network with any mDNS responder already there. Both need root, so here.
# SONORA_AIRPLAY=0 (or the Home Assistant app's "airplay: false") turns it
# all off. Nothing in here may stop Sonora from starting (a read-only root
# file system, a daemon that will not start): it runs in a subshell that
# ignores errors, and its caller ignores its status.
airplay_off() {
    if [ -f /data/options.json ] && command -v python >/dev/null 2>&1; then
        python - <<'PY' 2>/dev/null && SONORA_AIRPLAY=0 && export SONORA_AIRPLAY
import json, sys
sys.exit(0 if json.load(open("/data/options.json")).get("airplay") is False else 1)
PY
    fi
    case "$(printf %s "${SONORA_AIRPLAY:-}" | tr '[:upper:]' '[:lower:]')" in
        0|false|no|off) return 0 ;;
    esac
    return 1
}

start_avahi() (
    set +e
    command -v shairport-sync >/dev/null 2>&1 || exit 0
    command -v avahi-daemon >/dev/null 2>&1 || exit 0
    # .sonora marks a bus started here: after a container restart its socket
    # is stale, while a socket without the mark is the host's.
    if [ -S /run/dbus/system_bus_socket ] && [ ! -e /run/dbus/.sonora ]; then
        exit 0                                         # the host's bus and avahi
    fi
    mkdir -p /run/dbus 2>/dev/null
    rm -f /run/dbus/system_bus_socket /run/dbus/pid /run/avahi-daemon/pid 2>/dev/null
    touch /run/dbus/.sonora 2>/dev/null
    if ! dbus-daemon --system --fork; then
        echo "AirPlay: dbus did not start; AirPlay receivers will be off" >&2
        exit 0
    fi
    avahi-daemon --daemonize --no-chroot --no-rlimits \
        || echo "AirPlay: avahi did not start; AirPlay receivers will be off" >&2
    exit 0
)

if [ "$(id -u)" = 0 ]; then
    if airplay_off; then
        echo "AirPlay: off (SONORA_AIRPLAY=0)" >&2
    else
        start_avahi || true
    fi
    owner=$(stat -c %u /data)
    group=$(stat -c %g /data)
    if [ "$owner" = 0 ]; then
        chown sonora:sonora /data
        owner=$(id -u sonora)
        group=$(id -g sonora)
    fi
    # Home Assistant writes the app's options before every start, as root and
    # readable by root alone; Sonora reads its ports from them.
    if [ -f /data/options.json ]; then
        chown "$owner:$group" /data/options.json
    fi
    exec setpriv --reuid="$owner" --regid="$group" --clear-groups -- "$@"
fi
exec "$@"
