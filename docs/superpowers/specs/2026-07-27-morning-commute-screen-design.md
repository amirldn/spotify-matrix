# Morning commute screen — Elizabeth line departures from Custom House

Date: 2026-07-27

## Problem

On weekday mornings the panel shows a clock, which is decorative. What's
actually wanted at that hour is one fact: **when to walk out of the door** to
catch an Elizabeth line train from Custom House platform B (towards
Paddington).

The panel is 32×32. A conventional departure board is roughly six rows of
twenty-plus characters, so it cannot be shrunk to fit. The design has to
reframe rather than reduce.

## Goals

- Answer "when do I leave" at a glance from across a room on weekday mornings.
- Degrade honestly: a delay, a cancellation, an empty line-up and stale data
  must each look different from a healthy countdown.
- Cost a negligible share of the RTT free-tier quota.
- Leave the existing Spotify behaviour untouched outside the morning window.

## Non-goals

- Any station or line other than Custom House platform B. The data layer takes
  a station and platform as config, but no multi-station UI.
- Journey planning, arrival times at the destination, or onward connections.
- Showing destinations on the timeline. Every platform B service goes the same
  way, so the destination is redundant and the pixels are not spare.

## Key decisions

### The reframe

The hero screen shows `expected_departure − now − walk_minutes`, not the raw
departure time. One large number, colour-coded, is legible across a room; a
5px-tall timetable is not. This is what makes 32×32 workable.

### Data source: next-generation RTT only

`api.rtt.io` (v1, HTTP Basic) **shuts down 30 September 2026** and
`secure.realtimetrains.co.uk` on 31 March 2027. Every Python wrapper on PyPI
targets v1, so they are actively unhelpful here.

This targets `https://data.rtt.io` with a bearer token from
`api-portal.rtt.io`. Free-tier limits are 30/min, 750/hour, 9000/day,
30000/week.

### Requires a manual step

A next-gen API token must be registered at `api-portal.rtt.io` and placed in
`.env` as `RTT_TOKEN`. This cannot be automated.

## Architecture

The main file is ~1300 lines. This adds roughly 350, so the new concerns get
their own modules rather than growing it further. Existing Spotify, display and
transition code is **not** refactored — it works, and churning it is out of
scope.

### `tinyfont.py`

Rendering primitives with no knowledge of trains or Spotify.

- A **variable-width** bitmap font, 5px tall. Width varies by glyph because
  fixed 3px is not viable: `M` and `W` need 5 columns and `N` needs 4 for its
  diagonal, or they read as `N`, `U` and `M` respectively. This was confirmed
  by rendering, not assumed.
- Seven-segment digits, parametric in width/height/thickness, for the hero
  number. Seven-segment suits an LED matrix and scales without a second font.
  `1` is special-cased to a single stem with a narrow advance, otherwise `11`
  renders as four evenly spaced bars.
- `text()`, `text_width()`, `centred()`, `seven_seg()`, `big_number()`.

### `trains.py`

```python
@dataclass
class Departure:
    scheduled: datetime      # temporalData.departure.scheduleAdvertised
    expected: datetime       # realtimeActual ?? realtimeForecast ?? scheduled
    platform: str            # locationMetadata.platform: actual ?? forecast ?? planned
    cancelled: bool          # temporalData.departure.isCancelled
    lateness: int            # computed: expected - scheduled, in minutes
    destination: str
```

`realtimeEstimate` is deliberately unused: the spec states it is only populated
with the right token entitlement, so relying on it would work for some tokens
and silently not others. Confirmed against a real token — entitlements came
back empty.

**Two field mappings corrected against a live response** (23 services at Custom
House, 2026-07-27). The originals were read off the OpenAPI schema and were
wrong about what is actually populated:

- **`lateness` must be computed, not read.** `realtimeAdvertisedLateness` was
  absent from all 23 services, and the schema says it is null unless
  `realtimeActual` is set — which only happens once a train has *already run*.
  For upcoming departures, the only ones this screen cares about, it is always
  null. Reading it would leave `lateness` permanently 0, so no service would
  ever count as delayed and the timeline would never trigger. Compute
  `expected - scheduled` instead.
- **`platform.actual` was absent from all 23 services**; only `planned` and
  `forecast` appear. The fallback must be `actual ?? forecast ?? planned` or a
  forecast platform change is silently ignored.

Confirmed at the same time: **platform B is westbound** (Reading, Heathrow T4,
Maidenhead, Paddington); platform A is Abbey Wood.

`RttClient.departures(station, platform)` issues
`GET /rtt/location?code=gb-nr:{station}`, parses `services[]`, filters by
platform, and returns them sorted by `expected`. It mirrors `SpotifyClient`:
non-blocking backoff on 429 via a `rate_limited_until` deadline, and no
unbounded retry.

### Poll thread

A second daemon thread mirroring `poll_spotify`, dormant outside the commute
window. It reuses **`RequestBudget`** (added 2026-07-27 for Spotify) configured
to RTT's 30/min ceiling.

Cadence: 60s normally, tightening to 20s when the next departure is under five
minutes away — the same hot-window idea as the Spotify poller, applied where
accuracy matters most. That is roughly 250 requests per weekday against a 9000
daily allowance.

Network failures reuse the existing `STATUS_OFFLINE` indicator; no new fault
signalling is needed.

### `CommuteSchedule`

Answers "is the commute screen active now" from `--commute-days`,
`--commute-start`, `--commute-end`. Uses the Pi's local timezone, consistent
with `render_clock`.

## Screens

`render_commute()` lives in `spotify_matrix.py` alongside `render_idle`,
`render_clock` and `render_record`, matching the existing layout. Only the
reusable primitives move to `tinyfont.py`.

It selects among four states:

| State | Trigger | Shows |
|---|---|---|
| Hero | Next usable train is on time | `LEAVE IN n`, seven-segment, plus departure time and platform |
| Timeline | Next departure delayed ≥2 min or cancelled | Three services with times, countdowns and `DLY` / `CAN` |
| Empty | No platform B services in the window | `NO PLAT B TRAINS` |
| Stale | Last successful fetch > 3 minutes old | Dimmed digits, countdown frozen |

Hero colour thresholds, where *n* is minutes until you must leave:

| *n* | Colour | Reading |
|---|---|---|
| ≥ 5 | green | comfortable |
| 1–4 | amber | move now |
| ≤ 0 | amber, `LEAVE NOW` | the number is replaced by the words |

Red is reserved for cancellations on the timeline, so it always means "this
train is not happening" rather than "hurry".

Three behaviours that matter more than they look:

- The countdown uses `expected`, never `scheduled`, so a delay pushes the
  number **up** rather than lying.
- A cancelled next train is skipped entirely — the hero counts down to the
  first train that can actually be caught, not the first one listed.
- **A train leaving sooner than `walk_minutes` away is not an option at all**
  (`trains.in_reach`). This was missed in the original design and found only
  against live data: without it the hero targets a train it cannot reach,
  `minutes_to_leave` clamps to zero, and the screen shows `LEAVE NOW`. Because
  the Elizabeth line runs every ~5 minutes there is *always* such a train, so
  at a 15-minute walk the screen would have shown `LEAVE NOW` permanently and
  never once produced a useful countdown. Cancellations survive this filter —
  a cancelled train you could have reached still needs reporting.

**The stale state is the important failure mode.** A countdown computed from
stale data keeps ticking and looks authoritative. Freezing and dimming it makes
the fault visible; a confidently wrong "LEAVE IN 4" is worse than an obviously
broken screen.

## Loop integration

| Condition | Behaviour |
|---|---|
| Idle, in window | Commute screen instead of the clock |
| Playing, in window | Alternate: record 20s, commute 8s, via the existing transition system |
| Outside window | Unchanged |

Alternation reuses `pick_transition` so swaps read as deliberate rather than
glitchy.

## Configuration

| Setting | Default |
|---|---|
| `RTT_TOKEN` (`.env`) | none — required |
| `--station` | `CUS` |
| `--platform` | `B` |
| `--walk-minutes` | `15` |
| `--commute-days` | `mon,tue,wed,thu,fri` (comma-separated, case-insensitive) |
| `--commute-start` / `--commute-end` | `06:30` / `09:30` |
| `--no-commute` | off |

`--walk-minutes` must be set correctly by the user; the hero screen is wrong by
exactly its error, and it also decides which trains are reachable at all.
Confirmed with the user: 15 minutes to Custom House.

## Verification

Consistent with the existing no-test-framework, CLI-mode approach.

1. **`--self-test` gains:** every glyph ≤5px wide and 5 rows tall; no label used
   by any screen exceeds 32px when rendered; seven-segment `11` produces two
   distinct stems; `Departure` parsing against a **recorded RTT response
   fixture** committed to the repo, so parsing is testable with no token and no
   network.
2. **`--preview-commute DIR`** renders every state — comfortable, hurry, now,
   delayed, cancelled, empty, stale — credential-free, mirroring
   `--preview-frames`.
3. **Schedule boundaries** asserted directly: inside/outside window, weekend,
   and both edges.
4. **On hardware:** confirm legibility at the wall-mounted distance and that
   alternation reads well against real album art.

## Staging

Each stage is independently verifiable and independently deployable.

### Stage 1 — rendering, no network

`tinyfont.py`, `render_commute()`, all four screen states, `--preview-commute`,
and the font/label self-tests. Driven by hand-built `Departure` objects.

*Done when:* every state can be previewed and shown on the panel with fake
data. No RTT token needed, so this is unblocked by API registration.

### Stage 2 — data, no integration

`trains.py`, `RttClient`, parsing against the committed fixture, and a
`--commute-once` mode that fetches live and prints/renders a single frame.

*Done when:* real Custom House platform B departures render correctly on
demand. Requires the RTT token.

### Stage 3 — live integration

Poll thread, `RequestBudget` wiring, `CommuteSchedule`, render-loop branch and
alternation.

**Seam inherited from Stage 1 — do not miss it.** `trains.on_platform()` is
implemented and tested, but *nothing in the render path calls it*.
`render_commute()` takes a `platform` argument used only for display text, not
for filtering. So the integration code is solely responsible for calling
`on_platform()` before handing departures to `render_commute()`. Forget it and
services from both platforms compete in the hero and timeline logic, with
nothing to signal that filtering was skipped — the screen would confidently
count down to a train leaving from the other platform. Wire it explicitly and
add a check that proves a platform A service never reaches the screen.

Two smaller Stage 1 assumptions that live data will test:

- `minutes_to_leave` assumes `now` and `departure.expected` share a clock.
  Stage 1 fixtures are naive datetimes from one base, so this is untested
  against the Pi's system timezone versus RTT's returned times. The idle clock
  already has a timezone gotcha documented in `CLAUDE.md`; this is the same
  class of bug.
- `Departure.destination` is populated but never read, so nothing pins its
  expected shape. Confirm RTT's payload maps onto it cleanly before relying
  on it.

*Done when:* the panel switches to the commute screen by itself on a weekday
morning and alternates correctly while music plays.

## Risks

- **API deadline.** Targeting `data.rtt.io` from the start avoids a forced
  rewrite before 30 September 2026, at the cost of no usable third-party
  wrappers and thinner community documentation.
- **Walk time is a guess until configured.** Mitigated by making it a flag and
  documenting it, not by trying to infer it.
- **Alternation could annoy.** 20s/8s is a starting point; both become
  constants that are trivial to retune after living with it.
- **`platform` may be absent** on some services. Those are excluded rather than
  assumed to be platform B, so the screen under-reports rather than misleads.
