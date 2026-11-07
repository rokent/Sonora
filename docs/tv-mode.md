# TV mode

TV mode is a full-screen page for a party. It follows what the party is
playing and shows it on a TV, a tablet on the wall, or a Home Assistant
dashboard. It needs no login on the screen itself.

## Display links

A display link is a private address for one screen. A signed-in, named user
creates one per screen ("Display links" on the parties page):

- a name (for example "Living room TV");
- which party to follow: any party, or one in particular. With "any party",
  the screen shows "No party playing" when the party ends and picks up the
  next one by itself;
- the mode (below);
- whether to show lyrics.

The link is shown once, as a URL and a QR code. Opening it signs that screen in
as a read-only display for a year and opens `/tv`. A display can only watch:
it cannot start, stop or change anything.
The link's token is a secret: Sonora Home writes it into its access log as
`token=REDACTED`.

Deleting a link ends that screen's session: its next request is refused. The
TV page checks its link every 60 seconds, so within a minute the screen says
"This screen link was removed".

## Modes

- **Video.** Full-screen video when the track comes from YouTube; the cover
  otherwise. If a speaker will not play YouTube items at all, set
  `SONORA_YOUTUBE_FORMAT=aac` (see [youtube.md](youtube.md)).
- **Visualizer.** MilkDrop-style visuals that change preset every 30 seconds.
  Tap the screen for the next one.
- **Cover.** Large cover art, title, artist and the rooms playing, with
  lyrics beside them when the link allows it.

Lyrics appear only when "lyrics" is ticked on the link. They come from LRCLIB,
and the person who ticks the box accepts the same LRCLIB notice as in the
normal lyrics window.

The visualizer uses the best source it has:

1. **Exact.** For audio that passes through Sonora Home (YouTube items, and
   live sound: AirPlay, the Windows sound card, a browser tab), the server
   analyses the sound with ffmpeg and sends the page waveform frames over
   `/ws/visual`: about 700 bytes of JSON each (512 samples, base64), up to 30
   a second, roughly 20 KB/s per screen. For YouTube the frames are matched to
   the speakers' track position. Live sound has no track position, so its
   frames are drawn a fixed 3 seconds after they arrive (about what a speaker
   buffers); if the picture runs early or late, move the video offset slider
   on a party's page on the same device, which shifts that delay. The
   container image includes ffmpeg. If ffmpeg is missing, Sonora Home falls
   back to the next choice.
2. **Microphone.** For Spotify and library tracks, tap "Use microphone" and
   the page listens to the room. Browsers allow the microphone only on
   **HTTPS** (or localhost), so reach Sonora Home through an HTTPS address.
3. **Generic.** Otherwise a gentle animation that moves to a steady pulse.

## Samsung TVs

Sonora Home can open TV mode on a Samsung smart TV by itself (2016 and later,
on the same network).

1. Under **TVs** on the parties page, **add a TV**: a name, its IP address, and optionally its
   MAC address. With the MAC, Sonora Home wakes the TV (Wake-on-LAN) before
   opening the page. Give the TV a fixed IP in your router.
2. **Pair** it. The TV shows an "Allow" prompt: accept it once on the remote.
   Sonora Home keeps the token it gets, so the prompt does not come back.
3. Use **Show on TV** on a party (pick the TV and a mode), or "Show now" in the
   TV list to test. Sonora Home makes a display link for that TV and opens
   the TV's browser on it. A preset can also name a TV and mode, and shows the
   party there after it starts.

If the TV does not answer, the app says "The TV did not answer. Check that it
is on and on the same network." Without a MAC address that takes about five
seconds; with one, Sonora Home first wakes the TV and keeps trying for about
15 seconds more, so up to about 20 seconds. If it was never paired (or the
prompt was refused), it says "The TV needs to be paired. Press Pair and allow
it on the TV."

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

The card's page signs in with a cookie, and a page framed by another site
only gets its cookies when they are marked `SameSite=None; Secure`. Sonora
Home does that for display sessions opened over **HTTPS**, so the card works
whatever site Home Assistant is on. Over plain **HTTP** the cookie stays
`SameSite=Lax` (browsers refuse `None` without HTTPS), and the card only
works when Home Assistant and Sonora Home are the same site: the same host
name (ports may differ), for example `http://homeserver:8123` and
`http://homeserver:8000`. Otherwise use HTTPS.

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

`party_id` comes from `sensor.sonora_state`. `tv_id` is shown under each TV in
the **TVs** list ("ID for Home Assistant"), and `GET /api/ha/state` lists the
TVs as `"tvs": [{"id", "name"}]`. An optional JSON body `{"mode": "video" |
"visualizer" | "cover"}` overrides the TV's default mode.

The call answers at once, well inside `rest_command`'s 10-second timeout:

- an unknown party or TV is `404 {"error": "not_found"}`, a bad mode is
  `400 {"error": "invalid"}`;
- otherwise `200 {"tv": "pending", "tv_id", "party_id", "mode"}`, and the
  show itself runs in the background. Waking the TV and opening the page can
  take up to about 20 seconds. The outcome is not returned to Home Assistant:
  it is shown in Sonora (a banner on the parties page): the TV showing the
  party, or the error, such as `tv_unreachable` or `tv_needs_pairing` (which
  the TV routes in Sonora answer as 504 and 409).
