# Spotify Matrix

Shows the current Spotify album art on an RGB LED matrix as a spinning vinyl record. The album art *is* the record surface: it is cropped to a disk and spins while Spotify reports playback as active. Pause the music and it coasts to a halt like a real turntable; change the song and one of 8 random transitions plays (crossfade, record swap, flip, spin-whip, tonearm sweep, scratch glitch, iris, pixel dissolve). After a minute of nothing playing, the record dims into a small analog clock.

This uses Spotify's Web API `currently-playing` endpoint, not the browser-only Web Playback SDK. The first run opens Spotify OAuth, then the script stores a refresh token in `.cache/spotify_token.json`.

Confirmed working on a **Raspberry Pi Zero 2 W + Adafruit RGB Matrix Bonnet + 32x32 panel**. The panel size is configurable (`--rows`/`--cols`), so 64x64 works too.

## Files

- `spotify_matrix.py` - the runtime script.
- `setup.sh` - one-shot Pi provisioning (deps, venv, bindings). See below.
- `spotify-matrix.service` - systemd unit for auto-start on boot.
- `.env` / `.env.example` - local Spotify credentials (`.env` is gitignored).
- `requirements.txt` - Python deps, excluding the hardware-specific RGB matrix bindings.
- `CLAUDE.md` - detailed setup notes, hardware decisions, and gotchas. **Read this if anything goes wrong.**

## Raspberry Pi setup

The quick path — run from the project directory as your normal user:

```bash
bash setup.sh
```

`setup.sh` installs system deps, creates the `.venv`, installs requirements, seeds `.env`, adds a temporary swapfile for the memory-hungry build, runs Adafruit's `rgb-matrix.py` bindings installer, and copies the compiled `rgbmatrix` package into the project venv. It is idempotent and reboots partway through the bindings install — just re-run it after the reboot to finish.

> **Note:** the current Adafruit installer builds `rgbmatrix` into its *own* private venv, so a `--system-site-packages` venv does **not** inherit it — the bindings must be copied into `.venv`. `setup.sh` handles this. See `CLAUDE.md` for the full explanation and the manual steps.

## Spotify setup

In the [Spotify developer dashboard](https://developer.spotify.com/dashboard), create an app with **Web API** enabled and allowlist this redirect URI **exactly**:

```text
http://127.0.0.1:8888/callback
```

Put the client ID/secret in `.env` (copy `.env.example`). Then authorize. For a headless Pi, forward the callback port from a machine with a browser:

```bash
# on your laptop
ssh -L 8888:127.0.0.1:8888 nova@nova.local

# on the Pi (inside that session)
cd ~/spotify-matrix
sudo -E .venv/bin/python spotify_matrix.py --auth-only --no-browser
```

Open the printed authorization URL in your laptop browser. The callback flows back through the tunnel and the token is cached to `.cache/spotify_token.json`.

## Run

The working command for the Pi Zero 2 W + Bonnet + 32x32 panel:

```bash
sudo -E .venv/bin/python spotify_matrix.py \
  --rows 32 --cols 32 \
  --gpio-slowdown 5 \
  --no-hardware-pulse \
  --hardware-mapping adafruit-hat
```

Notes:
- Defaults are **64x64** — override `--rows`/`--cols` for other panels.
- `--gpio-slowdown` (5 here) is the Bonnet installer's recommended value; lower it if the display is fine, raise it if you see noise/flicker.
- `--no-hardware-pulse` avoids the Pi's onboard-audio conflict (no PWM solder mod).
- Runs as `sudo` on purpose: the matrix needs GPIO, and the app keeps root (`drop_privileges=False`) so the background thread can download album art.

### Verify hardware without Spotify

```bash
# bright moving color bars (no credentials needed)
sudo -E .venv/bin/python spotify_matrix.py --rows 32 --cols 32 \
  --gpio-slowdown 5 --no-hardware-pulse --hardware-mapping adafruit-hat --test-pattern
```

### Preview the render on any machine (no Pi hardware)

```bash
# one PNG frame of the current state
python spotify_matrix.py --rows 32 --cols 32 --mock-output /tmp/frame.png --once

# spinning-disk sample frames, the idle screen, and status-LED filmstrips
# (pass the same --rotate you run with, so the LED shows in the right corner)
python spotify_matrix.py --rows 32 --cols 32 --rotate 90 --preview-frames /tmp/preview

# filmstrips + animated GIFs of all 8 song-change transitions
python spotify_matrix.py --preview-transitions /tmp/transitions

# every morning-commute screen state (no RTT token needed)
python spotify_matrix.py --rows 32 --cols 32 --preview-commute /tmp/commute

# assert status-LED placement, poll pacing and the request budget
python spotify_matrix.py --self-test
```

### Status LED

When nothing is playing, a single pixel in the panel's top-right corner reports why the display is quiet: **red, pulsing once a second** means the network is unreachable, and **blue, slowly breathing** means Spotify is rate-limiting us and the app is waiting out the ban. Nothing lights when all is well, and the LED never appears over album art. `--no-status-dot` turns it off.

### Latency and rate limits

`--poll-seconds` (default 2) is the worst-case delay before a **skip** reaches the panel; `--idle-poll-seconds` (default 5) is the worst-case delay before **pressing play** does. Right after any change the app polls once a second for 15 seconds, so rapid skipping keeps up.

Request volume is capped separately by `--max-requests-per-minute` (default 45), enforced with a token bucket. Because that ceiling is independent of the poll cadence, the cadences above can chase latency without risking a Spotify 429 ban.

Transition behaviour is tunable: `--transition-seconds` (default 1.0) sets the duration, and `--no-transitions` swaps art instantly instead. `--spin-lag` (default 0.5) controls how quickly the record spins up on play and coasts down on pause. `--idle-clock-seconds` (default 60) sets how long nothing must be playing before the dim analog clock appears; `--no-idle-clock` keeps the ghost record instead.

The clock uses the Pi's system time zone. If it shows the wrong time, set the zone: `sudo timedatectl set-timezone Europe/London` (or via `sudo raspi-config` → Localisation).

## Auto-start on boot (systemd)

Install `spotify-matrix.service` so the panel starts on power-up (symlinked, so editing the repo copy edits the installed unit):

```bash
sudo ln -sf /home/nova/spotify-matrix/spotify-matrix.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-matrix.service
```

The service runs as root, waits for the network, restarts on failure, and stops with SIGINT so the panel clears cleanly. Authorize Spotify (above) before enabling it — it relies on the cached token.

## Common commands

Run these from your Mac unless noted. Host is `nova@nova.local`, project at `~/spotify-matrix`.

**Sync a changed file to the Pi and restart the service to pick it up:**

```bash
# copy the code over, then restart in one line
scp spotify_matrix.py nova@nova.local:~/spotify-matrix/ && \
  ssh nova@nova.local 'sudo systemctl restart spotify-matrix.service'

# copy several files
scp spotify_matrix.py spotify-matrix.service CLAUDE.md nova@nova.local:~/spotify-matrix/
```

**Service control (on the Pi, or prefix with `ssh nova@nova.local '<cmd>'`):**

```bash
sudo systemctl restart spotify-matrix.service   # pick up code / .env changes
sudo systemctl status  spotify-matrix.service   # is it running?
sudo systemctl stop    spotify-matrix.service    # stop (e.g. to run in foreground)
sudo systemctl start   spotify-matrix.service    # start again
sudo systemctl disable spotify-matrix.service    # don't start on boot
journalctl -u spotify-matrix.service -f          # follow live logs
```

**Edited the unit file itself?** Reload systemd, then restart:

```bash
sudo systemctl daemon-reload && sudo systemctl restart spotify-matrix.service
```

**Debug in the foreground** (stop the service first — two processes can't share the matrix GPIO):

```bash
sudo systemctl stop spotify-matrix.service
cd ~/spotify-matrix
sudo -E .venv/bin/python spotify_matrix.py --rows 32 --cols 32 \
  --gpio-slowdown 5 --no-hardware-pulse --hardware-mapping adafruit-hat
# ...Ctrl-C when done...
sudo systemctl start spotify-matrix.service
```

**Re-authorize Spotify** (token expired / revoked):

```bash
# laptop: ssh -L 8888:127.0.0.1:8888 nova@nova.local
sudo systemctl stop spotify-matrix.service
sudo -E .venv/bin/python ~/spotify-matrix/spotify_matrix.py --auth-only --no-browser
sudo systemctl start spotify-matrix.service
```

**Reboot the Pi** (the service auto-starts again):

```bash
ssh nova@nova.local 'sudo reboot'
```
