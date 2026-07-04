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
- **Concurrency** — a daemon poll thread updates `SharedPlaybackState` behind a `Lock` every `--poll-seconds`; the main thread renders at `--fps`. Image download happens **outside** the lock so slow network never stalls the animation.

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
- `--rpm` (spin speed, default 20), `--fps` (default 20), `--poll-seconds` (default 2)

### Testing without hardware
- `--mock-output frame.png --once` — render one frame to a PNG.
- `--preview-frames dir/` — four sample spinning-disk frames.
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
