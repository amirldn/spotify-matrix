# Low-latency polling and an idle status indicator

Date: 2026-07-27

## Problem

Two user-visible latency complaints and one visibility gap.

1. **Starting playback takes up to 30s to show on the panel.** When nothing is
   playing, `compute_poll_delay` returns `--idle-poll-seconds` (30s). Pressing
   play is invisible to the poller until that timer expires.
2. **Skipping a track takes up to 30s to show.** While playing,
   `compute_poll_delay` predicts the track boundary from
   `progress_ms`/`duration_ms` and waits until then, capped at
   `--idle-poll-seconds`. That is optimal for a track ending on its own and
   pessimal for a manual skip: the poller has explicitly decided nothing can
   change before the boundary, which is exactly wrong when the user intervenes.
3. **Faults are silent.** A Spotify 429 ban (which can carry a multi-hour
   `Retry-After`) and a WiFi dropout both present identically: the display just
   stops updating. There is no way to tell "banned, wait it out" from "network
   down, go fix the router" without SSHing in and reading the journal.

The slow cadence exists to avoid re-earning a 429 ban. That trade is
mispriced: the documented 7.6-hour ban was caused by the unbounded 401 retry
recursion (a request storm), which commit `384e262` already fixed. Steady
polling at 12-30 req/min sits far below the community-estimated ~180 req/min
limit.

## Goals

- Playback start visible within ~5s, a skip within ~2s, consecutive skips
  within ~1s.
- Rate-limit safety that is *enforced*, not merely hoped for.
- A single-pixel status indicator on the idle screen: red for no network, blue
  for rate-limited.

## Non-goals

- Extended Quota Mode (a Spotify dashboard action, not a code change).
- Prefetching the next track's art via `/me/player/queue`. It would make
  unattended track changes zero-latency, but it does not help manual skips
  (the queue changes underneath you) and art download is not the bottleneck.
- Indicating any state other than offline and rate-limited. No auth-failure or
  healthy-state indicator.

## Design

### 1. Poll pacing: tiered cadence

Delete the predictive-to-boundary branch. Replace `compute_poll_delay` with a
three-tier function of playback state and time since the last observed change:

| Tier | Condition | Cadence |
|---|---|---|
| Hot | Something changed < `HOT_WINDOW_SECONDS` (15s) ago | `HOT_POLL_SECONDS` (1.0s) |
| Playing | `is_playing` | `--poll-seconds` (new default 2.0s) |
| Idle | Nothing playing, or paused | `--idle-poll-seconds` (new default 5.0s) |

"Something changed" means the art key changed or `is_playing` flipped. The hot
tier exists because track changes are bursty: a user who skips once usually
skips again within seconds. First skip costs one `--poll-seconds` interval,
subsequent ones ~1s.

Dropping prediction also removes `--idle-poll-seconds`' confusing second job as
the cap on the playing cadence. It now means one thing.

### 2. Rate-limit control: a token-bucket ceiling

New `RequestBudget`: a token bucket refilling at `--max-requests-per-minute`
(default 45), with capacity equal to one minute's worth. The poll thread waits
for a token before every Spotify request and consumes it when the request goes
out.

This is the substantive change to how rate limiting is controlled. A slow
cadence is an *indirect* limit that couples request volume to user-visible
latency; a token bucket is a *direct* one that decouples them. Request volume
is physically capped regardless of what the pacing logic asks for, so the
pacing logic is free to be aggressive.

Steady state: 30 req/min playing, 12 req/min idle, hot-window bursts of 60
req/min clamped to 45 by the bucket.

Interaction with the existing 429 backoff: during a backoff window no request
is made, so no token is consumed. `SpotifyClient.backoff_remaining()` exposes
the deadline so the poll loop can skip both the token and the request.

The existing non-blocking 429 handling and bounded 401 retry are unchanged.

### 3. Status detection

`SharedPlaybackState` gains one field:

```python
status: str = "ok"   # "ok" | "offline" | "rate_limited"
```

Written by the poll thread under the existing lock, read by the render loop
alongside the rest of the playback state. One field, one meaning.

- **offline** — set after `OFFLINE_STRIKES` (2) consecutive network-level
  failures. `http_request` converts HTTP error responses into `HttpResponse`
  objects, so an exception escaping it genuinely means "could not reach
  Spotify". Cleared on the first success.
- **rate_limited** — `backoff_remaining() > 0`.
- Precedence: offline wins. If the network is down, the rate-limit state is
  unknowable.

**Required behaviour change: stale playback expiry.** Today a mid-playback
network failure leaves `SharedPlaybackState` untouched, so the record spins on
stale art indefinitely and the display never returns to idle. With an
idle-only indicator, the red dot would then be unreachable in its primary
scenario. So after `OFFLINE_IDLE_SECONDS` (60) of consecutive failures the
poller clears playback state; the display falls to the idle screen and the red
dot appears. Failures shorter than 60s ride through with the record still
spinning, as today.

### 4. The dot

`draw_status_dot(frame, status, elapsed, rotate)` lights a single pixel in the
corner that reads as top-right on the *physical* panel.

`MatrixDisplay.show()` applies `rotate_frame(image, --rotate)` after rendering,
so a pixel drawn at the rendered frame's top-right does not land at the
panel's top-right. The source corner is chosen by inverting the rotation:

| `--rotate` | source pixel (N = frame size) |
|---|---|
| 0 | (N-1, 0) — top-right |
| 90 | (0, 0) — top-left |
| 180 | (0, N-1) — bottom-left |
| 270 | (N-1, N-1) — bottom-right |

The service runs at `--rotate 90`, so the dot is drawn at the rendered frame's
top-*left*. This is easy to get backwards and is verified by a rotation
round-trip check.

Rhythms are driven from a monotonic clock, so they are framerate-independent:

- **offline (red)** — 1 Hz pulse, ~35% duty with soft edges, peak
  `(210, 35, 35)`, fully dark between pulses. Reads as an alarm.
- **rate_limited (blue)** — 4s sine breathe, peak `(45, 110, 255)`, floor 25%
  so it never fully extinguishes. Reads as waiting.

Distinct rhythm plus distinct hue means the two states are separable across a
dark room, and the motion distinguishes a status dot from a stuck pixel.

Peak levels sit just above the clock's brightest element `(155, 155, 175)`, so
the dot is legible without dominating a dim idle screen.

**Placement in the render loop:** applied only in the idle branch, after
`last_idle_frame` is assigned. Transitions into and out of idle therefore use a
dot-free frame, and the dot never overlays album art.

### CLI changes

| Flag | Change |
|---|---|
| `--poll-seconds` | default 5.0 → 2.0; help text drops the "floor for predictive polling" language |
| `--idle-poll-seconds` | default 30.0 → 5.0; help text drops the "cap on the playing cadence" role |
| `--max-requests-per-minute` | new, default 45.0 |
| `--no-status-dot` | new, disables the indicator |

`HOT_POLL_SECONDS`, `HOT_WINDOW_SECONDS`, `OFFLINE_STRIKES` and
`OFFLINE_IDLE_SECONDS` stay as module constants. They describe behaviour, not
tuning, and every extra flag is surface area on a single-file hobby project.

## Verification

The repo has no test framework and a single-file layout; verification is
proportionate to that.

1. **Rotation round-trip.** For each of 0/90/180/270, draw the dot on a blank
   frame, apply `rotate_frame`, and assert the lit pixel is at `(N-1, 0)` and
   that exactly one pixel is lit.
2. **Pacing table.** Assert `compute_poll_delay` returns the expected cadence
   for each tier, including that a skip mid-track no longer returns a
   track-boundary-sized delay.
3. **Token bucket.** Assert the bucket hands out at most `per_minute` tokens in
   a simulated minute and reports a non-zero wait once drained.
4. **Visual.** `--preview-frames` gains `status-offline.png` and
   `status-rate-limited.png`, rendered at several phases of each rhythm, so the
   dot can be eyeballed without hardware or Spotify credentials.
5. **On hardware.** Deploy to the Pi, confirm the dot lands in the physical
   top-right at `--rotate 90`, and confirm play/skip latency by observation.

## Risks

- **Higher steady request volume** (12-30 req/min vs ~2-12 today). Mitigated by
  the token-bucket ceiling and by the fact that this is still ~4x below the
  estimated limit. If a 429 does occur, the existing non-blocking backoff
  handles it and the blue dot now makes it visible.
- **Stale playback expiry changes behaviour.** A >60s network blip now returns
  the display to idle instead of spinning stale art. This is intentional and
  is what makes the red dot reachable.
