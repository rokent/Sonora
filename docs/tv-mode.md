# TV mode

TV mode is a full-screen page for a party. It follows what the party is
playing and shows it on a TV, a tablet on the wall, or a Home Assistant
dashboard. It needs no login on the screen itself.

## Display links

A display link is a private address for one screen. A signed-in, named user
creates one per screen (screen settings, "Display links"):

- a name (for example "Living room TV");
- which party to follow: any party, or one in particular. With "any party",
  the screen shows "No party playing" when the party ends and picks up the
  next one by itself;
- the mode (below);
- whether to show lyrics.

The link is shown once, as a URL and a QR code. Opening it signs that screen in
as a read-only display for a year and opens `/tv`. A display can only watch:
it cannot start, stop or change anything.

Deleting a link logs that screen out at once. The screen then says "This
screen link was removed".

## Modes

- **Video.** Full-screen video when the track comes from YouTube; the cover
  otherwise.
- **Visualizer.** MilkDrop-style visuals that change preset every 30 seconds.
  Tap the screen for the next one.
- **Cover and lyrics.** Large cover art, title, artist and the rooms playing.

Lyrics appear only when "lyrics" is ticked on the link. They come from LRCLIB,
and the person who ticks the box accepts the same LRCLIB notice as in the
normal lyrics window.

The visualizer uses the best source it has:

1. **Exact.** For audio that passes through Sonora Home (YouTube today), the
   server analyses the sound with ffmpeg and sends the page small waveform
   frames in time with the speakers. The container image includes ffmpeg. If
   ffmpeg is missing, Sonora Home falls back to the next choice.
2. **Microphone.** For Spotify and library tracks, tap "Use microphone" and
   the page listens to the room. Browsers allow the microphone only on
   **HTTPS** (or localhost), so reach Sonora Home through an HTTPS address.
3. **Generic.** Otherwise a gentle animation that moves to a steady pulse.

## Samsung TVs

Sonora Home can open TV mode on a Samsung smart TV by itself (2016 and later,
on the same network).

1. In the TV settings, **add a TV**: a name, its IP address, and optionally its
   MAC address. With the MAC, Sonora Home wakes the TV (Wake-on-LAN) before
   opening the page. Give the TV a fixed IP in your router.
2. **Pair** it. The TV shows an "Allow" prompt: accept it once on the remote.
   Sonora Home keeps the token it gets, so the prompt does not come back.
3. Use **Show on TV** on a party (pick the TV and a mode), or "Show now" in the
   TV settings to test. Sonora Home makes a display link for that TV and opens
   the TV's browser on it. A preset can also name a TV and mode, and shows the
   party there after it starts.

If the TV is off and unreachable, the app says so within about five seconds
("TV unreachable"). If it was never paired, it says it needs pairing.

### Manual fallback

For any other TV, or if control does not work: create a display link, open the
TV's web browser, type the address, and bookmark it. Opening the bookmark is
all it takes after that.

## Home Assistant

Show the page in a dashboard with a webpage card and a display link:

```yaml
type: iframe
url: "https://sonora.example/auth/display?token=PASTE_DISPLAY_TOKEN"
aspect_ratio: 56%
```

If Home Assistant is served over HTTPS, the display link must be HTTPS too
(browsers block mixed content).

To open a party on a Samsung TV from an automation, call the API with the same
bearer token as in [home-assistant.md](home-assistant.md):

```yaml
rest_command:
  sonora_show_on_tv:
    url: "https://sonora.example/api/ha/parties/{{ party_id }}/tv/{{ tv_id }}"
    method: post
    headers:
      Authorization: !secret sonora_token_header   # "Bearer <token>"
```

`party_id` comes from `sensor.sonora_state`; `tv_id` is the TV's id in the TV
settings. The call wakes the TV if it has a MAC, then opens TV mode on it.
Errors are JSON `{error}`: `tv_unreachable` (504), `tv_needs_pairing`.
