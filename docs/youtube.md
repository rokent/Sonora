# YouTube in a party

Guests and household members can search YouTube or paste a link and add the
video's sound to the party queue. Sonora resolves the audio with
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and relays it to the speakers; the
video plays muted in the page and on a TV in TV mode.

## How the sound reaches the speakers

A YouTube queue item points at Sonora, not at YouTube:
`http://<sonora>:<port>/api/home/media/<key>.m4a`. The key is opaque and maps
only to a video id (kept in `home/media.json`, forgotten after a day unused).
Each request from a speaker resolves the current googlevideo URL and relays
it. Nothing is downloaded to disk.

yt-dlp needs two things to get stream URLs from YouTube:

* the `yt-dlp-ejs` package (a Python dependency, pinned to the version the
  installed yt-dlp asks for), which holds the scripts that solve YouTube's
  JavaScript challenges, and
* a JavaScript runtime to run them. The image carries Node; when running
  Sonora outside the image, `node` must be on `PATH`.

## `SONORA_YOUTUBE_FORMAT`

| Value | What the speaker gets | Seeking |
|---|---|---|
| `m4a` (default) | YouTube's m4a file as it is, relayed with HTTP Range | yes |
| `aac` | ffmpeg copies the AAC audio out of the m4a into a raw ADTS stream (`audio/aac`, no re-encode) | no |

YouTube's m4a is fragmented MP4. Most speakers play it, but not every model
or firmware does. **If a speaker will not play YouTube items (they skip, or
stay silent), set `SONORA_YOUTUBE_FORMAT=aac`.** Every Sonos speaker plays
ADTS AAC; the cost is that the track has no length the speaker can seek in.

`aac` needs ffmpeg on `PATH` (the image has it). Without ffmpeg Sonora logs a
warning and uses `m4a`. The setting applies to items added after the change;
items already queued keep the format they were added in.

## Limits

* At most 3 yt-dlp runs at once (searches, adds, the visualizer). A speaker
  asking for a fresh stream URL has one more slot of its own, so a burst of
  searches never holds up playback.
* Each person may search 10 times and add 6 YouTube videos a minute. Past
  that the answer is `429 {"error": "slow_down"}` and the page says to wait a
  few seconds.
* Adding a video resolves it once: the title, length and stream URL come from
  the same yt-dlp run, and two people adding the same video at once share it.

## When YouTube playback breaks

YouTube changes its site often, and yt-dlp releases fixes within days. When
searches or adds start failing with "YouTube did not answer" (and the log
shows yt-dlp errors), update yt-dlp and its challenge solver together:

1. Find the newest yt-dlp release and the `yt-dlp-ejs` version it requires:

   ```sh
   pip index versions yt-dlp
   pip download --no-deps yt-dlp==<new> -d /tmp/ytdlp && \
     unzip -p /tmp/ytdlp/yt_dlp-*.whl '*/METADATA' | grep 'yt-dlp-ejs'
   ```

2. In `app/pyproject.toml`, raise `yt-dlp>=<new>` and set
   `yt-dlp-ejs==<the version printed above>`.

3. Regenerate both locks, from `app/` (the commands are also in each lock's
   header):

   ```sh
   cd app
   uv pip compile pyproject.toml --generate-hashes -o requirements.lock
   uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements-dev.lock
   ```

   To move only these two packages and leave every other pin where it is, add
   `--upgrade-package yt-dlp --upgrade-package yt-dlp-ejs` to both commands.

4. Run the tests (`test_home_youtube.py` checks that the installed
   `yt-dlp-ejs` is the version yt-dlp asks for):

   ```sh
   uv pip sync requirements-dev.lock
   PYTHONPATH=$PWD python -m pytest -q
   ```

5. Rebuild and restart the image:

   ```sh
   docker build -t sonora .
   docker compose up -d
   ```

Dependabot checks the Python dependencies daily and puts yt-dlp and
`yt-dlp-ejs` in a pull request of their own (`.github/dependabot.yml`).
