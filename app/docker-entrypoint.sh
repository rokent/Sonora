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
start_avahi() {
    command -v shairport-sync >/dev/null 2>&1 || return 0
    command -v avahi-daemon >/dev/null 2>&1 || return 0
    # .sonora marks a bus started here: after a container restart its socket
    # is stale, while a socket without the mark is the host's.
    if [ -S /run/dbus/system_bus_socket ] && [ ! -e /run/dbus/.sonora ]; then
        return 0                                       # the host's bus and avahi
    fi
    mkdir -p /run/dbus
    rm -f /run/dbus/system_bus_socket /run/dbus/pid /run/avahi-daemon/pid
    touch /run/dbus/.sonora
    dbus-daemon --system --fork || { echo "AirPlay: dbus did not start" >&2; return 0; }
    avahi-daemon --daemonize --no-chroot --no-rlimits \
        || echo "AirPlay: avahi did not start; AirPlay names will not be announced" >&2
}

if [ "$(id -u)" = 0 ]; then
    start_avahi
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
