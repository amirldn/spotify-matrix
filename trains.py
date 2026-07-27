#!/usr/bin/env python3
"""Train departure data for the morning commute screen.

Split deliberately: the model and selection rules are pure, so they stay
checkable with no token and no network, and `parse_departures` takes a plain
dict so the whole parser can be exercised against a recorded fixture.
`RttClient` is the only part that touches the network.

Targets the **next-generation** Realtime Trains API at data.rtt.io. The old
api.rtt.io shuts down 30 September 2026, so every Python wrapper on PyPI aims
at an API that is about to disappear.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Any

RTT_BASE = "https://data.rtt.io"
ACCESS_TOKEN_URL = f"{RTT_BASE}/api/get_access_token"
LOCATION_URL = f"{RTT_BASE}/rtt/location"

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
    """Services that are still running, soonest first."""
    return sorted((d for d in departures if not d.cancelled), key=lambda d: d.expected)


def in_reach(
    departures: list[Departure], now: datetime.datetime, walk_minutes: int
) -> list[Departure]:
    """Services you could still physically get to, soonest first.

    Distinct from `catchable`, which only drops cancellations: this drops
    trains leaving sooner than you can walk to the platform. Without it the
    screen fixates on a train it cannot reach, clamps the countdown to zero and
    shows LEAVE NOW - and on a line running every few minutes there is always
    such a train, so it would show LEAVE NOW permanently and never once give a
    useful countdown. Found against live data; fixtures all happened to place
    the first train beyond the walk time, so nothing caught it.

    Cancellations are deliberately kept: the caller still needs to see that the
    next train it could have reached is off.
    """
    cutoff = now + datetime.timedelta(minutes=walk_minutes)
    return sorted((d for d in departures if d.expected >= cutoff), key=lambda d: d.expected)


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


def _parse_time(value: str | None) -> datetime.datetime | None:
    """RTT returns naive ISO-8601 in the station's local time."""
    return datetime.datetime.fromisoformat(value) if value else None


def parse_departures(payload: dict[str, Any]) -> list[Departure]:
    """Turn an /rtt/location response into Departures, soonest first.

    Takes a plain dict rather than doing its own fetching, so the whole parser
    is checkable against a recorded fixture with no token and no network.

    Two field mappings here were wrong when read off the OpenAPI schema and are
    corrected against a live response:

    - Lateness is *computed*, not read. `realtimeAdvertisedLateness` is null
      until a train has actually run, so for the upcoming departures this
      screen cares about it is always absent - reading it would leave every
      service permanently on time and the timeline would never trigger.
    - Platform falls back `actual -> forecast -> planned`. `actual` never
      appeared in a live response; only the other two did, so stopping at
      `planned` would silently ignore a forecast platform change.
    """
    departures = []
    for service in payload.get("services") or []:
        temporal = (service.get("temporalData") or {}).get("departure") or {}
        scheduled = _parse_time(temporal.get("scheduleAdvertised"))
        if scheduled is None:
            continue  # a service that doesn't depart here (terminates, or passes)

        expected = (
            _parse_time(temporal.get("realtimeActual"))
            or _parse_time(temporal.get("realtimeForecast"))
            or scheduled
        )
        platform = (service.get("locationMetadata") or {}).get("platform") or {}
        departures.append(
            Departure(
                scheduled=scheduled,
                expected=expected,
                platform=platform.get("actual")
                or platform.get("forecast")
                or platform.get("planned")
                or "",
                cancelled=bool(temporal.get("isCancelled")),
                lateness=max(0, int((expected - scheduled).total_seconds() // 60)),
                destination=", ".join(
                    d["location"]["description"]
                    for d in service.get("destination") or []
                    if d.get("location", {}).get("description")
                ),
            )
        )
    return sorted(departures, key=lambda d: d.expected)


class RttClient:
    """Fetches departures from the next-generation Realtime Trains API.

    Mirrors SpotifyClient's failure handling deliberately: a 429 records a
    deadline and returns immediately rather than sleeping, so a long ban can
    never freeze the poll thread and take the display down with it.

    The configured credential is a *refresh* token; it buys a short-lived
    access token which is cached until shortly before it expires.
    """

    def __init__(self, refresh_token: str, http_request, timeout: float = 10) -> None:
        self.refresh_token = refresh_token
        # Injected so the client can be exercised without importing the app.
        self._http_request = http_request
        self.timeout = timeout
        self._access_token: str | None = None
        self._access_expires_at = 0.0
        self.rate_limited_until = 0.0

    def backoff_remaining(self) -> float:
        return max(0.0, self.rate_limited_until - time.time())

    def _note_rate_limit(self, response) -> None:
        retry_after = max(int(response.headers.get("Retry-After", "60") or 60), 1)
        self.rate_limited_until = time.time() + retry_after
        print(f"RTT rate-limited (429); backing off {retry_after}s", flush=True)

    def _valid_access_token(self) -> str:
        if self._access_token and time.time() < self._access_expires_at:
            return self._access_token

        response = self._http_request(
            "GET",
            ACCESS_TOKEN_URL,
            headers={"Authorization": f"Bearer {self.refresh_token}"},
            timeout=self.timeout,
        )
        if response.status == 429:
            self._note_rate_limit(response)
            raise RuntimeError("RTT rate-limited while fetching an access token")
        if response.status != 200:
            raise RuntimeError(f"RTT access-token request failed: HTTP {response.status}")

        body = response.json()
        token = body.get("token")
        if not token:
            raise RuntimeError("RTT access-token response contained no token")

        valid_until = _parse_time((body.get("validUntil") or "").replace("Z", "+00:00"))
        if valid_until is not None:
            # Renew a minute early rather than racing the expiry.
            self._access_expires_at = valid_until.timestamp() - 60
        else:
            self._access_expires_at = time.time() + 1800

        self._access_token = token
        return token

    def departures(self, station: str, minutes: int = 60) -> list[Departure] | None:
        """Departures at `station`, or None while rate-limited.

        None means "no fresh data", not "no trains" - the caller must keep
        showing what it had rather than claiming the platform is empty.
        """
        if self.backoff_remaining() > 0:
            return None

        response = self._http_request(
            "GET",
            LOCATION_URL,
            params={"code": f"gb-nr:{station}", "timeWindow": str(minutes)},
            headers={"Authorization": f"Bearer {self._valid_access_token()}"},
            timeout=self.timeout,
        )
        if response.status == 429:
            self._note_rate_limit(response)
            return None
        if response.status == 204:
            return []
        if response.status == 401:
            # The access token expired early; drop it so the next call re-mints.
            self._access_token = None
            raise RuntimeError("RTT rejected the access token (401)")
        if response.status != 200:
            raise RuntimeError(f"RTT location request failed: HTTP {response.status}")

        return parse_departures(response.json())
