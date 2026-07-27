# CLAUDE.md — Spotify Matrix

Project notes for spinning the current Spotify album art as a vinyl record on an RGB LED matrix.

## What this project is

Single-file Python app (`spotify_matrix.py`, ~665 lines) that:
- Polls Spotify's Web API `currently-playing` endpoint (OAuth Authorization Code flow).
- Crops the album art into a circular disk and spins it while playback is active; freezes the angle when paused.
- Drives a HUB75 RGB LED matrix via the `hzeller/rpi-rgb-led-matrix` bindings on a Raspberry Pi.

Uses the Web API, **not** the browser-only Web Playback SDK. First run does OAuth and caches a refresh token at `.cache/spotify_token.json`.

## Architecture (all in `spotify_matrix.py`)

- **HTTP layer** (`http_request`, `HttpResponse`) — built on stdlib `urllib`; no `requests` for OAuth/API calls. `requests` is lazy-imported only in `download_image`.
- **`SpotifyClient`** — OAuth, token caching, auto-refresh on 401, 429 backoff via `Retry-After`, 204 = nothing playing.
- **`LocalCallbackServer`** — throwaway `HTTPServer` on the redirect URI to catch the OAuth code, with CSRF `state` check.
- **Display abstraction** — `MatrixDisplay` (real hardware, lazy-imports `rgbmatrix`) vs `MockDisplay` (writes a PNG). Same `show()`/`clear()` interface.
- **Rendering** — `render_record` (fit → rotate → circular mask → label + center hole), plus `render_idle`, `render_test_pattern`, `demo_album_art`.
- **Concurrency** — a daemon poll thread updates `SharedPlaybackState` (art + `status`) behind a `Lock`; the main thread renders at `--fps`. Image download happens **outside** the lock so slow network never stalls the animation.

## My hardware

- **Pi:** Raspberry Pi Zero 2 W
- **HAT:** Adafruit RGB Matrix Bonnet (adafruit.com/product/3211)
- **Panel:** RGB LED Matrix Panel, **32×32**, 6mm pitch (1/16 scan)

## Soldering decisions (IMPORTANT — differs from typical tutorials)

Tutorials often show a **64×64** panel, which needs two solders. For a **32×32** panel the situation is different:

| Solder | Needed for my 32×32? | Notes |
|---|---|---|
| **E → 8** (address line) | **NO** | The E line only exists for 64×64 (1/32 scan). A 32×32 panel is 1/16 scan and uses address lines A–D only, which the Bonnet already wires. Skip it. |
| **GPIO 4 → 18** (hardware PWM) | **Optional** | Panel-independent flicker/quality mod. Lets the library drive Output-Enable from hardware PWM (on GPIO 18) instead of software bit-banging. Not required to work. |

**Decision: run solder-free for now.** Neither solder is required to light up the panel. The 4→18 mod is a permanent, "polish" upgrade — only do it if flicker still bugs me after tuning `--gpio-slowdown` and `--brightness`.

### Pi Zero 2 W caveat for the 4→18 mod
GPIO 18 is shared with the Pi's onboard audio. If I ever do the 4→18 solder:
1. Disable audio: `dtparam=audio=off` in `/boot/firmware/config.txt`.
2. Switch to `--hardware-mapping adafruit-hat-pwm`.
3. Drop `--no-hardware-pulse`.

Otherwise onboard audio and the PWM peripheral fight over GPIO 18.

## Running

The script **defaults to 64×64** (`build_parser()`), so I must override `--rows`/`--cols` for my 32×32 panel. Render size = `min(rows, cols)`; everything scales off it.

Solder-free command for my setup:

```bash
sudo -E .venv/bin/python spotify_matrix.py \
  --rows 32 --cols 32 \
  --chain-length 1 --parallel 1 \
  --gpio-slowdown 4 \
  --no-hardware-pulse \
  --hardware-mapping adafruit-hat
```

### Tuning knobs (try before soldering)
- `--gpio-slowdown` (default 2) — bump `4`→`5` if the image is glitchy/noisy. Signal integrity on the Zero 2 W is the most common culprit, not PWM timing.
- `--brightness` (default 65)
- `--rpm` (spin speed, default 20), `--fps` (default 120)
- `--poll-seconds` (default 2) — poll cadence while playing = worst-case lag before a **skip** appears. `--idle-poll-seconds` (default 5) — cadence when idle/paused = worst-case lag before **pressing play** appears. `--max-requests-per-minute` (default 45) — the hard ceiling; see *Spotify polling & rate limits* below.
- `--no-status-dot` — hide the idle corner LED (red = network unreachable, blue = rate-limited).

### Testing without hardware
- `--self-test` — assert status-dot placement (for all four `--rotate` values), poll pacing and the request budget. No creds, no hardware, no test framework. **Run this after touching polling or the dot.**
- `--mock-output frame.png --once` — render one frame to a PNG.
- `--preview-frames dir/` — sample spinning-disk frames, `idle.png`, and `status-offline.png` / `status-rate-limited.png` filmstrips. Pass `--rotate` too: the status strips are rotated the way the panel sees them, so they show which physical corner the dot lands in.
- `--preview-commute dir/` — every commute screen state (comfortable, hurry, now, delayed, cancelled, empty, stale). Credential-free; Stage 1 of the commute feature renders from hand-built `Departure` objects, so this works before any RTT token exists.
- `--test-pattern` — moving color bars on real hardware.
- `--auth-only` — do the OAuth flow and cache the token, then exit.

## Spotify setup
- Redirect URI must be allowlisted **exactly** as `http://127.0.0.1:8888/callback` in the Spotify developer dashboard.
- Headless Pi: forward the callback port from a machine with a browser: `ssh -L 8888:127.0.0.1:8888 pi@raspberrypi.local`, run the script on the Pi, open the printed auth URL locally.
- Credentials live in `.env` (gitignored); template in `.env.example`.

## Auto-start on boot (systemd)
Unit file: `spotify-matrix.service` (in the repo). Runs as root (needs GPIO + `drop_privileges=False`), waits for network, restarts on failure, and stops with SIGINT so the panel clears cleanly.

Install once (symlinked so editing the repo copy edits the installed unit — no re-copy needed):
```bash
sudo ln -sf /home/nova/spotify-matrix/spotify-matrix.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spotify-matrix.service
```
Verified: survives reboot and auto-starts.

### Picking up changes
- **Edited the Python (`spotify_matrix.py`) or `.env`:** `sudo systemctl restart spotify-matrix.service` (no daemon-reload).
- **Edited the unit file:** `sudo systemctl daemon-reload && sudo systemctl restart spotify-matrix.service` (symlinked, so no re-copy).
- **Debugging code:** stop the service and run in the foreground for live output — `sudo systemctl stop spotify-matrix.service` then the manual run command; `systemctl start` when done. (Two processes can't share the matrix GPIO.)
- Logs: `journalctl -u spotify-matrix.service -f`

## Animation features (added after initial commit)
- **Turntable spin model:** the main loop eases an angular `spin_velocity` toward a target (full speed when playing, 0 when paused) via exponential decay, so the record spins up on play and coasts to a halt on pause. Tunable with `--spin-lag`.
- **Random song-change transitions:** 8 transition classes (`Crossfade`, `PixelDissolve`, `Iris`, `RecordSwap`, `FlipSide`, `SpinWhip`, `TonearmSweep`, `ScratchGlitch`) with a uniform `__init__(old_frame, new_frame, size)` + `__call__(t)->frame` interface. The loop detects an `art_key` change, freezes the spin, plays a random transition (`pick_transition` avoids immediate repeats) for `--transition-seconds` (default 1.0), then spins the new art up. `--no-transitions` restores the instant swap. PIL-only (no numpy). Preview all 8 locally with `--preview-transitions DIR` (writes filmstrips + GIFs, credential-free).
- **Idle analog clock:** after `--idle-clock-seconds` (default 60) of nothing playing, the idle ghost ring (`render_idle`) fades over ~1s into a dim analog clock (`render_clock`, hour+minute hands, no font). `--no-idle-clock` disables it. The loop tracks `idle_since` (monotonic) and `last_idle_frame` (so resuming playback transitions out from whatever idle showed — ring or clock). **Uses the Pi's system timezone** — if the time is wrong, `sudo timedatectl set-timezone Europe/London`.

## Commute screen

Shows when to leave for an Elizabeth line train from Custom House platform B,
on weekday mornings. `tinyfont.py` (font + seven-segment digits) and
`trains.py` (`Departure` model, pure selection rules, `RttClient`) back
`render_commute()`.

**Window:** weekdays 06:30-09:30 by default (`--commute-days`,
`--commute-start`, `--commute-end`). Idle inside the window shows trains
instead of the clock; while music plays the panel alternates 20s record / 8s
trains with a cross-fade. Outside the window nothing changes. `--no-commute`
disables it, and it stays off entirely if `RTT_TOKEN` is unset - the panel is a
music display first.

**A second poll thread** mirrors `poll_spotify`, dormant outside the window,
with its own `RequestBudget` (`--rtt-requests-per-minute`, default 20 against a
30/min free tier). 60s cadence, tightening to 20s within 5 minutes of a
departure. Filtering to one platform happens in the poll thread so exactly one
place knows about it.

**Check it end to end:** `--commute-once` fetches live, prints the departures
it parsed, and renders a single frame. That is the fastest way to tell a
rendering problem from a data problem.

**Why the font is variable-width:** at a fixed 3px, `N` renders identically to
`M`, and `M`/`W` collapse into `N`/`U`. `N` gets 4 columns for its diagonal and
`W` gets 5. Confirmed by rendering, not assumed — see `--self-test font`.

**Why `1` is special-cased in the seven-segment digits:** a true seven-segment
`1` lights the two right-hand bars, so `11` reads as four evenly spaced posts.
It is drawn as a single stem with a narrow advance instead.

**Red means cancelled, never "hurry".** Urgency tops out at amber so red always
carries exactly one meaning.

**Unreachable trains are not options (`trains.in_reach`).** A train leaving
sooner than the walk time is filtered out before anything else looks at the
list. Without this the hero counts down to a train it cannot reach, clamps to
zero and shows `LEAVE NOW` - and since the Elizabeth line runs every ~5 minutes
there is always such a train, so at a 15-minute walk it showed `LEAVE NOW`
permanently and never gave a real countdown. All 29 checks passed while this
was broken; only live data exposed it. Cancellations deliberately survive the
filter.

**Walk time is 15 minutes** (`DEFAULT_WALK_MINUTES`) to Custom House. Every
countdown is wrong by exactly the error in this number, and it also decides
which trains count as reachable at all.

**RTT credentials:** `RTT_TOKEN` in `.env` is a *refresh* token. Exchange it at
`GET https://data.rtt.io/api/get_access_token` (Bearer) for a ~1h access token,
then call `GET /rtt/location?code=gb-nr:CUS`. Verified working against live
data on 2026-07-27.

## Spotify polling & rate limits (429 bans)

Spotify rate-limits the Web API per `client_id` over a **rolling 30-second window**. Exact numbers are undocumented (community estimate ~180 req/min in *Development Mode*, stricter in practice), and **bans escalate** — a repeat offender can get a 429 with a multi-hour `Retry-After` (observed once: **27432s ≈ 7.6h**). The ban is server-side on the `client_id`, so **rebooting/restarting does not clear it**.

**How the app stays under the limit (`poll_spotify` + `SpotifyClient`):**
- **Non-blocking 429 backoff.** On a 429 the client records a `rate_limited_until` deadline and returns immediately (it does **not** `time.sleep(Retry-After)` — that used to freeze the poll thread, and the whole display, for the entire ban). Subsequent polls short-circuit (no request) until the deadline, then auto-recover. A 429 is logged: `Spotify rate-limited (429); backing off Ns`.
- **Bounded 401 retry.** `get_currently_playing(refresh_retry=…)` refreshes + retries **once**; if still 401 it backs off 30s instead of recursing. The old unbounded `return self.get_currently_playing()` recursion was a request-storm risk (a prime way to *earn* a multi-hour ban).
- **Token-bucket ceiling (`RequestBudget`).** This is the actual rate-limit control. It refills at `--max-requests-per-minute` (default 45) and the poll thread takes a token before every request. Volume is capped no matter what the pacing logic asks for, so a bug that tightens the cadence to zero still can't earn a ban. During a 429 back-off no request is made, so no token is spent either.
- **Tiered cadence (`compute_poll_delay`).** Three tiers: `HOT_POLL_SECONDS` (1s) for `HOT_WINDOW_SECONDS` (15s) after any observed change, else `--poll-seconds` (2s) while playing, else `--idle-poll-seconds` (5s). The hot tier exists because skipping is bursty — the first skip costs 2s, every skip after it lands in ~1s.

> **Superseded design — don't reintroduce it.** Commit `384e262` made polling *predictive*: while playing it waited until the track boundary computed from `progress_ms`/`duration_ms`. That is optimal for a track ending by itself and pessimal for a manual skip — it assumes the art can't change before the boundary, which is exactly wrong when you hit next. Both "starting playback takes 30s to show" and "skips take 30s to show" came from this. It was removed in favour of the bucket, which holds volume down *without* trading away latency.

**If album art is stuck for a long time:** look at the **idle corner LED** first — blue means rate-limited, red means the network is unreachable. Then confirm in `journalctl -u spotify-matrix.service` (`rate-limited (429)` or `unreachable (…)`). If banned, wait out the `Retry-After`; the display keeps rendering the idle clock and recovers automatically. The long-term fix is **Extended Quota Mode** (request it in the Spotify developer dashboard — much higher limit).

## Idle status LED

A single pixel in the panel's **physical top-right**, drawn only on idle screens (ghost ring + clock) so it never sits over album art:

| State | Colour | Rhythm | Meaning |
|---|---|---|---|
| `STATUS_OFFLINE` | red `(210,35,35)` | 1 Hz pulse, 35% duty | Can't reach Spotify at all — check WiFi/router |
| `STATUS_RATE_LIMITED` | blue `(45,110,255)` | 4 s breathe, never fully dark | 429 back-off; wait it out |

Rhythm carries as much of the signal as colour does — one pixel's hue is hard to read across a dark room, and the motion also stops it looking like a dead pixel.

**Detection:** offline = `OFFLINE_STRIKES` (2) consecutive `OSError`s from the poll. That's a clean connectivity probe because `http_request` turns HTTP *error responses* into `HttpResponse` objects, so the only exceptions that escape are transport-level. Rate-limited = `SpotifyClient.backoff_remaining() > 0`. Offline wins if somehow both.

**GOTCHA — rotation.** `MatrixDisplay.show()` applies `--rotate` *after* rendering, so a pixel drawn at the rendered frame's top-right does **not** reach the panel's top-right. `status_dot_position()` inverts the rotation to pick the right source corner — at our `--rotate 90` the dot is drawn at the frame's top-**left**. `--self-test` round-trips all four rotations; run it if you touch this.

**Behaviour change this required:** after `OFFLINE_IDLE_SECONDS` (60) of consecutive failures the poller clears playback state, so the display drops to idle instead of spinning stale art forever. Without that the red dot would be unreachable in its main scenario (WiFi dying mid-playback). Outages shorter than 60s still ride through with the record spinning.

## Local fixes to the script (differ from the initial commit)

- **`--test-pattern` and `--preview-frames` no longer require Spotify credentials.** The credential check in `run()` used to run *before* the test-pattern branch, so a hardware-only test still failed with "Missing required environment values". Fixed by moving the no-Spotify modes to the top of `run()` and extracting a `build_display(args)` helper. Now you can verify the panel before doing any Spotify setup.
- **`--preview-frames` now honors `--rows`/`--cols`.** It was hardcoded to render at 64px, ignoring the panel size. Now it renders at `min(rows, cols)` and also writes an `idle.png`. Use `--rows 32 --cols 32 --preview-frames dir/` to preview the real 32x32 output. (Tip for eyeballing: upscale the PNGs with nearest-neighbor so each LED pixel is visible.)
- **Record label/hole radii are now proportional to panel size.** `render_record` used fixed floors (`max(5, size//11)`, `max(2, size//25)`) tuned for 64x64, which made the center label eat ~30% of a 32x32 disc. Changed to `size // 7` (label) and `size // 16` (hole) so the disc looks right at any resolution and more album art shows on the 32x32.
- **`MatrixDisplay` sets `options.drop_privileges = False`.** By default `rpi-rgb-led-matrix` drops root to user `daemon` right after `RGBMatrix()` init. That silently broke the poll thread's album-art download — `requests.get` returned unusable bytes, surfacing as `UnidentifiedImageError: cannot identify image file` (looked like an image/Pillow bug, was actually a privilege drop). Confirmed by bisection: `download_image` works alone but fails if called after `MatrixDisplay()` init. We run with `sudo` intentionally, so keeping root is correct. **If album art fails to load only when the matrix is running, this is the first thing to check.**

## Pi install note

### Quick path: `setup.sh`
`bash setup.sh` (run as the normal user, from the project dir) does everything below: apt deps, the `.venv` (`--system-site-packages`) + `pip install -r requirements.txt`, `.env` from template, optional swap bump on low RAM, and the Adafruit bindings installer. Idempotent — safe to re-run. The Adafruit installer is interactive and reboots at the end. The manual steps below are the same thing, for reference/debugging.

### Installing the `rgbmatrix` bindings (Adafruit Bonnet)
Adafruit replaced the old `rgb-matrix.sh` shell script (now a **404**) with a **Python installer, `rgb-matrix.py`**, which depends on the `adafruit-python-shell` package. Current method (Raspberry Pi OS Bookworm):

```bash
cd ~
sudo apt-get update
sudo apt-get install -y git python3-dev python3-pip python3-pillow cython3 python3-setuptools cmake unzip curl
sudo pip3 install --break-system-packages adafruit-python-shell
git clone https://github.com/adafruit/Raspberry-Pi-Installer-Scripts.git
cd Raspberry-Pi-Installer-Scripts
sudo python3 rgb-matrix.py
```

Installer prompts:
- **Interface board** → Adafruit RGB Matrix Bonnet
- **Quality vs convenience** → **convenience** (keeps sound, software pulsing — matches our no-solder + `--no-hardware-pulse` plan)
- **CPU isolation** (Zero 2 W is quad-core, so this appears) → optional; reserving a core = steadier display
- Reboot when it asks.

`--break-system-packages` is required because Bookworm enforces PEP 668. Only `adafruit_shell` goes system-wide (the installer runs as root outside the venv); project deps stay in `.venv`.

### GOTCHA: the installer isolates `rgbmatrix` in its OWN venv
The current `rgb-matrix.py` does **not** install `rgbmatrix` system-wide (the old `.sh` did). It builds into its private venv at `~/Raspberry-Pi-Installer-Scripts/env`. So a `--system-site-packages` `.venv` does **not** inherit it — `import rgbmatrix` fails in our venv even though the install "succeeded".

Fix: copy the compiled package from the Adafruit env into our `.venv`. Both are the same CPython version (3.13) on the same machine, so the binary is ABI-compatible — no rebuild needed (and rebuilding via `pip install ~/.../rpi-rgb-led-matrix/` would re-trigger the heavy, OOM-prone C++ compile for an identical result):
```bash
cp -r ~/Raspberry-Pi-Installer-Scripts/env/lib/python3.13/site-packages/rgbmatrix* \
      ~/spotify-matrix/.venv/lib/python3.13/site-packages/
.venv/bin/python -c "import rgbmatrix; print('rgbmatrix OK in .venv')"
```
`setup.sh` step 4 automates this: it copies from the Adafruit env if present, else runs the installer (which reboots) and asks you to re-run `setup.sh` to do the copy. The hzeller extension links the C++ lib statically into `core.*.so`, so copying just the package dir is self-contained.

Note: the `pyproject.toml` is at the **repo root** of `rpi-rgb-led-matrix` (not `bindings/python/`), so a from-source install would be `pip install ~/Raspberry-Pi-Installer-Scripts/rpi-rgb-led-matrix/`.

### Confirmed working config (Pi Zero 2 W + Bonnet + 32x32)
Test pattern lit up clean with `--gpio-slowdown 5` (the installer's recommended value for this board), `--no-hardware-pulse`, `--hardware-mapping adafruit-hat`.

### Low-memory builds
The `rgbmatrix` wheel compile is the heaviest step and OOMs on the Zero 2 W (~415Mi RAM). Symptom: SSH drops mid-build ("Connection reset by peer") — the Pi thrashed and starved the network.

This Pi uses **zram** (compressed in-RAM swap), not `dphys-swapfile` (which isn't installed — no `/etc/dphys-swapfile`). zram doesn't add real capacity, so add a **disk-backed swapfile**:
```bash
sudo fallocate -l 1G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=1024
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
```
Non-persistent (not in `/etc/fstab`) — only needed for the build. Reclaim after: `sudo swapoff /swapfile && sudo rm /swapfile`.

**Also run the installer under `tmux`** so a WiFi drop can't kill the build:
```bash
sudo apt install -y tmux && tmux new -s matrix
# inside: cd ~/Raspberry-Pi-Installer-Scripts && sudo python3 rgb-matrix.py
# detach: Ctrl-b then d   |   reattach: tmux attach -t matrix
```
