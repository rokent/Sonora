# Windows sound on the speakers (Scream)

Sonora Home can play what a Windows PC plays on a party or a room, by acting
as the receiver for the Scream virtual sound card. Windows sends its audio
over the network; Sonora turns it into a live stream for the speakers.

## 1. Install the Scream driver on Windows

Download the latest release from
<https://github.com/duncanthrax/scream/releases> and install the driver
(`Install-x64.bat`, run as administrator; you may need to allow the unsigned
driver). A new output called **Scream (WDM)** appears in Windows.

## 2. Unicast or multicast

The driver sends to the multicast group `239.255.77.77` on UDP port `4010` by
default. Sonora listens for both.

* **Multicast** (default): nothing to set. It works when the PC and the
  Sonora host are on the same network and the switch/Wi-Fi passes multicast.
  Wi-Fi and some switches drop or throttle it; if the sound stutters or never
  arrives, use unicast.
* **Unicast**: send straight to the Sonora host. In the Windows registry, set
  the string value `Network` under
  `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\Scream\Options` to
  `<apps-vm address> 4010` (for example `192.168.1.20 4010`), and restart the
  Windows Audio service or reboot. The same key can set the multicast address.

## 3. Pick Scream as the Windows output

Windows Settings, System, Sound, Output: choose **Scream**. Everything
Windows plays is now sent over the network (and silent on the PC itself).

## 4. Map the source in Sonora

When Windows plays something, the PC shows up as a source (by its IP address)
at `GET /api/scream/sources`. Audio from a source is ignored until you map it
to a party or a room:

```
PUT /api/scream/sources/<ip>
{"target": {"kind": "party", "id": "<party id>"}}     # or {"kind": "zone", "id": "<zone uuid>"}
{"target": null}                                       # stop playing it
```

The mapping is kept in `scream.json` in Sonora's data folder. After mapping,
the next sound Windows plays starts on that target; when Windows is silent for
about 8 seconds the stream ends and the speaker goes back to its queue.

## 5. Firewall

On the host running Sonora (apps-vm), allow **UDP 4010 inbound**:

```
sudo ufw allow 4010/udp
```

If Sonora runs in a container, publish the port (`-p 4010:4010/udp`); multicast
reaches a container reliably only with host networking. Set
`SONORA_SCREAM_PORT` to use another port, or `0` to turn the receiver off. If
the port cannot be bound, Sonora logs a line and runs without it.

## Delay

Expect about 2 to 4 seconds between Windows and the speakers: the audio is
encoded to MP3 and a Sonos buffers a stream before it plays. This is fine for
music, but video and games will be out of sync with the picture.
