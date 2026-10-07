#!/usr/bin/env python3
"""Which ranked season a match was played in.

The client caches every season it has ever been told about - `season_0000_<ver>.json`
onwards, one file per season, back to 2021 - as plain JSON in its own config-cache. That is
enough to place any match on the ladder calendar with no request, no account and no
network: this module is file reads and nothing else.

What matters here is the date handling, which has two traps that a naive reading walks
straight into.

**`seasonTitleDate` is not the start of the season.** It is the month the season is *named*
for, which is why season 53 carries `seasonTitleDate: 2026-07-01` alongside prize ids like
`ranked_jul2026_nestleague_reward`. Taken as a start date it produces a calendar that both
gaps and overlaps: season 52 ends 2026-07-16 while season 53 "starts" 2026-07-01, so a
fortnight in July belongs to two seasons at once, and every earlier pair leaves days
belonging to none.

**`endDate` alone gives a clean calendar.** It is monotonically increasing across every
season (checked, not assumed), so treating season N as running from season N-1's `endDate`
to its own leaves no gap and no overlap *by construction*. That is the whole design here.

A timestamp past the newest `endDate` we hold is **unknown, never the newest season** — the
client fetches only what the account has needed, so a season that has not started yet is
simply absent, and the next one will appear on its own. Same rule as everywhere else that
reads this folder: a missing document means "unknown", not "empty".
"""

import glob
import json
import os
import re
from datetime import datetime

# Where the game keeps the config it downloaded, one location per platform. Read-only.
CONFIG_DIRS = [
    os.path.expanduser("~/AppData/LocalLow/pokemon/Pokemon TCG Live/config-cache"),
    os.path.expanduser("~/Library/Application Support/com.pokemon.pokemontcgl/config-cache"),
]

_SEASON_FILE = re.compile(r"season_(\d+)_[\d.]+\.json$")

# Memoised: ingest reads this once per run over every match. forget_cache() exists because a
# long-lived process that warms the folder after this was first read needs a way to re-ask.
_CALENDAR = None


def _moment(text):
    """Parse the ISO timestamps in play here. None if it is not one.

    Deliberately not `datetime.fromisoformat`: it did not accept a trailing `Z` until
    Python 3.11 and this project's floor is 3.9. Handles both the config's
    `2026-09-15T17:00:00Z` (lowercase `z` in the older season documents) and the event
    log's `2026-08-23T00:07:12.920Z`, whose milliseconds are dropped - seasons turn over on
    a whole second and nothing here needs finer.
    """
    if not isinstance(text, str):
        return None
    stamp = text.strip().rstrip("zZ").split(".")[0]
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(stamp, "%Y-%m-%dT%H:%M")
        except ValueError:
            return None


def _payload(envelope):
    """The season object inside a config document, or None.

    These are JSON-inside-JSON: the outer file has `keys.<name>.contentString` holding the
    real payload as a string. Some documents have already been decoded to an object by the
    time we see them, so both shapes are accepted.
    """
    for value in (envelope.get("keys") or {}).values():
        if not isinstance(value, dict):
            continue
        inner = value.get("contentString")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                continue
        if isinstance(inner, dict) and "endDate" in inner:
            return inner
    return None


def calendar(dirs=None):
    """[(season_id, starts_after, ends_at)], oldest first. Empty when nothing can be read.

    `starts_after` is the previous season's end, so the ranges tile the timeline with no
    gaps and no overlaps.

    **The calendar is bounded at both ends, deliberately.** The oldest season has no
    previous end to derive a start from, so it falls back to its own `seasonTitleDate` -
    the one place that field is the only signal available. Leaving it open-ended instead
    would tag anything at all before it as season 0, which turns a corrupt or zero
    timestamp into a confident wrong answer. Unknown before the first season we hold is the
    same answer as unknown after the last one.
    """
    global _CALENDAR
    if _CALENDAR is not None and dirs is None:
        return _CALENDAR

    best, opened = {}, {}
    for folder in dirs or CONFIG_DIRS:
        for path in glob.glob(os.path.join(folder, "season_*.json")):
            match = _SEASON_FILE.search(os.path.basename(path))
            if not match:
                continue                       # season_current has no id of its own
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    payload = _payload(json.load(fh))
            except (OSError, ValueError):
                continue
            if not payload:
                continue
            ends = _moment(payload.get("endDate"))
            if ends is None:
                continue
            sid = int(match.group(1))
            # A season is cached at more than one schema version (_0.1 and _0.2). They
            # agree on the date; take either, deterministically.
            if sid not in best or ends > best[sid]:
                best[sid] = ends
                opened[sid] = _moment(payload.get("seasonTitleDate"))

    out, previous = [], None
    for sid in sorted(best):
        if previous is None:
            # Oldest season only: no earlier end exists, so fall back to its own title
            # date. Guarded, because a title date at or after the end would invert the
            # window and swallow everything before it - the exact failure this bound is
            # here to prevent.
            titled = opened.get(sid)
            previous = titled if titled and titled < best[sid] else best[sid]
        out.append((sid, previous, best[sid]))
        previous = best[sid]

    if dirs is None:
        _CALENDAR = out
    return out


def season_for(when, dirs=None):
    """The season id a timestamp falls in, or None when it cannot be known.

    None means exactly that and never a guess: no config cache on this machine, an
    unparseable timestamp, a match from before the oldest season we hold, or one played
    after the newest - which is the normal state for a few days every time a season rolls
    over, until the client fetches the next document.

    Boundary convention: a season owns the instant it ends, so `previous_end < t <= end`.
    Seasons turn over at 17:00:00Z and nothing is expected to land on it exactly.
    """
    moment = _moment(when)
    if moment is None:
        return None
    for sid, starts_after, ends_at in calendar(dirs):
        if moment <= ends_at:
            return sid if moment > starts_after else None
    return None


def forget_cache():
    """Drop the memoised calendar so the folder is read again."""
    global _CALENDAR
    _CALENDAR = None


def main():
    cal = calendar()
    if not cal:
        print("No season config found. Looked in:")
        for folder in CONFIG_DIRS:
            print("   ", folder)
        return
    print(f"{len(cal)} seasons, s{cal[0][0]} to s{cal[-1][0]}")
    print(f"{'id':>4}  {'from':<20} {'to':<20}")
    for sid, starts_after, ends_at in cal:
        since = starts_after.isoformat() if starts_after else "-"
        print(f"{sid:>4}  {since:<20} {ends_at.isoformat():<20}")


if __name__ == "__main__":
    main()
