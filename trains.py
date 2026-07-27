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
