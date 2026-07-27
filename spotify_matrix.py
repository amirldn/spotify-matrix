#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime
from io import BytesIO
import json
import math
import os
import random
import secrets
import threading
import time
import urllib.parse
import urllib.request
from email.message import Message
from urllib.error import HTTPError
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageOps

import tinyfont

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SCOPE = "user-read-currently-playing"

# Poll pacing. Track changes are bursty - someone who skips once usually skips
# again within seconds - so any observed change drops the poller into a brief
# "hot" tier where a follow-up skip shows up almost immediately. These describe
# behaviour rather than taste, so they stay constants instead of CLI flags.
HOT_POLL_SECONDS = 1.0
HOT_WINDOW_SECONDS = 15.0

# Network failure handling. Two strikes before we call it offline (a single
# blip shouldn't light the alarm), and a minute of grace before we stop
# believing the last known playback state.
OFFLINE_STRIKES = 2
OFFLINE_IDLE_SECONDS = 60.0

STATUS_OK = "ok"
STATUS_OFFLINE = "offline"
STATUS_RATE_LIMITED = "rate_limited"


@dataclass
class PlaybackArt:
    key: str
    image_url: str
    is_playing: bool


@dataclass
class SharedPlaybackState:
    art_key: str | None = None
    image_url: str | None = None
    image: Image.Image | None = None
    is_playing: bool = False
    # How the poll thread is faring, for the idle status dot: one of
    # STATUS_OK / STATUS_OFFLINE / STATUS_RATE_LIMITED.
    status: str = STATUS_OK


@dataclass
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> HttpResponse:
    if params:
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers=headers or {},
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.headers, response.read())
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.headers, exc.read())


def raise_http_error(response: HttpResponse, context: str) -> None:
    body = response.body.decode("utf-8", errors="replace")
    raise RuntimeError(f"{context} failed with HTTP {response.status}: {body}")


class RequestBudget:
    """Token bucket capping how many Spotify requests we make per minute.

    This is the real rate-limit control. A slow poll cadence is only an
    *indirect* limit - it couples request volume to how long the panel takes to
    notice you pressed play. A bucket is a direct one: volume is capped no
    matter what the pacing logic asks for, which is exactly what lets the
    pacing be aggressive. It also fails safe, since a bug that tightens the
    cadence to zero still cannot exceed the ceiling.
    """

    def __init__(self, per_minute: float) -> None:
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self.tokens = per_minute
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_seconds(self) -> float:
        """Seconds until a token is available (0 if one is free right now)."""
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def consume(self) -> None:
        self._refill()
        self.tokens = max(0.0, self.tokens - 1.0)


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_cache: Path,
        open_browser: bool,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_cache = token_cache
        self.open_browser = open_browser
        self.token = self._load_token()
        # Monotonic-ish wall-clock deadline: while now < this, we're honoring a
        # Spotify 429 back-off and skip polling entirely (see get_currently_playing).
        self.rate_limited_until = 0.0

    def backoff_remaining(self) -> float:
        """Seconds left on a 429 back-off (0 when we're free to poll).

        The poll loop uses this to skip both the request and its budget token
        during a ban, and to light the rate-limited status dot.
        """
        return max(0.0, self.rate_limited_until - time.time())

    def get_currently_playing(self, refresh_retry: bool = True) -> dict[str, Any] | None:
        # Honor a prior 429 without blocking the poll thread: during the back-off
        # window we simply report "nothing playing" and make no request at all, so
        # the render loop stays alive and we auto-recover the moment it expires.
        if time.time() < self.rate_limited_until:
            return None

        token = self._valid_access_token()
        response = http_request(
            "GET",
            CURRENTLY_PLAYING_URL,
            params={"additional_types": "track,episode"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status == 204:
            return None
        if response.status == 401:
            # Refresh and retry exactly once. If it STILL 401s, do not recurse:
            # an unbounded refresh->retry loop hammers the API and is a prime way
            # to earn a multi-hour 429 ban. Back off briefly and report idle.
            if not refresh_retry:
                self.rate_limited_until = time.time() + 30
                print("Spotify: still unauthorized after refresh; backing off 30s", flush=True)
                return None
            self._refresh_access_token()
            return self.get_currently_playing(refresh_retry=False)
        if response.status == 429:
            retry_after = max(int(response.headers.get("Retry-After", "5")), 1)
            # Do NOT sleep here: Spotify can return a multi-hour Retry-After, and
            # sleeping on the poll thread would freeze the display for that long.
            # Instead record a deadline and return; subsequent polls short-circuit.
            self.rate_limited_until = time.time() + retry_after
            print(f"Spotify rate-limited (429); backing off {retry_after}s", flush=True)
            return None
        if response.status != 200:
            raise_http_error(response, "Spotify currently-playing request")

        return response.json()

    def authorize(self) -> None:
        self._valid_access_token()

    def _valid_access_token(self) -> str:
        if not self.token:
            self.token = self._authorize()

        if time.time() >= float(self.token.get("expires_at", 0)):
            self._refresh_access_token()

        return str(self.token["access_token"])

    def _load_token(self) -> dict[str, Any] | None:
        if not self.token_cache.exists():
            return None

        with self.token_cache.open("r", encoding="utf-8") as token_file:
            return json.load(token_file)

    def _save_token(self, token: dict[str, Any]) -> None:
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60

        previous_refresh_token = self.token.get("refresh_token") if self.token else None
        if previous_refresh_token and "refresh_token" not in token:
            token["refresh_token"] = previous_refresh_token

        with self.token_cache.open("w", encoding="utf-8") as token_file:
            json.dump(token, token_file, indent=2)

        self.token = token

    def _authorize(self) -> dict[str, Any]:
        state = secrets.token_urlsafe(18)
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("This script expects a localhost Spotify redirect URI.")

        callback = LocalCallbackServer(
            host=parsed_redirect.hostname or "127.0.0.1",
            port=parsed_redirect.port or 80,
            path=parsed_redirect.path or "/callback",
            expected_state=state,
        )

        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPE,
                "state": state,
            }
        )
        auth_url = f"{AUTH_URL}?{query}"

        print("Authorize Spotify in your browser:")
        print(auth_url)
        if self.open_browser:
            webbrowser.open(auth_url)

        code = callback.wait_for_code()
        token = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        self._save_token(token)
        return token

    def _refresh_access_token(self) -> None:
        refresh_token = self.token.get("refresh_token") if self.token else None
        if not refresh_token:
            self.token = self._authorize()
            return

        token = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._save_token(token)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        basic_auth = base64.b64encode(credentials).decode("ascii")
        response = http_request(
            "POST",
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        if response.status != 200:
            raise_http_error(response, "Spotify token request")
        return response.json()


class LocalCallbackServer:
    def __init__(self, host: str, port: int, path: str, expected_state: str) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.state_error: str | None = None
        self.path = path
        self.expected_state = expected_state

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if parsed.path != parent.path:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Wrong callback path.")
                    return

                returned_state = params.get("state", [""])[0]
                if returned_state != parent.expected_state:
                    parent.state_error = "Spotify callback state did not match."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    return

                if "error" in params:
                    parent.error = params["error"][0]
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Spotify authorization failed.")
                    return

                parent.code = params.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Spotify authorization complete. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = HTTPServer((host, port), Handler)

    def wait_for_code(self) -> str:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            while not self.code and not self.error and not self.state_error:
                time.sleep(0.1)
        finally:
            self.server.shutdown()
            self.server.server_close()

        if self.state_error:
            raise RuntimeError(self.state_error)
        if self.error:
            raise RuntimeError(f"Spotify authorization failed: {self.error}")
        if not self.code:
            raise RuntimeError("Spotify authorization did not return a code.")
        return self.code


# Map a clockwise rotation (degrees) to the equivalent PIL transpose. PIL's own
# rotate() counts counter-clockwise, so 90 CW == ROTATE_270. Used to reorient the
# whole output when the panel is mounted rotated (e.g. on a wall).
_ROTATE_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def rotate_frame(image: Image.Image, degrees: int) -> Image.Image:
    transpose = _ROTATE_TRANSPOSE.get(degrees % 360)
    return image.transpose(transpose) if transpose else image


class MatrixDisplay:
    def __init__(self, args: argparse.Namespace) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as exc:
            raise RuntimeError(
                "The rgbmatrix Python bindings are not installed. "
                "Install hzeller/rpi-rgb-led-matrix on the Pi, or run with --mock-output."
            ) from exc

        options = RGBMatrixOptions()
        options.rows = args.rows
        options.cols = args.cols
        options.chain_length = args.chain_length
        options.parallel = args.parallel
        options.brightness = args.brightness
        options.gpio_slowdown = args.gpio_slowdown
        options.hardware_mapping = args.hardware_mapping
        options.pwm_bits = args.pwm_bits
        options.limit_refresh_rate_hz = args.limit_refresh_rate_hz
        options.disable_hardware_pulsing = args.no_hardware_pulse
        # By default the library drops root to user 'daemon' right after init.
        # That breaks the poll thread's album-art HTTP download (it can no longer
        # fetch/decode the image). We launch with sudo intentionally, so keep root.
        options.drop_privileges = False

        self.rotate = args.rotate
        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()

    def show(self, image: Image.Image) -> None:
        self.canvas.SetImage(rotate_frame(image, self.rotate).convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self) -> None:
        self.matrix.Clear()


class MockDisplay:
    def __init__(self, output: Path, rotate: int = 0) -> None:
        self.output = output
        self.rotate = rotate
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def show(self, image: Image.Image) -> None:
        rotate_frame(image, self.rotate).save(self.output)

    def clear(self) -> None:
        return


def demo_album_art(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size // 2, size // 2), fill=(238, 70, 60))
    draw.rectangle((size // 2, 0, size, size // 2), fill=(245, 180, 40))
    draw.rectangle((0, size // 2, size // 2, size), fill=(35, 150, 235))
    draw.rectangle((size // 2, size // 2, size, size), fill=(65, 185, 95))
    draw.line((0, 0, size, size), fill=(255, 255, 255), width=max(2, size // 18))
    draw.line((size, 0, 0, size), fill=(0, 0, 0), width=max(2, size // 22))
    return image


def playback_art_from_response(playback: dict[str, Any] | None) -> PlaybackArt | None:
    if not playback:
        return None

    item = playback.get("item")
    if not item:
        return None

    item_type = item.get("type")
    if item_type == "track":
        images = item.get("album", {}).get("images", [])
    else:
        images = item.get("images", [])

    if not images:
        return None

    image = max(images, key=lambda candidate: candidate.get("width") or 0)
    item_id = item.get("id") or item.get("uri") or image["url"]
    return PlaybackArt(
        key=str(item_id),
        image_url=image["url"],
        is_playing=bool(playback.get("is_playing")),
    )


def download_image(url: str) -> Image.Image:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


def render_record(art: Image.Image | None, angle: float, size: int) -> Image.Image:
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    if art is None:
        return frame.convert("RGB")

    margin = size // 40  # ~0 on a 32x32 so the disc reaches the panel edge
    disc_size = size - margin * 2
    # The album art is the record surface: rotate it first, then cut it into a circular disk.
    art_square = ImageOps.fit(art, (disc_size, disc_size), method=Image.Resampling.LANCZOS)
    rotated = art_square.rotate(angle, resample=Image.Resampling.BICUBIC)

    disc_mask = Image.new("L", (disc_size, disc_size), 0)
    mask_draw = ImageDraw.Draw(disc_mask)
    mask_draw.ellipse((0, 0, disc_size - 1, disc_size - 1), fill=255)
    frame.paste(rotated.convert("RGBA"), (margin, margin), disc_mask)

    draw = ImageDraw.Draw(frame, "RGBA")
    outer = (margin, margin, size - margin - 1, size - margin - 1)
    draw.ellipse(outer, outline=(6, 6, 6, 255), width=max(1, size // 32))

    center = size // 2
    # Radii scale with the panel so the label stays proportional at any resolution
    # (a fixed floor tuned for 64x64 would swallow a 32x32 disc).
    label_radius = max(3, size // 7)
    hole_radius = max(1, size // 24)  # ~3px on a 32x32 - smallest hole that still reads
    draw.ellipse(
        (
            center - label_radius,
            center - label_radius,
            center + label_radius,
            center + label_radius,
        ),
        fill=(16, 16, 16, 210),
        outline=(220, 220, 220, 90),
    )
    draw.ellipse(
        (
            center - hole_radius,
            center - hole_radius,
            center + hole_radius,
            center + hole_radius,
        ),
        fill=(0, 0, 0, 255),
    )
    return frame.convert("RGB")


def render_idle(size: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    margin = max(2, size // 32)
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), outline=(55, 55, 55), width=2)
    center = size // 2
    radius = max(3, size // 18)
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=(18, 18, 18))
    return frame


def render_clock(size: int, when: datetime.datetime) -> Image.Image:
    # A dim analog clock on the faint record ring - the idle screen turned timepiece.
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    cx = cy = (size - 1) / 2.0
    r = size / 2.0 - max(1, size // 40) - 1

    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(45, 45, 52), width=1)

    def hand_point(theta: float, length: float) -> tuple[float, float]:
        return cx + length * math.sin(theta), cy - length * math.cos(theta)

    # Tick marks: brighter at 12/3/6/9, faint for the rest.
    for hour in range(12):
        theta = math.radians(hour * 30)
        cardinal = hour % 3 == 0
        outer = hand_point(theta, r - 1)
        inner = hand_point(theta, r - (3 if cardinal else 2))
        draw.line((*inner, *outer), fill=(85, 85, 95) if cardinal else (50, 50, 58), width=1)

    hour_theta = math.radians((when.hour % 12 + when.minute / 60.0) * 30)
    minute_theta = math.radians(when.minute * 6)
    draw.line((cx, cy, *hand_point(hour_theta, r * 0.5)), fill=(120, 120, 135), width=2)
    draw.line((cx, cy, *hand_point(minute_theta, r * 0.78)), fill=(155, 155, 175), width=1)
    draw.ellipse((cx - 1, cy - 1, cx + 1, cy + 1), fill=(95, 95, 108))
    return frame


# --- Idle status dot ---------------------------------------------------------
# One LED in the panel's top-right corner so a stalled display explains itself:
# red for "can't reach the network", blue for "Spotify is rate-limiting us,
# waiting it out". Colour alone is a weak signal on a single pixel across a dark
# room, so each state gets its own rhythm too - an urgent 1Hz pulse versus a
# patient 4s breathe. The motion also distinguishes a status dot from the dead
# pixel a lone steady LED would look like.
_STATUS_DOT_STYLE = {
    STATUS_OFFLINE: ((210, 35, 35), 1.0, 0.35),
    STATUS_RATE_LIMITED: ((45, 110, 255), 4.0, None),
}

# The dot must land in the top-right of what the *panel* shows, and
# MatrixDisplay.show() rotates the frame afterwards. So pick the source corner
# that rotation carries to (size-1, 0). Easy to get backwards - hence the
# rotation round-trip check in --self-test.
_STATUS_DOT_CORNER = {
    0: lambda size: (size - 1, 0),
    90: lambda size: (0, 0),
    180: lambda size: (0, size - 1),
    270: lambda size: (size - 1, size - 1),
}


def status_dot_position(size: int, rotate: int) -> tuple[int, int]:
    return _STATUS_DOT_CORNER[rotate % 360](size)


def status_dot_level(status: str, elapsed: float) -> float:
    """Brightness envelope in [0, 1] for the given status at time `elapsed`.

    Driven off a monotonic clock rather than a frame counter, so the rhythm is
    the same whether we're rendering at 120fps or limping.
    """
    style = _STATUS_DOT_STYLE.get(status)
    if style is None:
        return 0.0

    _, period, duty = style
    phase = (elapsed % period) / period
    if duty is None:
        # Breathe: a full sine swing that never fully extinguishes, so the dot
        # stays readable as "present but waiting".
        return 0.25 + 0.75 * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
    if phase >= duty:
        return 0.0
    # Pulse: a short lit burst with soft edges, dark the rest of the period.
    return math.sin(math.pi * phase / duty)


def draw_status_dot(
    frame: Image.Image, status: str, elapsed: float, rotate: int
) -> Image.Image:
    level = status_dot_level(status, elapsed)
    if level <= 0.0:
        return frame

    color, _, _ = _STATUS_DOT_STYLE[status]
    # Copy: idle frames are cached and reused across renders, so painting in
    # place would permanently stain them.
    dotted = frame.copy()
    dotted.putpixel(
        status_dot_position(frame.size[0], rotate),
        tuple(round(channel * level) for channel in color),
    )
    return dotted


def render_test_pattern(size: int, offset: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    colors = (
        (255, 0, 0),
        (255, 160, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 120, 255),
        (80, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    )
    stripe_width = max(1, size // len(colors))
    for index, color in enumerate(colors):
        x0 = (index * stripe_width + offset) % size
        draw.rectangle((x0, 0, min(size - 1, x0 + stripe_width - 1), size - 1), fill=color)
        if x0 + stripe_width > size:
            draw.rectangle((0, 0, (x0 + stripe_width) % size, size - 1), fill=color)
    draw.rectangle((0, 0, size - 1, size - 1), outline=(255, 255, 255))
    return frame


# --- Song-change transitions -------------------------------------------------
# Each transition is constructed once per song change with the old and new
# rendered disc frames, then called per rendered frame with t in [0, 1] and
# returns the composited frame. Constructing up front lets each precompute its
# own state (noise map, spin direction, glitch RNG) - no shared state.


class Crossfade:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new

    def __call__(self, t: float) -> Image.Image:
        return Image.blend(self.old, self.new, t)


class PixelDissolve:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        # A fixed uniform-random field; each pixel crosses over once t passes its value.
        field = bytes(random.randrange(256) for _ in range(size * size))
        self.noise = Image.frombytes("L", (size, size), field)

    def __call__(self, t: float) -> Image.Image:
        cutoff = int(t * 255)
        mask = self.noise.point(lambda p: 255 if p <= cutoff else 0)
        return Image.composite(self.new, self.old, mask)


class Iris:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.size = size

    def __call__(self, t: float) -> Image.Image:
        mask = Image.new("L", (self.size, self.size), 0)
        r = t * self.size * 0.72  # 0.72*size > center-to-corner, so t=1 fully reveals
        c = self.size / 2
        ImageDraw.Draw(mask).ellipse((c - r, c - r, c + r, c + r), fill=255)
        return Image.composite(self.new, self.old, mask)


class RecordSwap:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.size = size

    def __call__(self, t: float) -> Image.Image:
        s = self.size
        frame = Image.new("RGB", (s, s), (0, 0, 0))
        offset = int(round(t * s))
        frame.paste(self.old, (0, -offset))      # old lifts up and out the top
        frame.paste(self.new, (0, s - offset))   # new rises into place from below
        return frame


class FlipSide:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.size = size

    def __call__(self, t: float) -> Image.Image:
        s = self.size
        frame = Image.new("RGB", (s, s), (0, 0, 0))
        if t < 0.5:
            width = max(1, int(round(s * (1.0 - t * 2.0))))
            img = self.old
        else:
            width = max(1, int(round(s * (t * 2.0 - 1.0))))
            img = self.new
        squashed = img.resize((width, s), Image.BILINEAR)
        frame.paste(squashed, ((s - width) // 2, 0))
        return frame


class SpinWhip:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.direction = random.choice((-1, 1))

    def __call__(self, t: float) -> Image.Image:
        img = self.old if t < 0.5 else self.new  # swap under cover of peak blur
        smear = math.sin(math.pi * t) * 45.0      # degrees of motion blur, peaks mid
        base_spin = t * 90.0 * self.direction
        samples = 5
        acc = None
        for i in range(samples):
            frac = i / (samples - 1)
            angle = base_spin + self.direction * smear * (frac - 0.5)
            rotated = img.rotate(angle, resample=Image.BILINEAR)
            acc = rotated if acc is None else Image.blend(acc, rotated, 1.0 / (i + 1))
        return acc


class TonearmSweep:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.size = size
        self.start = random.uniform(0.0, 360.0)

    def __call__(self, t: float) -> Image.Image:
        s = self.size
        if t >= 1.0:
            return self.new
        mask = Image.new("L", (s, s), 0)
        if t > 0.0:
            ImageDraw.Draw(mask).pieslice(
                (0, 0, s - 1, s - 1), self.start, self.start + t * 360.0, fill=255
            )
        return Image.composite(self.new, self.old, mask)


class ScratchGlitch:
    def __init__(self, old: Image.Image, new: Image.Image, size: int) -> None:
        self.old = old
        self.new = new
        self.size = size
        self.rng = random.Random(random.random())

    def __call__(self, t: float) -> Image.Image:
        s = self.size
        base = Image.blend(self.old, self.new, t)  # crossfade underneath the glitch
        intensity = math.sin(math.pi * t)          # glitch strongest mid-transition
        glitched = base.copy()
        band_h = max(1, s // 5)
        for y in range(0, s, band_h):
            if self.rng.random() < 0.6 * intensity:
                dx = self.rng.randint(-4, 4)
                band = base.crop((0, y, s, min(s, y + band_h)))
                glitched.paste(band, (dx, y))
        offset = int(round(3 * intensity))
        if offset > 0:
            r, g, b = glitched.split()
            glitched = Image.merge(
                "RGB", (ImageChops.offset(r, offset, 0), g, ImageChops.offset(b, -offset, 0))
            )
        return glitched


TRANSITIONS = [
    Crossfade,
    PixelDissolve,
    Iris,
    RecordSwap,
    FlipSide,
    SpinWhip,
    TonearmSweep,
    ScratchGlitch,
]


def pick_transition(last: type | None) -> type:
    choices = [tx for tx in TRANSITIONS if tx is not last] or TRANSITIONS
    return random.choice(choices)


def compute_poll_delay(
    is_playing: bool,
    seconds_since_change: float,
    poll_seconds: float,
    idle_poll_seconds: float,
) -> float:
    """Decide how long to wait before the next Spotify poll.

    Three tiers, chosen by playback state and how recently anything changed:

    - Hot: something changed in the last HOT_WINDOW_SECONDS. Track changes are
      bursty - skipping once usually means skipping again in a moment - so poll
      hard for a short window. The first skip costs poll_seconds; every skip
      after it lands in about a second.
    - Playing: the base cadence. This is the ceiling on how long a skip can go
      unnoticed.
    - Idle or paused: the slow cadence, which is also how long pressing play can
      go unnoticed.

    Deliberately *not* predictive. An earlier version waited until the track
    boundary computed from progress_ms/duration_ms, which is optimal for a track
    ending by itself and pessimal for a manual skip: it assumes the art cannot
    change before the boundary, which is exactly wrong when the user intervenes.
    Request volume is now held down by RequestBudget instead, which does not
    trade away latency to do it.
    """
    if seconds_since_change < HOT_WINDOW_SECONDS:
        return HOT_POLL_SECONDS
    return poll_seconds if is_playing else idle_poll_seconds


def poll_spotify(
    spotify: SpotifyClient,
    state: SharedPlaybackState,
    state_lock: threading.Lock,
    stop_event: threading.Event,
    poll_seconds: float,
    idle_poll_seconds: float,
    budget: RequestBudget,
) -> None:
    last_status: str | None = None
    last_change = 0.0            # monotonic time we last saw the playback change
    failures = 0                 # consecutive network-level failures
    offline_since: float | None = None

    while not stop_event.is_set():
        backoff = spotify.backoff_remaining()
        if backoff > 0:
            # Rate-limited: there's no request to make, so spend no token
            # either. Re-check often enough that the dot clears promptly once
            # the window expires.
            with state_lock:
                state.status = STATUS_RATE_LIMITED
            stop_event.wait(min(backoff, idle_poll_seconds))
            continue

        wait = budget.wait_seconds()
        if wait > 0:
            stop_event.wait(wait)
            continue

        budget.consume()
        delay = poll_seconds
        try:
            playback = spotify.get_currently_playing()
            art = playback_art_from_response(playback)
            failures = 0
            offline_since = None

            # A 429 answered by the request we just made shows up here as a
            # back-off deadline; reflect it now so the dot doesn't blink
            # through "ok" for one cycle before turning blue.
            health = STATUS_RATE_LIMITED if spotify.backoff_remaining() > 0 else STATUS_OK

            if art:
                with state_lock:
                    needs_download = art.key != state.art_key or art.image_url != state.image_url
                    changed = art.key != state.art_key or art.is_playing != state.is_playing

                image = download_image(art.image_url) if needs_download else None

                with state_lock:
                    state.art_key = art.key
                    state.image_url = art.image_url
                    state.is_playing = art.is_playing
                    state.status = health
                    if image is not None:
                        state.image = image

                status = f"art found, is_playing={art.is_playing}"
            else:
                with state_lock:
                    changed = state.art_key is not None or state.is_playing
                    state.art_key = None
                    state.image_url = None
                    state.image = None
                    state.is_playing = False
                    state.status = health
                status = "no currently playing item"

            if changed:
                last_change = time.monotonic()

            if status != last_status:
                print(f"Spotify: {status}", flush=True)
                last_status = status

            delay = compute_poll_delay(
                is_playing=bool(art and art.is_playing),
                seconds_since_change=time.monotonic() - last_change,
                poll_seconds=poll_seconds,
                idle_poll_seconds=idle_poll_seconds,
            )
        except OSError as exc:
            # http_request turns HTTP error *responses* into HttpResponse
            # objects, so an OSError escaping it (URLError, socket timeout, DNS
            # failure) genuinely means we couldn't reach Spotify at all.
            failures += 1
            now = time.monotonic()
            if offline_since is None:
                offline_since = now

            with state_lock:
                if failures >= OFFLINE_STRIKES:
                    state.status = STATUS_OFFLINE
                if now - offline_since >= OFFLINE_IDLE_SECONDS:
                    # A sustained outage means we no longer know what's playing.
                    # Drop to the idle screen - which is where the red dot
                    # lives - instead of spinning stale art indefinitely.
                    state.art_key = None
                    state.image_url = None
                    state.image = None
                    state.is_playing = False

            status = f"unreachable ({exc})"
            if status != last_status:
                print(f"Spotify: {status}", flush=True)
                last_status = status
            delay = idle_poll_seconds
        except Exception as exc:
            print(f"Spotify poll failed: {exc}", flush=True)
            delay = idle_poll_seconds

        stop_event.wait(delay)


SELF_TESTS: list[tuple[str, Any]] = []


def self_test(name: str):
    """Register a named check runnable via --self-test [PATTERN]."""

    def register(func):
        SELF_TESTS.append((name, func))
        return func

    return register


@self_test("status-dot-rotation")
def _check_status_dot_rotation() -> None:
    # The dot must reach the panel's top-right for every rotation, and it must
    # be exactly one pixel. Getting this backwards is invisible until the
    # panel is on the wall.
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
    for a, b in (("N", "H"), ("V", "W"), ("O", "0"), ("S", "5")):
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


def build_display(args: argparse.Namespace) -> MatrixDisplay | MockDisplay:
    if args.mock_output:
        return MockDisplay(args.mock_output, args.rotate)
    return MatrixDisplay(args)


def run(args: argparse.Namespace) -> None:
    # Modes that never touch Spotify run first, so they never need credentials.
    if args.self_test is not None:
        run_self_test(args.self_test or None)
        return

    if args.preview_frames:
        render_preview_frames(args.preview_frames, min(args.rows, args.cols), args.rotate)
        return

    if args.preview_transitions:
        render_transition_previews(args.preview_transitions)
        return

    size = min(args.rows, args.cols)

    if args.test_pattern:
        display = build_display(args)
        try:
            offset = 0
            while True:
                display.show(render_test_pattern(size, offset))
                offset = (offset + 1) % size
                time.sleep(1.0 / args.fps)
        except KeyboardInterrupt:
            pass
        finally:
            display.clear()
        return

    load_dotenv()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
            ("SPOTIFY_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    spotify = SpotifyClient(
        client_id=client_id or "",
        client_secret=client_secret or "",
        redirect_uri=redirect_uri,
        token_cache=args.token_cache,
        open_browser=not args.no_browser,
    )

    if args.auth_only:
        spotify.authorize()
        print(f"Spotify token cached at {args.token_cache}")
        return

    display = build_display(args)
    idle = render_idle(size)
    playback_state = SharedPlaybackState()
    playback_lock = threading.Lock()
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_spotify,
        args=(
            spotify,
            playback_state,
            playback_lock,
            stop_event,
            args.poll_seconds,
            args.idle_poll_seconds,
            RequestBudget(args.max_requests_per_minute),
        ),
        daemon=True,
    )
    poll_thread.start()

    angle = 0.0
    spin_velocity = 0.0  # degrees/sec, eased toward the target speed
    full_speed = 360.0 * (args.rpm / 60.0)
    last_frame = time.monotonic()

    displayed_image: Image.Image | None = None
    displayed_key: str | None = None
    active_transition = None
    transition_end = 0.0
    last_transition_cls: type | None = None
    idle_since: float | None = None      # monotonic time we went idle (None = playing)
    last_idle_frame = idle               # what the idle screen currently shows (ring or clock)

    try:
        while True:
            frame_start = time.monotonic()
            with playback_lock:
                new_image = playback_state.image
                new_key = playback_state.art_key
                is_playing = playback_state.is_playing
                status = playback_state.status

            now = time.monotonic()
            delta = now - last_frame
            last_frame = now

            # Start a transition when the shown art changes and we're moving to real
            # album art. This covers idle -> playing (transition in from the ghost
            # record) as well as song -> song. Fully stopping (-> idle) stays instant
            # to avoid spurious fade-outs on brief blips between tracks.
            if (
                not args.no_transitions
                and active_transition is None
                and new_key != displayed_key
                and new_image is not None
            ):
                old_frame = (
                    render_record(displayed_image, angle, size)
                    if displayed_image is not None
                    else last_idle_frame  # transition out from whatever idle showed (ring or clock)
                )
                new_frame = render_record(new_image, angle, size)
                cls = pick_transition(last_transition_cls)
                last_transition_cls = cls
                active_transition = cls(old_frame, new_frame, size)
                transition_end = now + args.transition_seconds
                spin_velocity = 0.0  # hold the record still while it swaps
                displayed_image = new_image
                displayed_key = new_key

            if active_transition is not None:
                # Play the transition; keep the disc angle frozen (the "hold").
                t = 1.0 - max(0.0, transition_end - now) / args.transition_seconds
                if t >= 1.0:
                    active_transition = None
                    image = render_record(displayed_image, angle, size) if displayed_image else idle
                else:
                    image = active_transition(t)
            else:
                # No transition: adopt the current art and run the normal spin model.
                displayed_image = new_image
                displayed_key = new_key
                target_speed = full_speed if (is_playing and displayed_image is not None) else 0.0
                # Ease velocity toward target: spin-up on play, coast-down on pause.
                spin_velocity += (target_speed - spin_velocity) * (1.0 - math.exp(-delta / args.spin_lag))
                if target_speed == 0.0 and spin_velocity < 1.0:
                    spin_velocity = 0.0  # snap the final crawl to a clean stop
                angle = (angle - spin_velocity * delta) % 360.0

                if displayed_image is not None:
                    idle_since = None
                    image = render_record(displayed_image, angle, size)
                else:
                    # Nothing playing: ghost ring, then fade to a dim clock after a while.
                    if idle_since is None:
                        idle_since = now
                    elapsed = now - idle_since
                    if not args.no_idle_clock and elapsed >= args.idle_clock_seconds:
                        clock = render_clock(size, datetime.datetime.now())
                        image = Image.blend(idle, clock, min(1.0, elapsed - args.idle_clock_seconds))
                    else:
                        image = idle
                    # Stash the dot-free frame: transitions out of idle should
                    # start from the screen itself, not a mid-blink of the dot.
                    last_idle_frame = image
                    if not args.no_status_dot:
                        image = draw_status_dot(image, status, now, args.rotate)

            display.show(image)

            if args.once:
                break

            sleep_for = max(0.0, (1.0 / args.fps) - (time.monotonic() - frame_start))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        poll_thread.join(timeout=1)
        display.clear()


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def render_preview_frames(directory: Path, size: int, rotate: int = 0) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    art = demo_album_art(max(size * 3, 96))
    for index, angle in enumerate((0, 45, 90, 135)):
        render_record(art, angle, size).save(directory / f"album-disk-{index:02d}.png")
    render_idle(size).save(directory / "idle.png")
    render_status_previews(directory, size, rotate)


def render_status_previews(directory: Path, size: int, rotate: int) -> None:
    """Filmstrips of the idle clock with each status dot, one frame per phase.

    Rotation is applied here the way MatrixDisplay does it, so the strips show
    where the dot lands on the *panel* rather than in render space.
    """
    clock = render_clock(size, datetime.datetime(2026, 1, 1, 10, 9))
    frames = 6
    for status in (STATUS_OFFLINE, STATUS_RATE_LIMITED):
        _, period, _ = _STATUS_DOT_STYLE[status]
        strip = Image.new("RGB", (size * frames + (frames - 1), size), (30, 30, 30))
        for index in range(frames):
            dotted = draw_status_dot(clock, status, index * period / frames, rotate)
            strip.paste(rotate_frame(dotted, rotate), (index * (size + 1), 0))
        strip.save(directory / f"status-{status.replace('_', '-')}.png")


def render_transition_previews(directory: Path, size: int = 32) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    old_art = demo_album_art(size * 3)
    # A channel-rotated copy makes a clearly-different "new" album to transition to.
    r, g, b = demo_album_art(size * 3).split()
    new_art = Image.merge("RGB", (b, r, g))
    old_frame = render_record(old_art, 0.0, size)
    new_frame = render_record(new_art, 0.0, size)

    scale = 6
    strip_steps = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    gif_count = 24
    for cls in TRANSITIONS:
        # Filmstrip: a few key moments side by side.
        transition = cls(old_frame, new_frame, size)
        shots = [transition(t) for t in strip_steps]
        strip = Image.new("RGB", (size * len(shots) + 2 * (len(shots) - 1), size), (30, 30, 30))
        x = 0
        for shot in shots:
            strip.paste(shot, (x, 0))
            x += size + 2
        strip.resize((strip.width * scale, strip.height * scale), Image.NEAREST).save(
            directory / f"{cls.__name__}-strip.png"
        )

        # Animated GIF: a fresh instance so per-frame randomness (glitch) plays out.
        animation = cls(old_frame, new_frame, size)
        frames = [
            animation(i / (gif_count - 1)).resize((size * scale, size * scale), Image.NEAREST)
            for i in range(gif_count)
        ]
        frames[0].save(
            directory / f"{cls.__name__}.gif",
            save_all=True,
            append_images=frames[1:],
            duration=int(600 / gif_count),
            loop=0,
        )
    print(f"Wrote {len(TRANSITIONS)} transition previews (strips + GIFs) to {directory}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spin Spotify album art on a 64x64 RGB matrix.")
    parser.add_argument("--rows", type=int, default=64)
    parser.add_argument("--cols", type=int, default=64)
    parser.add_argument("--chain-length", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--brightness", type=int, default=65)
    parser.add_argument("--gpio-slowdown", type=int, default=2)
    parser.add_argument("--hardware-mapping", default="regular")
    parser.add_argument("--pwm-bits", type=int, default=11)
    parser.add_argument("--limit-refresh-rate-hz", type=int, default=120)
    parser.add_argument(
        "--no-hardware-pulse",
        action="store_true",
        help="Avoid Pi onboard sound conflict at the cost of more possible flicker.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=positive_float,
        default=2.0,
        help="Poll cadence while a track is playing. This is the worst-case lag "
        "before a skip shows up on the panel.",
    )
    parser.add_argument(
        "--idle-poll-seconds",
        type=positive_float,
        default=5.0,
        help="Poll cadence when idle or paused. This is the worst-case lag before "
        "pressing play shows up on the panel.",
    )
    parser.add_argument(
        "--max-requests-per-minute",
        type=positive_float,
        default=45.0,
        help="Hard ceiling on Spotify API requests, enforced by a token bucket. "
        "This - not a slow poll cadence - is what keeps us clear of a 429 ban, so "
        "the cadences above are free to be snappy.",
    )
    parser.add_argument(
        "--no-status-dot",
        action="store_true",
        help="Hide the idle corner LED that shows red when the network is "
        "unreachable and blue while Spotify is rate-limiting us.",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Rotate the whole display this many degrees CLOCKWISE. Use when the "
        "panel is mounted rotated (e.g. on a wall).",
    )
    parser.add_argument("--fps", type=positive_float, default=120.0)
    parser.add_argument("--rpm", type=positive_float, default=20.0)
    parser.add_argument("--spin-lag", type=positive_float, default=0.5, help="Seconds-scale easing for spin-up on play and coast-down on pause. Lower is snappier.")
    parser.add_argument("--transition-seconds", type=positive_float, default=1.0, help="Duration of the random album-art change transition.")
    parser.add_argument("--no-transitions", action="store_true", help="Swap album art instantly instead of animating a random transition.")
    parser.add_argument("--idle-clock-seconds", type=positive_float, default=60.0, help="Seconds of nothing playing before the idle screen fades to a dim analog clock.")
    parser.add_argument("--no-idle-clock", action="store_true", help="Keep the idle ghost record instead of showing a clock when idle.")
    parser.add_argument("--token-cache", type=Path, default=Path(".cache/spotify_token.json"))
    parser.add_argument("--mock-output", type=Path, help="Write the current frame PNG instead of using RGB matrix hardware.")
    parser.add_argument("--preview-frames", type=Path, help="Render sample spinning-album-art disk frames and exit.")
    parser.add_argument("--preview-transitions", type=Path, help="Render sample song-change transition filmstrips and GIFs, then exit.")
    parser.add_argument("--auth-only", action="store_true", help="Authorize Spotify, cache the token, and exit without using the matrix.")
    parser.add_argument("--test-pattern", action="store_true", help="Show a bright moving color test pattern without using Spotify.")
    parser.add_argument(
        "--self-test",
        nargs="?",
        const="",
        default=None,
        metavar="PATTERN",
        help="Run the built-in checks (optionally only those whose name contains PATTERN) and exit.",
    )
    parser.add_argument("--once", action="store_true", help="Render one frame and exit.")
    parser.add_argument("--no-browser", action="store_true", help="Print the Spotify auth URL without trying to open a browser.")
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
