# Morning Commute Screen — Stage 1 (Rendering) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render all four commute screen states on the 32×32 LED matrix from hand-built data, with no network access and no RTT token.

**Architecture:** A new `tinyfont.py` holds reusable pixel-drawing primitives (a variable-width 5px bitmap font and parametric seven-segment digits). A new `trains.py` holds the `Departure` dataclass and pure selection helpers. `render_commute()` joins them in `spotify_matrix.py` alongside the existing `render_*` functions. Verification runs through the existing `--self-test` CLI mode, restructured into a filterable registry of named checks so red-green TDD works.

**Tech Stack:** Python 3.11+, Pillow. No new dependencies. No test framework — checks run via `python spotify_matrix.py --self-test [PATTERN]`.

**Spec:** `docs/superpowers/specs/2026-07-27-morning-commute-screen-design.md`

## Global Constraints

- **No new runtime dependencies.** `requirements.txt` must not change. Pillow and the stdlib only.
- **Panel size is `min(rows, cols)`**, defaulting to 32. Horizontal placement (centring, right-alignment) must derive from the passed `size`/`width`, never a literal 32. Vertical layout constants are tuned for a 32px panel and may be literals — the commute screens are designed for this panel, and making them fully responsive is YAGNI. `--preview-commute` honours `--rows`/`--cols` like `--preview-frames` does.
- **The Mac has no system Pillow and no `.venv`.** Create a scratch venv once to run checks locally: `python3 -m venv /tmp/mvenv && /tmp/mvenv/bin/pip install pillow`. On the Pi use `.venv/bin/python`.
- **Every glyph is exactly 5 rows tall.** Width varies per glyph and is derived from the glyph data, never assumed.
- **Colour meanings are fixed:** red means "this train is not happening" (cancellation) and nothing else. Urgency tops out at amber.
- `DELAY_THRESHOLD_MINUTES = 2` — at or above this a service counts as delayed.
- **Stage 1 adds no network code.** `trains.py` gets the dataclass and pure helpers only; `RttClient` is Stage 2.
- Commit after every task. Follow the existing comment style: explain *why*, not *what*.

---

### Task 1: Restructure `--self-test` into a filterable check registry

The current `run_self_test()` is one function of sequential asserts. TDD needs to run a single named check and watch it fail, so this converts it into a registry. No behaviour is lost — every existing assertion keeps running.

**Files:**
- Modify: `spotify_matrix.py` (`run_self_test`, the `--self-test` argparse entry, and the `run()` dispatch branch)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `SELF_TESTS: list[tuple[str, Callable[[], None]]]` — ordered registry
  - `self_test(name: str)` — decorator registering a zero-arg check function
  - `run_self_test(pattern: str | None = None) -> None` — runs checks whose name contains `pattern` (all if `None`), prints one line each, raises `SystemExit(1)` if any fail or if `pattern` matches nothing

- [ ] **Step 1: Write the failing check**

Add near the existing `run_self_test`, replacing nothing yet:

```python
SELF_TESTS: list[tuple[str, Any]] = []


def self_test(name: str):
    """Register a named check runnable via --self-test [PATTERN]."""

    def register(func):
        SELF_TESTS.append((name, func))
        return func

    return register


@self_test("registry-smoke")
def _check_registry_smoke() -> None:
    assert len(SELF_TESTS) >= 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test registry-smoke`

Expected: FAIL — `--self-test` is currently `store_true`, so passing a value errors with `unrecognized arguments: registry-smoke`.

- [ ] **Step 3: Implement the harness**

Replace the whole body of `run_self_test` with the registry runner, and move each numbered block of the existing function into its own registered check. The existing assertions are copied verbatim — only their packaging changes.

```python
def run_self_test(pattern: str | None = None) -> None:
    """Run the registered checks, optionally filtered by name substring.

    No hardware, no credentials, no test framework - this project ships as a
    couple of files, so the checks live next to the code they guard. The
    registry (rather than one long function) exists so a single check can be
    run and watched to fail while writing it.
    """
    selected = [(name, func) for name, func in SELF_TESTS if not pattern or pattern in name]
    if not selected:
        print(f"self-test: no check matches {pattern!r}", flush=True)
        raise SystemExit(1)

    failures = 0
    for name, func in selected:
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}", flush=True)
        else:
            print(f"ok   {name}", flush=True)

    print(f"self-test: {len(selected) - failures}/{len(selected)} passed", flush=True)
    if failures:
        raise SystemExit(1)
```

Now split the old body into checks. Each keeps its original comment:

```python
@self_test("status-dot-rotation")
def _check_status_dot_rotation() -> None:
    # The dot must reach the panel's top-right for every rotation, and it must
    # be exactly one pixel. Getting this backwards is invisible until the panel
    # is on the wall.
    size = 32
    for rotate in (0, 90, 180, 270):
        frame = draw_status_dot(Image.new("RGB", (size, size)), STATUS_OFFLINE, 0.25, rotate)
        shown = rotate_frame(frame, rotate)
        lit = [
            xy
            for xy in ((x, y) for y in range(size) for x in range(size))
            if shown.getpixel(xy) != (0, 0, 0)
        ]
        assert lit == [(size - 1, 0)], f"rotate={rotate} lit {lit}, expected top-right"


@self_test("poll-pacing")
def _check_poll_pacing() -> None:
    # Including the case that used to hurt: a skip early in a long track must
    # not wait for the track boundary.
    assert compute_poll_delay(True, 0.0, 2.0, 5.0) == HOT_POLL_SECONDS
    assert compute_poll_delay(False, 0.0, 2.0, 5.0) == HOT_POLL_SECONDS
    assert compute_poll_delay(True, 60.0, 2.0, 5.0) == 2.0
    assert compute_poll_delay(False, 60.0, 2.0, 5.0) == 5.0


@self_test("request-budget")
def _check_request_budget() -> None:
    # The budget is a real ceiling: a full bucket hands out exactly its
    # capacity, then makes the caller wait.
    budget = RequestBudget(45.0)
    for _ in range(45):
        assert budget.wait_seconds() == 0.0
        budget.consume()
    assert budget.wait_seconds() > 0.0


@self_test("status-dot-rhythm")
def _check_status_dot_rhythm() -> None:
    # Rhythms stay in range and actually vary; the breathe never goes dark.
    for status in (STATUS_OFFLINE, STATUS_RATE_LIMITED):
        levels = [status_dot_level(status, tick / 20.0) for tick in range(100)]
        assert all(0.0 <= level <= 1.0 for level in levels), status
        assert max(levels) > 0.9 and len(set(levels)) > 10, status
    assert min(status_dot_level(STATUS_RATE_LIMITED, t / 20.0) for t in range(100)) > 0.2
    assert min(status_dot_level(STATUS_OFFLINE, t / 20.0) for t in range(100)) == 0.0
    assert status_dot_level(STATUS_OK, 0.0) == 0.0
```

Delete the `_check_registry_smoke` scaffold from Step 1 — the real checks now prove the registry works.

Change the argparse entry so it takes an optional pattern:

```python
    parser.add_argument(
        "--self-test",
        nargs="?",
        const="",
        default=None,
        metavar="PATTERN",
        help="Run the built-in checks (optionally only those whose name contains PATTERN) and exit.",
    )
```

And the dispatch branch at the top of `run()`:

```python
    if args.self_test is not None:
        run_self_test(args.self_test or None)
        return
```

- [ ] **Step 4: Run to verify all checks pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test`

Expected: four `ok` lines and `self-test: 4/4 passed`.

Then confirm filtering and the no-match guard:

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test poll`
Expected: `ok   poll-pacing` and `self-test: 1/1 passed`.

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test nonesuch; echo "exit=$?"`
Expected: `self-test: no check matches 'nonesuch'` and `exit=1`.

- [ ] **Step 5: Commit**

```bash
git add spotify_matrix.py
git commit -m "Turn --self-test into a filterable registry of named checks"
```

---

### Task 2: `tinyfont.py` — variable-width bitmap font

**Files:**
- Create: `tinyfont.py`
- Modify: `spotify_matrix.py` (add checks)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `GLYPHS: dict[str, list[str]]` — each value is 5 strings of `"0"`/`"1"`, all the same length within a glyph
  - `glyph_width(char: str) -> int`
  - `text_width(text: str) -> int` — includes 1px inter-character spacing, excludes trailing space
  - `draw_text(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color: tuple[int, int, int]) -> int` — returns the x just past the last glyph
  - `draw_text_centred(draw, y: int, text: str, color, width: int) -> None`

- [ ] **Step 1: Write the failing checks**

In `spotify_matrix.py`, add `import tinyfont` at the top with the other imports, and append:

```python
# Every label any commute screen draws. Checked as a group so a new label
# cannot silently overflow the panel.
COMMUTE_LABELS = ("LEAVE IN", "LEAVE", "NOW", "PLAT B", "THEN", "NO", "TRAINS", "DLY", "CAN")


@self_test("font-glyph-shape")
def _check_font_glyph_shape() -> None:
    for char, glyph in tinyfont.GLYPHS.items():
        assert len(glyph) == 5, f"{char!r} has {len(glyph)} rows, expected 5"
        widths = {len(row) for row in glyph}
        assert len(widths) == 1, f"{char!r} has ragged rows: {widths}"
        assert set("".join(glyph)) <= {"0", "1"}, f"{char!r} has non-binary pixels"
        assert 1 <= len(glyph[0]) <= 5, f"{char!r} is {len(glyph[0])}px wide"


@self_test("font-metrics")
def _check_font_metrics() -> None:
    # Width is glyph data plus one pixel between characters, with no trailing gap.
    assert tinyfont.text_width("B") == tinyfont.glyph_width("B")
    assert tinyfont.text_width("BB") == tinyfont.glyph_width("B") * 2 + 1
    # Every label must fit the narrowest panel we support.
    for label in COMMUTE_LABELS:
        assert tinyfont.text_width(label) <= 32, f"{label!r} is {tinyfont.text_width(label)}px"


@self_test("font-legibility")
def _check_font_legibility() -> None:
    # These pairs were confirmed to collide at 3px while building the mockups:
    # N read as M, V read as U, W read as U. They must differ as bitmaps.
    #
    # O and 0 are deliberately identical. At 3x5 the only way to distinguish
    # them is an interior pixel, which turns the zero into an 8 - and every
    # time on the commute screen starts with a zero. They never appear in an
    # ambiguous position anyway: O only in "NO"/"NOW", 0 only in times.
    for a, b in (("N", "H"), ("V", "W"), ("S", "5")):
        assert tinyfont.GLYPHS[a] != tinyfont.GLYPHS[b], f"{a} and {b} render identically"


@self_test("font-draw")
def _check_font_draw() -> None:
    frame = Image.new("RGB", (32, 32), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    end = tinyfont.draw_text(draw, 0, 0, "1", (255, 255, 255))
    assert end == tinyfont.glyph_width("1") + 1, end
    # '1' is a single 1px column, 5 tall.
    lit = [(x, y) for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)]
    assert lit == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)], lit


@self_test("font-centred")
def _check_font_centred() -> None:
    frame = Image.new("RGB", (32, 32), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    tinyfont.draw_text_centred(draw, 0, "1", (255, 255, 255), 32)
    xs = {x for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)}
    # A 1px glyph in a 32px frame starts at (32 - 1) // 2 = 15.
    assert xs == {15}, xs
```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test font`

Expected: FAIL — `ModuleNotFoundError: No module named 'tinyfont'`.

- [ ] **Step 3: Implement `tinyfont.py`**

```python
#!/usr/bin/env python3
"""Pixel-drawing primitives for the LED matrix: a tiny font and LED digits.

Deliberately domain-free - it just puts pixels on a PIL draw surface, so any
screen in this project can share it without inheriting another screen's
concerns.
"""
from __future__ import annotations

from PIL import ImageDraw

# 5-row bitmap font. Width varies per glyph, which is not a nicety: at a fixed
# 3px, N reads as M, and M and W are indistinguishable from N and U. Confirmed
# by rendering them, not by assumption. Only the characters the screens
# actually use are defined; adding a glyph is a one-line change.
GLYPHS: dict[str, list[str]] = {
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "C": ["011", "100", "100", "100", "011"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "H": ["101", "101", "111", "101", "101"],
    "I": ["1", "1", "1", "1", "1"],
    "L": ["100", "100", "100", "100", "111"],
    # N needs a 4th column for its diagonal, or it reads as M or H.
    "N": ["1001", "1101", "1011", "1001", "1001"],
    "O": ["111", "101", "101", "101", "111"],
    "P": ["110", "101", "110", "100", "100"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["011", "100", "010", "001", "110"],
    "T": ["111", "010", "010", "010", "010"],
    # V drops its stems a row earlier than W so the two don't collide.
    "V": ["101", "101", "101", "010", "010"],
    "W": ["10001", "10001", "10101", "11011", "10001"],
    "Y": ["101", "101", "010", "010", "010"],
    "0": ["111", "101", "101", "101", "111"],
    "1": ["1", "1", "1", "1", "1"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    " ": ["0", "0", "0", "0", "0"],
}

GLYPH_HEIGHT = 5
CHAR_SPACING = 1


def glyph_width(char: str) -> int:
    return len(GLYPHS[char][0])


def text_width(text: str) -> int:
    if not text:
        return 0
    return sum(glyph_width(char) + CHAR_SPACING for char in text) - CHAR_SPACING


def draw_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
) -> int:
    """Draw `text` with its top-left at (x, y); return the x just past it."""
    for char in text:
        glyph = GLYPHS[char]
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    draw.point((x + col, y + row), fill=color)
        x += len(glyph[0]) + CHAR_SPACING
    return x


def draw_text_centred(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    color: tuple[int, int, int],
    width: int,
) -> None:
    draw_text(draw, (width - text_width(text)) // 2, y, text, color)
```

- [ ] **Step 4: Run to verify they pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test font`

Expected: five `ok` lines, `self-test: 5/5 passed`.

Then confirm nothing regressed: `/tmp/mvenv/bin/python spotify_matrix.py --self-test`
Expected: `self-test: 9/9 passed`.

- [ ] **Step 5: Commit**

```bash
git add tinyfont.py spotify_matrix.py
git commit -m "Add tinyfont: a variable-width 5px bitmap font for the matrix"
```

---

### Task 3: `tinyfont.py` — seven-segment digits

**Files:**
- Modify: `tinyfont.py`, `spotify_matrix.py` (add checks)

**Interfaces:**
- Consumes: `tinyfont` from Task 2
- Produces:
  - `SEGMENTS: dict[str, str]` — digit to segment letters (`abcdefg`)
  - `seven_seg_width(digit: str, w: int, t: int) -> int`
  - `draw_seven_seg(draw, x: int, y: int, w: int, h: int, digit: str, color, t: int = 2) -> None`
  - `big_number_width(value: int, w: int, t: int, gap: int) -> int`
  - `draw_big_number(draw, value: int, y: int, color, width: int, w: int = 11, h: int = 17, t: int = 2, gap: int = 3) -> None` — horizontally centred in `width`

- [ ] **Step 1: Write the failing checks**

Append to `spotify_matrix.py`:

```python
@self_test("seg-one-is-a-stem")
def _check_seg_one_is_a_stem() -> None:
    # A literal seven-segment '1' lights the two right-hand bars, so at w=11
    # "11" reads as four evenly spaced posts. '1' is special-cased to one stem
    # with a narrow advance; this check is why.
    assert tinyfont.seven_seg_width("1", 11, 2) == 2
    assert tinyfont.seven_seg_width("8", 11, 2) == 11

    frame = Image.new("RGB", (32, 32), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    tinyfont.draw_big_number(draw, 11, 0, (255, 255, 255), 32)
    columns = sorted({x for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)})
    # Two stems of thickness 2, separated by the 3px gap.
    assert len(columns) == 4, columns
    assert columns[1] - columns[0] == 1 and columns[3] - columns[2] == 1, columns
    assert columns[2] - columns[1] == 4, columns


@self_test("seg-digits-differ")
def _check_seg_digits_differ() -> None:
    rendered = {}
    for digit in "0123456789":
        frame = Image.new("RGB", (16, 20), (0, 0, 0))
        tinyfont.draw_seven_seg(ImageDraw.Draw(frame), 0, 0, 11, 17, digit, (255, 255, 255))
        rendered[digit] = frame.tobytes()
    assert len(set(rendered.values())) == 10, "two digits render identically"


@self_test("seg-bounds")
def _check_seg_bounds() -> None:
    # Nothing may spill outside the requested box, or it will clip on-panel.
    frame = Image.new("RGB", (32, 32), (0, 0, 0))
    tinyfont.draw_seven_seg(ImageDraw.Draw(frame), 4, 6, 11, 17, "8", (255, 255, 255))
    lit = [(x, y) for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)]
    assert lit, "nothing drawn"
    assert min(x for x, _ in lit) >= 4 and max(x for x, _ in lit) <= 4 + 11 - 1
    assert min(y for _, y in lit) >= 6 and max(y for _, y in lit) <= 6 + 17 - 1


@self_test("seg-centred")
def _check_seg_centred() -> None:
    # Centring must account for the narrow '1', or numbers containing 1 sit off-centre.
    assert tinyfont.big_number_width(8, 11, 2, 3) == 11
    assert tinyfont.big_number_width(11, 11, 2, 3) == 2 + 3 + 2
    assert tinyfont.big_number_width(18, 11, 2, 3) == 2 + 3 + 11
```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test seg`

Expected: FAIL — `AttributeError: module 'tinyfont' has no attribute 'seven_seg_width'`.

- [ ] **Step 3: Implement**

Append to `tinyfont.py`:

```python
# Seven-segment digits, drawn as rectangles rather than a second bitmap font.
# Parametric in size, so one implementation covers any large-digit use without
# a second font, and the segmented shape stays legible at low pixel counts.
#
#   aaa
#  f   b
#   ggg
#  e   c
#   ddd
SEGMENTS: dict[str, str] = {
    "0": "abcdef",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgedc",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcfgd",
}


def seven_seg_width(digit: str, w: int, t: int) -> int:
    """Advance width. '1' is a bare stem, so it gets a narrow cell."""
    return t if digit == "1" else w


def draw_seven_seg(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    digit: str,
    color: tuple[int, int, int],
    t: int = 2,
) -> None:
    if digit == "1":
        # A true seven-segment '1' lights segments b and c, which sit at
        # opposite edges of the cell - so "11" reads as four separate posts.
        # One stem is what people actually recognise as a 1.
        draw.rectangle((x, y, x + t - 1, y + h - 1), fill=color)
        return

    mid = y + (h - t) // 2
    bars = {
        "a": (x + t, y, x + w - 1 - t, y + t - 1),
        "b": (x + w - t, y + t, x + w - 1, mid - 1),
        "c": (x + w - t, mid + t, x + w - 1, y + h - 1 - t),
        "d": (x + t, y + h - t, x + w - 1 - t, y + h - 1),
        "e": (x, mid + t, x + t - 1, y + h - 1 - t),
        "f": (x, y + t, x + t - 1, mid - 1),
        "g": (x + t, mid, x + w - 1 - t, mid + t - 1),
    }
    for segment in SEGMENTS[digit]:
        draw.rectangle(bars[segment], fill=color)


def big_number_width(value: int, w: int, t: int, gap: int) -> int:
    digits = str(value)
    return sum(seven_seg_width(d, w, t) for d in digits) + gap * (len(digits) - 1)


def draw_big_number(
    draw: ImageDraw.ImageDraw,
    value: int,
    y: int,
    color: tuple[int, int, int],
    width: int,
    w: int = 11,
    h: int = 17,
    t: int = 2,
    gap: int = 3,
) -> None:
    digits = str(value)
    x = (width - big_number_width(value, w, t, gap)) // 2
    for digit in digits:
        draw_seven_seg(draw, x, y, w, h, digit, color, t)
        x += seven_seg_width(digit, w, t) + gap
```

- [ ] **Step 4: Run to verify they pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test seg`
Expected: `self-test: 4/4 passed`.

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test`
Expected: `self-test: 13/13 passed`.

- [ ] **Step 5: Commit**

```bash
git add tinyfont.py spotify_matrix.py
git commit -m "Add parametric seven-segment digits to tinyfont"
```

---

### Task 4: `trains.py` — `Departure` model and selection helpers

Pure data and pure functions. No network — `RttClient` arrives in Stage 2.

**Files:**
- Create: `trains.py`
- Modify: `spotify_matrix.py` (add checks)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DELAY_THRESHOLD_MINUTES = 2`
  - `Departure` dataclass: `scheduled: datetime`, `expected: datetime`, `platform: str`, `cancelled: bool`, `lateness: int`, `destination: str`; property `delayed -> bool`
  - `on_platform(departures: list[Departure], platform: str) -> list[Departure]`
  - `catchable(departures: list[Departure]) -> list[Departure]` — drops cancelled, sorts by `expected`
  - `minutes_to_leave(departure: Departure, now: datetime, walk_minutes: int) -> int`
  - `is_disrupted(departures: list[Departure]) -> bool`
  - In `spotify_matrix.py`: `SELF_TEST_NOW` and
    `sample_departure(base, minutes_out, *, cancelled=False, late=0) -> trains.Departure`,
    the single departure builder shared by the checks and by `--preview-commute` in Task 7

- [ ] **Step 1: Write the failing checks**

Add `import trains` to `spotify_matrix.py` and append:

```python
SELF_TEST_NOW = datetime.datetime(2026, 7, 27, 7, 30)


def sample_departure(
    base: datetime.datetime,
    minutes_out: int,
    *,
    cancelled: bool = False,
    late: int = 0,
) -> trains.Departure:
    """Build a Departure `minutes_out` after `base`.

    Shared by the checks and by --preview-commute so there is exactly one
    place that knows how to fabricate a departure.
    """
    scheduled = base + datetime.timedelta(minutes=minutes_out)
    return trains.Departure(
        scheduled=scheduled,
        expected=scheduled + datetime.timedelta(minutes=late),
        platform="B",
        cancelled=cancelled,
        lateness=late,
        destination="Paddington",
    )


def _departure(minutes_out: int, *, cancelled: bool = False, late: int = 0) -> trains.Departure:
    return sample_departure(SELF_TEST_NOW, minutes_out, cancelled=cancelled, late=late)


@self_test("trains-delayed")
def _check_trains_delayed() -> None:
    assert not _departure(10).delayed
    assert not _departure(10, late=1).delayed        # under the threshold
    assert _departure(10, late=2).delayed            # at the threshold
    assert _departure(10, late=9).delayed


@self_test("trains-platform-filter")
def _check_trains_platform_filter() -> None:
    b_train = _departure(5)
    a_train = _departure(6)
    a_train.platform = "A"
    unknown = _departure(7)
    unknown.platform = ""
    picked = trains.on_platform([b_train, a_train, unknown], "B")
    # A service with no platform is excluded, not assumed to be ours: the
    # screen under-reporting is far better than it misleading.
    assert picked == [b_train], picked


@self_test("trains-catchable")
def _check_trains_catchable() -> None:
    late_one = _departure(12)
    cancelled = _departure(4, cancelled=True)
    soon = _departure(8)
    got = trains.catchable([late_one, cancelled, soon])
    # Cancelled dropped, remainder sorted by when they actually leave.
    assert got == [soon, late_one], got


@self_test("trains-leave-minutes")
def _check_trains_leave_minutes() -> None:
    # 12 minutes away, 8 minute walk -> leave in 4.
    assert trains.minutes_to_leave(_departure(12), SELF_TEST_NOW, 8) == 4
    # A delay pushes the number UP; it must never report the scheduled time.
    assert trains.minutes_to_leave(_departure(12, late=5), SELF_TEST_NOW, 8) == 9
    # Already too late clamps to zero rather than going negative.
    assert trains.minutes_to_leave(_departure(3), SELF_TEST_NOW, 8) == 0


@self_test("trains-disrupted")
def _check_trains_disrupted() -> None:
    assert not trains.is_disrupted([_departure(5), _departure(12)])
    assert trains.is_disrupted([_departure(5, late=4), _departure(12)])
    assert trains.is_disrupted([_departure(5, cancelled=True), _departure(12)])
    # Trouble further down the list is not a reason to abandon the hero screen.
    assert not trains.is_disrupted([_departure(5), _departure(12, cancelled=True)])
    assert not trains.is_disrupted([])
```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test trains`

Expected: FAIL — `ModuleNotFoundError: No module named 'trains'`.

- [ ] **Step 3: Implement `trains.py`**

```python
#!/usr/bin/env python3
"""Train departure data for the morning commute screen.

Stage 1 is the model and the pure selection rules only; the Realtime Trains
client lands in Stage 2. Keeping the rules pure means they are checkable with
no token and no network.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

# At or above this many minutes late, a service counts as delayed and the
# screen switches from the hero countdown to the timeline.
DELAY_THRESHOLD_MINUTES = 2


@dataclass
class Departure:
    scheduled: datetime.datetime
    expected: datetime.datetime
    platform: str
    cancelled: bool
    lateness: int
    destination: str

    @property
    def delayed(self) -> bool:
        return self.lateness >= DELAY_THRESHOLD_MINUTES


def on_platform(departures: list[Departure], platform: str) -> list[Departure]:
    """Keep only services on `platform`.

    A service with no platform is dropped rather than assumed to be ours. RTT
    leaves the field empty when the platform isn't yet known, and a missed
    train is a much cheaper mistake than a confidently wrong one.
    """
    return [d for d in departures if d.platform == platform]


def catchable(departures: list[Departure]) -> list[Departure]:
    """Services that can actually be caught, soonest first."""
    return sorted((d for d in departures if not d.cancelled), key=lambda d: d.expected)


def minutes_to_leave(departure: Departure, now: datetime.datetime, walk_minutes: int) -> int:
    """Whole minutes until you must set off, floored at zero.

    Uses `expected`, never `scheduled`: a delay must push this number up rather
    than quietly send you to the platform early.
    """
    seconds = (departure.expected - now).total_seconds() - walk_minutes * 60
    return max(0, int(seconds // 60))


def is_disrupted(departures: list[Departure]) -> bool:
    """Whether the *next* service is delayed or cancelled.

    Only the next one matters: that is what decides whether the hero countdown
    still tells the truth, or whether the rider needs to see their options.
    """
    ordered = sorted(departures, key=lambda d: d.expected)
    if not ordered:
        return False
    return ordered[0].cancelled or ordered[0].delayed
```

- [ ] **Step 4: Run to verify they pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test trains`
Expected: `self-test: 5/5 passed`.

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test`
Expected: `self-test: 18/18 passed`.

- [ ] **Step 5: Commit**

```bash
git add trains.py spotify_matrix.py
git commit -m "Add Departure model and pure commute selection rules"
```

---

### Task 5: The hero screen

**Files:**
- Modify: `spotify_matrix.py` (add `render_commute_hero` next to `render_clock`, plus checks)

**Interfaces:**
- Consumes: `tinyfont`, `trains` from Tasks 2–4
- Produces:
  - `COMMUTE_GREEN`, `COMMUTE_AMBER`, `COMMUTE_RED`, `COMMUTE_LABEL`, `COMMUTE_DIM`, `COMMUTE_WHITE` colour constants
  - `hero_colour(minutes: int) -> tuple[int, int, int]`
  - `render_commute_hero(size: int, departure: trains.Departure, minutes: int) -> Image.Image`

- [ ] **Step 1: Write the failing checks**

```python
@self_test("hero-colour")
def _check_hero_colour() -> None:
    # Red is reserved for cancellations, so urgency tops out at amber.
    assert hero_colour(9) == COMMUTE_GREEN
    assert hero_colour(5) == COMMUTE_GREEN
    assert hero_colour(4) == COMMUTE_AMBER
    assert hero_colour(1) == COMMUTE_AMBER
    assert hero_colour(0) == COMMUTE_AMBER
    assert COMMUTE_RED not in (hero_colour(m) for m in range(0, 30))


@self_test("hero-render")
def _check_hero_render() -> None:
    frame = render_commute_hero(32, _departure(12), 4)
    assert frame.size == (32, 32)
    lit = [(x, y) for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)]
    assert lit, "hero screen rendered blank"
    assert max(y for _, y in lit) <= 31 and max(x for x, _ in lit) <= 31
    # The countdown digit must dominate: most lit pixels sit in the middle band.
    middle = [xy for xy in lit if 8 <= xy[1] <= 24]
    assert len(middle) > len(lit) // 2, "hero number is not the dominant element"


@self_test("hero-now")
def _check_hero_now() -> None:
    # At zero the digits are replaced by words - "LEAVE IN 0" is a worse
    # instruction than "LEAVE NOW".
    frame = render_commute_hero(32, _departure(8), 0)
    other = render_commute_hero(32, _departure(8), 6)
    assert frame.tobytes() != other.tobytes()
```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test hero`
Expected: FAIL — `NameError: name 'hero_colour' is not defined`.

- [ ] **Step 3: Implement**

Add after `render_clock` in `spotify_matrix.py`:

```python
# --- Commute screen ----------------------------------------------------------
# A 32x32 panel cannot show a departure board, so it answers a different
# question: not "when is the train" but "when do I leave", which is one number.
COMMUTE_GREEN = (0, 215, 95)
COMMUTE_AMBER = (255, 150, 0)
COMMUTE_RED = (255, 45, 45)
COMMUTE_LABEL = (140, 152, 195)
COMMUTE_DIM = (72, 78, 98)
COMMUTE_WHITE = (215, 220, 240)

COMMUTE_COMFORTABLE_MINUTES = 5


def hero_colour(minutes: int) -> tuple[int, int, int]:
    """Green with time in hand, amber once it's time to move.

    Never red: red is reserved for cancellations, so that it always means "this
    train is not happening" rather than "hurry up".
    """
    return COMMUTE_GREEN if minutes >= COMMUTE_COMFORTABLE_MINUTES else COMMUTE_AMBER


def _clock_label(when: datetime.datetime) -> str:
    # No colon: dropping it is what makes a time fit in four characters, which
    # is what makes three of them fit on the timeline.
    return when.strftime("%H%M")


def render_commute_hero(size: int, departure: trains.Departure, minutes: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    colour = hero_colour(minutes)
    footer = f"{_clock_label(departure.expected)} {departure.platform}"

    if minutes <= 0:
        tinyfont.draw_text_centred(draw, 3, "LEAVE", COMMUTE_LABEL, size)
        # Drawn twice a pixel apart: at 5px tall this is the only way to give
        # the words the weight the digits would have had.
        tinyfont.draw_text_centred(draw, 12, "NOW", colour, size)
        tinyfont.draw_text_centred(draw, 13, "NOW", colour, size)
        tinyfont.draw_text_centred(draw, 24, footer, COMMUTE_WHITE, size)
        return frame

    tinyfont.draw_text_centred(draw, 1, "LEAVE IN", COMMUTE_LABEL, size)
    tinyfont.draw_big_number(draw, minutes, 8, colour, size)
    tinyfont.draw_text_centred(draw, 26, footer, COMMUTE_DIM, size)
    return frame
```

- [ ] **Step 4: Run to verify they pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test hero`
Expected: `self-test: 3/3 passed`.

- [ ] **Step 5: Commit**

```bash
git add spotify_matrix.py
git commit -m "Add the hero 'leave in' commute screen"
```

---

### Task 6: The timeline and empty screens

**Files:**
- Modify: `spotify_matrix.py` (add both renderers plus checks)

**Interfaces:**
- Consumes: Tasks 2–5
- Produces:
  - `render_commute_timeline(size: int, departures: list[trains.Departure], now: datetime.datetime) -> Image.Image`
  - `render_commute_empty(size: int, platform: str) -> Image.Image`

- [ ] **Step 1: Write the failing checks**

```python
@self_test("timeline-render")
def _check_timeline_render() -> None:
    rows = [_departure(4), _departure(9), _departure(16)]
    frame = render_commute_timeline(32, rows, SELF_TEST_NOW)
    assert frame.size == (32, 32)
    lit = [(x, y) for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)]
    assert lit and max(y for _, y in lit) <= 31 and max(x for x, _ in lit) <= 31


@self_test("timeline-states-differ")
def _check_timeline_states_differ() -> None:
    healthy = render_commute_timeline(32, [_departure(4), _departure(9)], SELF_TEST_NOW)
    delayed = render_commute_timeline(32, [_departure(4, late=6), _departure(9)], SELF_TEST_NOW)
    cancelled = render_commute_timeline(32, [_departure(4, cancelled=True), _departure(9)], SELF_TEST_NOW)
    frames = {healthy.tobytes(), delayed.tobytes(), cancelled.tobytes()}
    assert len(frames) == 3, "delay and cancellation must look different from each other"


@self_test("timeline-cancel-is-red")
def _check_timeline_cancel_is_red() -> None:
    frame = render_commute_timeline(32, [_departure(4, cancelled=True)], SELF_TEST_NOW)
    colours = {frame.getpixel((x, y)) for y in range(32) for x in range(32)}
    assert COMMUTE_RED in colours, "a cancellation must be red"
    healthy = render_commute_timeline(32, [_departure(4)], SELF_TEST_NOW)
    healthy_colours = {healthy.getpixel((x, y)) for y in range(32) for x in range(32)}
    assert COMMUTE_RED not in healthy_colours, "red leaked onto a healthy service"


@self_test("empty-render")
def _check_empty_render() -> None:
    frame = render_commute_empty(32, "B")
    lit = [(x, y) for y in range(32) for x in range(32) if frame.getpixel((x, y)) != (0, 0, 0)]
    assert lit, "empty screen rendered blank"
    assert max(x for x, _ in lit) <= 31
```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test timeline`
Expected: FAIL — `NameError: name 'render_commute_timeline' is not defined`.

- [ ] **Step 3: Implement**

Add after `render_commute_hero`:

```python
COMMUTE_TIMELINE_ROWS = 3


def render_commute_timeline(
    size: int, departures: list[trains.Departure], now: datetime.datetime
) -> Image.Image:
    """Three services with times and status.

    This fits only because the colon is dropped from the time and the
    destination is omitted - every platform B service goes the same way, so the
    destination is redundant and the pixels are not spare.
    """
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    platform = departures[0].platform if departures else ""
    tinyfont.draw_text_centred(draw, 0, f"PLAT {platform}".strip(), COMMUTE_LABEL, size)
    draw.line((2, 7, size - 3, 7), fill=(38, 40, 54))

    y = 10
    for index, departure in enumerate(sorted(departures, key=lambda d: d.expected)[:COMMUTE_TIMELINE_ROWS]):
        if departure.cancelled:
            colour, right = COMMUTE_RED, "CAN"
        elif departure.delayed:
            colour, right = COMMUTE_AMBER, "DLY"
        else:
            # The soonest service is highlighted; later ones recede.
            colour = (COMMUTE_GREEN, COMMUTE_WHITE, COMMUTE_DIM)[min(index, 2)]
            right = str(max(0, int((departure.expected - now).total_seconds() // 60)))
        tinyfont.draw_text(draw, 1, y, _clock_label(departure.scheduled), colour)
        tinyfont.draw_text(draw, size - 1 - tinyfont.text_width(right), y, right, colour)
        y += 7
    return frame


def render_commute_empty(size: int, platform: str) -> Image.Image:
    """Nothing running. Also what a line suspension looks like from here."""
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    tinyfont.draw_text_centred(draw, 6, "NO", COMMUTE_DIM, size)
    tinyfont.draw_text_centred(draw, 14, f"PLAT {platform}".strip(), COMMUTE_DIM, size)
    tinyfont.draw_text_centred(draw, 22, "TRAINS", COMMUTE_DIM, size)
    return frame
```

- [ ] **Step 4: Run to verify they pass**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test timeline`
Expected: `self-test: 3/3 passed`.

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test empty`
Expected: `self-test: 1/1 passed`.

- [ ] **Step 5: Commit**

```bash
git add spotify_matrix.py
git commit -m "Add the timeline and empty commute screens"
```

---

### Task 7: `render_commute()` dispatcher, stale dimming, and `--preview-commute`

**Files:**
- Modify: `spotify_matrix.py` (dispatcher, preview renderer, argparse entry, `run()` branch, checks)
- Modify: `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 2–6, including `sample_departure()` and `SELF_TEST_NOW` from Task 4 —
  `render_commute_previews` reuses that builder rather than defining its own
- Produces:
  - `render_commute(size, departures, now, walk_minutes, platform="B", stale=False) -> Image.Image`
  - `render_commute_previews(directory: Path, size: int) -> None`
  - `--preview-commute DIR` CLI flag

- [ ] **Step 1: Write the failing checks**

```python
@self_test("commute-dispatch")
def _check_commute_dispatch() -> None:
    healthy = [_departure(12), _departure(19)]
    disrupted = [_departure(12, cancelled=True), _departure(19)]
    hero = render_commute(32, healthy, SELF_TEST_NOW, 8)
    timeline = render_commute(32, disrupted, SELF_TEST_NOW, 8)
    empty = render_commute(32, [], SELF_TEST_NOW, 8)
    assert len({hero.tobytes(), timeline.tobytes(), empty.tobytes()}) == 3

    # A hero countdown must match what the rules say, so the screen and the
    # data can never disagree.
    expected = trains.minutes_to_leave(healthy[0], SELF_TEST_NOW, 8)
    assert hero.tobytes() == render_commute_hero(32, healthy[0], expected).tobytes()


@self_test("commute-cancelled-goes-to-timeline")
def _check_commute_cancelled_goes_to_timeline() -> None:
    # A cancelled next train must never be counted down to. Showing the
    # timeline is how the rider gets their actual options instead.
    rows = [_departure(6, cancelled=True), _departure(20)]
    frame = render_commute(32, rows, SELF_TEST_NOW, 8)
    # Next one is cancelled, so this is the timeline, not a countdown to a
    # train that isn't running.
    assert frame.tobytes() == render_commute_timeline(32, rows, SELF_TEST_NOW).tobytes()


@self_test("commute-stale-is-dimmer")
def _check_commute_stale_is_dimmer() -> None:
    rows = [_departure(12), _departure(19)]
    fresh = render_commute(32, rows, SELF_TEST_NOW, 8)
    stale = render_commute(32, rows, SELF_TEST_NOW, 8, stale=True)
    assert fresh.tobytes() != stale.tobytes()

    def brightness(frame):
        return sum(sum(frame.getpixel((x, y))) for y in range(32) for x in range(32))

    # A countdown from stale data keeps ticking and looks authoritative.
    # Dimming is what makes the fault visible.
    assert brightness(stale) < brightness(fresh) * 0.6

```

- [ ] **Step 2: Run to verify they fail**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test commute`
Expected: FAIL — `NameError: name 'render_commute' is not defined`.

- [ ] **Step 3: Implement**

Add after `render_commute_empty`:

```python
COMMUTE_STALE_FACTOR = 0.35


def render_commute(
    size: int,
    departures: list[trains.Departure],
    now: datetime.datetime,
    walk_minutes: int,
    platform: str = "B",
    stale: bool = False,
) -> Image.Image:
    """Pick the screen that tells the truth about the current situation.

    When `stale` the whole frame is dimmed. The caller is responsible for
    freezing the clock it passes as `now`, so the countdown stops advancing
    rather than confidently counting down from data we no longer trust.
    """
    if not departures:
        frame = render_commute_empty(size, platform)
    elif trains.is_disrupted(departures):
        frame = render_commute_timeline(size, departures, now)
    else:
        options = trains.catchable(departures)
        if not options:
            frame = render_commute_empty(size, platform)
        else:
            target = options[0]
            frame = render_commute_hero(
                size, target, trains.minutes_to_leave(target, now, walk_minutes)
            )

    if stale:
        frame = Image.eval(frame, lambda channel: int(channel * COMMUTE_STALE_FACTOR))
    return frame


def render_commute_previews(directory: Path, size: int) -> None:
    """Every commute state as a PNG. No token, no network, no hardware."""
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime(2026, 7, 27, 7, 30)

    def at(minutes: int, *, cancelled: bool = False, late: int = 0) -> trains.Departure:
        return sample_departure(now, minutes, cancelled=cancelled, late=late)

    scenes = {
        "commute-comfortable": ([at(16), at(21), at(28)], False),
        "commute-hurry": ([at(11), at(18), at(25)], False),
        "commute-now": ([at(8), at(15), at(22)], False),
        "commute-delayed": ([at(10, late=6), at(18), at(25)], False),
        "commute-cancelled": ([at(10, cancelled=True), at(18), at(25)], False),
        "commute-empty": ([], False),
        "commute-stale": ([at(16), at(21), at(28)], True),
    }
    for name, (departures, stale) in scenes.items():
        render_commute(size, departures, now, 8, stale=stale).save(directory / f"{name}.png")
```

Add the argparse entry beside `--preview-frames`:

```python
    parser.add_argument(
        "--preview-commute",
        type=Path,
        help="Render every commute screen state to PNGs and exit. No RTT token needed.",
    )
```

And the dispatch branch in `run()`, with the other no-Spotify modes:

```python
    if args.preview_commute:
        render_commute_previews(args.preview_commute, min(args.rows, args.cols))
        return
```

- [ ] **Step 4: Run to verify they pass, then eyeball the output**

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test commute`
Expected: `self-test: 3/3 passed`.

Run: `/tmp/mvenv/bin/python spotify_matrix.py --self-test`
Expected: `self-test: 28/28 passed`.

Render and inspect at 8× nearest-neighbour:

```bash
/tmp/mvenv/bin/python spotify_matrix.py --rows 32 --cols 32 --preview-commute /tmp/commute
/tmp/mvenv/bin/python - <<'EOF'
from PIL import Image
from pathlib import Path
for p in sorted(Path('/tmp/commute').glob('*.png')):
    im = Image.open(p)
    im.resize((im.width * 8, im.height * 8), Image.NEAREST).save(p.with_name(p.stem + '-big.png'))
EOF
```

Open the `-big.png` files and confirm: nothing clipped at the edges, the hero
number dominates, `DLY` is amber and `CAN` is red, and the stale frame is
clearly dimmer but still readable.

- [ ] **Step 5: Document**

In `README.md`, under "Preview the render on any machine (no Pi hardware)":

```markdown
# every morning-commute screen state (no RTT token needed)
python spotify_matrix.py --rows 32 --cols 32 --preview-commute /tmp/commute
```

In `CLAUDE.md`, under "Testing without hardware":

```markdown
- `--preview-commute dir/` — every commute screen state (comfortable, hurry, now, delayed, cancelled, empty, stale). Credential-free; Stage 1 of the commute feature renders from hand-built `Departure` objects, so this works before any RTT token exists.
```

Also add a short section to `CLAUDE.md` recording why the font is variable-width — it looks like an odd choice until you know:

```markdown
## Commute screen (Stage 1: rendering)

`tinyfont.py` (font + seven-segment digits) and `trains.py` (`Departure` model
+ pure selection rules) back `render_commute()`. Stage 1 has no network code.

**Why the font is variable-width:** at a fixed 3px, `N` renders identically to
`M`, and `M`/`W` collapse into `N`/`U`. `N` gets 4 columns for its diagonal and
`W` gets 5. Confirmed by rendering, not assumed — see `--self-test font`.

**Why `1` is special-cased in the seven-segment digits:** a true seven-segment
`1` lights the two right-hand bars, so `11` reads as four evenly spaced posts.
It is drawn as a single stem with a narrow advance instead.

**Red means cancelled, never "hurry".** Urgency tops out at amber so red always
carries exactly one meaning.
```

- [ ] **Step 6: Commit**

```bash
git add spotify_matrix.py README.md CLAUDE.md
git commit -m "Add the commute screen dispatcher, stale dimming and --preview-commute"
```

---

## Stage 1 exit criteria

- `python spotify_matrix.py --self-test` reports 28/28 passing.
- `--preview-commute` writes seven PNGs covering every state, with no RTT token and no network.
- On the Pi: `sudo -E .venv/bin/python spotify_matrix.py --rows 32 --cols 32 --rotate 90 --preview-commute /tmp/c` succeeds, and the frames are legible at wall-mounted distance.
- No change to `requirements.txt`, and no change to the running service's behaviour — nothing calls `render_commute()` from the render loop until Stage 3.

## Deferred to later stages

- `RttClient`, bearer-token auth, response parsing and the recorded fixture — **Stage 2**.
- Poll thread, `RequestBudget` wiring, `CommuteSchedule`, render-loop branch and record/commute alternation — **Stage 3**.
