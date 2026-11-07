# AirPlay: send sound from a phone or computer

Sonora shows up as AirPlay speakers on your home network:

- one per **running party**, named `Sonora: <party name>`, which plays on every speaker in that party;
- one per **room**, named `Sonora: <room name>`, for S1 and S2 rooms alike
  (not for offline rooms or rooms in a running party; see below).

Pick one as your AirPlay output and play anything. When you stop, or the
sender goes quiet for 8 seconds (phone locked, lid closed), the party or room
goes back to its queue where it was. A newer sender on the same party or room
takes over from the older one.

The current names are listed in the app under **AirPlay names**
(`GET /api/airplay/receivers`). Party names come and go as parties start and
end, within about 5 seconds.

## Picking the speaker

**iPhone / iPad.** Open Control Center, long-press the music card, tap the
AirPlay icon and pick `Sonora: …`. In a music or video app you can also tap
the AirPlay icon in the player.

**Mac.** Control Center > Sound > pick `Sonora: …` (or System Settings >
Sound > Output). In Music, use the AirPlay button in the toolbar.

**Linux (PipeWire).** Load PipeWire's RAOP discovery module once:

```sh
pactl load-module module-raop-discover
```

(or add `{ name = libpipewire-module-raop-discover }` to
`~/.config/pipewire/pipewire.conf.d/raop.conf` to keep it). The `Sonora: …`
speakers then appear as outputs in your sound settings.

**Windows.** Windows has no AirPlay sender built in. Install
[TuneBlade](https://www.tuneblade.com/), which lists AirPlay speakers and
sends the PC's sound to the one you pick. For Windows there is also the
network sound card (see `windows-sound.md`), which has less delay.

## Delay

AirPlay is not meant for lip sync here: expect 2 to 5 seconds between the
sender and the speakers (AirPlay's own buffer plus the speakers' radio-style
stream). Fine for music; video will not line up.

The sender's volume control does nothing. Use the party or room volume in
Sonora.

## Network and firewall

Sonora must run on the home network itself (Docker `network_mode: host`, or
the Home Assistant app, which uses the host's network). Open, on the machine
running Sonora:

| What | Port | Protocol |
|---|---|---|
| AirPlay connections and audio | 5100-5199 | TCP and UDP |
| Speaker announcements (mDNS / Bonjour) | 5353 | UDP (multicast 224.0.0.251) |

Each receiver listens on one TCP port in 5100-5199, and each active sender
uses three UDP ports from the same range.

The image runs its own avahi daemon (started by the entrypoint) to announce
the receivers. It shares the host's network with any mDNS responder already
there (another avahi, Home Assistant's). If you mount the host's D-Bus at
`/run/dbus`, the host's avahi is used instead.

## Turning it off

Set `SONORA_AIRPLAY=0` (Docker / docker-compose environment, or the Unraid
template's "AirPlay receivers" setting). In the Home Assistant app, turn
**AirPlay receivers** off on the Configuration tab. Then neither the
receivers nor the container's dbus/avahi daemons are started.

## Which rooms get a name

Rooms that are offline get none, and a room in a running party gets none
while the party runs: send to the party's name instead. The room's name comes
back within about 5 seconds of the party ending.

## When the names do not appear

- Check the log for `airplay:` lines:
  - `shairport-sync is not installed`: AirPlay is off (normal outside the
    Docker image).
  - `AirPlay off: no mDNS (avahi) available`: the container's avahi did not
    start (see the `AirPlay:` lines at the top of the log), or a mounted host
    D-Bus has no avahi on it.
  - `not restarting it`: that receiver exited 5 times in a row right after
    starting. Restart Sonora after fixing the cause (usually a port in
    5100-5199 taken by something else).
- The sender must be on the same network segment (VLAN) as Sonora, or mDNS
  must be forwarded between segments (UniFi "Multicast DNS", an mDNS
  reflector).
- Two parties or rooms with the same name get ` 2`, ` 3` added. Names longer
  than 50 bytes are cut.
