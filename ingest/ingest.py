#!/usr/bin/env python3
"""Fold the mod's event log into SQLite.

Reads the append-only JSONL the mod writes, pairs each match_start with its
match_end, and upserts one row per match. Safe to re-run at any time: matches are
keyed by match_id, so re-ingesting the same log changes nothing.
"""

import argparse
import io
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
import seasons     # noqa: E402  - path set immediately above

DEFAULT_DIR = os.path.expanduser("~/.ptcgl-tracker")

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id              TEXT PRIMARY KEY,
    started_at            TEXT,
    ended_at              TEXT,
    game_mode             TEXT,
    gameplay_type         TEXT,
    season_id             INTEGER,
    i_am_player1          INTEGER,
    my_display_name       TEXT,
    my_id_hash            TEXT,
    my_name_source        TEXT,
    my_elo                INTEGER,
    my_exp                INTEGER,
    my_elo_after          INTEGER,
    my_exp_after          INTEGER,
    exp_delta             INTEGER,
    consecutive_wins      INTEGER,
    my_deck               TEXT,
    my_decklist           TEXT,
    my_deck_wins          INTEGER,
    my_deck_source        TEXT,
    my_deck_iterations    INTEGER,
    my_deck_losses        INTEGER,
    opponent_display_name TEXT,
    opponent_id_hash      TEXT,
    opponent_elo          INTEGER,
    opponent_exp          INTEGER,
    opponent_deck         TEXT,
    opponent_decklist     TEXT,
    opponent_deck_source  TEXT,
    opponent_deck_iterations INTEGER,
    opponent_deck_wins    INTEGER,
    opponent_deck_losses  INTEGER,
    result                TEXT,
    end_reason            TEXT,
    abbreviated           INTEGER,
    duration_ms           INTEGER,
    turns                 INTEGER,
    went_first            INTEGER,
    prizes_taken          INTEGER,
    opponent_prizes_taken INTEGER,
    cards_played          INTEGER,
    knockouts             INTEGER,
    shape_source          TEXT,
    my_damage_dealt       INTEGER,
    my_coin_flips_won     INTEGER,
    my_mvp_card           TEXT,
    opponent_damage_dealt INTEGER,
    opponent_coin_flips_won INTEGER,
    opponent_mvp_card     TEXT,
    battle_log            TEXT,
    client_version        TEXT,
    capture_method        TEXT,
    schema_version        INTEGER,
    hidden                INTEGER,
    hidden_at             TEXT,
    hidden_reason         TEXT
);
"""

# Kept out of SCHEMA for a reason worth not rediscovering: migrate() reconciles columns by
# scanning SCHEMA line by line for "<name> <TYPE>", and it has no idea which CREATE TABLE a
# line belongs to. A second table in there would have every one of its columns bolted onto
# `matches` instead. Same shape of hazard as INDEXES below - one script, one table.
SEASON_SCHEMA = """
CREATE TABLE IF NOT EXISTS season_rank (
    recorded_at              TEXT PRIMARY KEY,
    match_id                 TEXT,
    elo                      INTEGER,
    elo_default              INTEGER,
    exp                      INTEGER,
    highest_exp              INTEGER,
    wins                     INTEGER,
    losses                   INTEGER,
    season_matches           INTEGER,
    consecutive_wins         INTEGER,
    previous_match_exp_delta INTEGER,
    season                   INTEGER
);
"""

SEASON_FIELDS = (
    "match_id", "elo", "elo_default", "exp", "highest_exp", "wins", "losses",
    "season_matches", "consecutive_wins", "previous_match_exp_delta", "season",
)


def whole(v):
    """A whole number, or None. See the note in attach_standings()."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def attach_standings(db):
    """Give each finished match the standing the server reported *after* it.

    The client only ever hands over a rating at match start, so a match's own result used
    to be visible only as the difference to the next match's opening figure - the newest
    game had no delta until another was played. The season-rank cache carries the value
    that comes back afterwards, so this closes that gap.

    The timestamp filter is load-bearing. The mod stamps a snapshot with the last match it
    recorded, and that id is set at match *start*, so a cache refresh that happens mid-game
    carries the current match's id and the *previous* standing. Only a snapshot taken after
    the match ended describes it.
    """
    updated = 0
    for mid, ended, start_elo in db.execute(
            "SELECT match_id, ended_at, my_elo FROM matches "
            "WHERE ended_at IS NOT NULL").fetchall():
        row = db.execute(
            """SELECT elo, exp, previous_match_exp_delta, consecutive_wins
                 FROM season_rank
                WHERE match_id = ? AND recorded_at > ?
             ORDER BY recorded_at LIMIT 1""", (mid, ended)).fetchone()
        if not row:
            continue
        # SQLite does not enforce column types, so a bad value from the mod would sit in an
        # INTEGER column and reach anything downstream as a string - competitiveElo, for one,
        # is a Dictionary<GameMode,uint> in the client. Coerce at the boundary; anything that
        # is not a whole number is stored as absent.
        row = tuple(whole(v) for v in row)

        # Two rules on the Elo, and neither applies to exp - nought ladder points is a
        # real standing.
        #
        # 0 is how the game says "no competitive rating", not a rating of zero.
        # competitiveElo is only populated in the top league; MatchHook.EloOrNull() records
        # a 0 as absent, and this path has to do the same, because whole(0) is 0.
        #
        # And 0 is not the only way the cache says nothing. It also reports
        # competitiveEloDefault - 1500 - for a player who has never been placed, which no
        # test on the value can tell from a real 1500. So the match's own STARTING figure
        # decides: `my_elo` comes from PlayerDetails through EloOrNull(), so it is absent
        # unless the game really did give this player a rating. A number coming out of a
        # match is only a rating if there was one going in.
        elo = row[0] if row[0] else None
        if not start_elo:
            elo = None
        row = (elo,) + row[1:]

        db.execute(
            """UPDATE matches
                  SET my_elo_after     = COALESCE(?, my_elo_after),
                      my_exp_after     = COALESCE(?, my_exp_after),
                      exp_delta        = COALESCE(?, exp_delta),
                      consecutive_wins = COALESCE(?, consecutive_wins)
                WHERE match_id = ?""", (row[0], row[1], row[2], row[3], mid))
        updated += 1
    return updated


# Kept out of SCHEMA and applied only after migrate(): an index naming a column that an
# older database has not got yet would fail the whole script.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_matches_started  ON matches(started_at);
CREATE INDEX IF NOT EXISTS idx_matches_opponent ON matches(opponent_id_hash);
CREATE INDEX IF NOT EXISTS idx_matches_ranked   ON matches(gameplay_type, started_at);
CREATE INDEX IF NOT EXISTS idx_matches_profile  ON matches(my_display_name, started_at);
CREATE INDEX IF NOT EXISTS idx_matches_season   ON matches(season_id, started_at);
CREATE INDEX IF NOT EXISTS idx_season_match     ON season_rank(match_id, recorded_at);
"""


def migrate(db):
    """Add any columns the schema has gained since this database was created.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so new columns never
    appear in a database made by an older build — and the next insert dies with
    "table matches has no column named X". Reconcile the two here instead.
    """
    have = {row[1] for row in db.execute("PRAGMA table_info(matches)")}
    if not have:
        return []

    added = []
    for line in SCHEMA.splitlines():
        parts = line.strip().rstrip(",").split()
        if len(parts) < 2:
            continue
        name, coltype = parts[0], parts[1].upper()
        if not name.isidentifier() or coltype not in ("TEXT", "INTEGER", "REAL"):
            continue
        if name not in have:
            db.execute(f"ALTER TABLE matches ADD COLUMN {name} {coltype}")
            added.append(name)

    if added:
        db.commit()
    return added

# Columns carried over from a match_start event, keyed by event field name.
START_FIELDS = [
    "game_mode", "gameplay_type", "i_am_player1",
    "my_display_name", "my_id_hash", "my_elo", "my_exp", "my_deck",
    "my_decklist", "my_deck_wins", "my_deck_losses",
    "opponent_display_name", "opponent_id_hash", "opponent_elo", "opponent_exp",
    # Where each deck came from and how worked-on it is. Import with 0 iterations is a raw
    # netdeck; a high count is somebody's own tuned list.
    "my_deck_source", "my_deck_iterations",
    "opponent_deck_source", "opponent_deck_iterations",
    "client_version", "capture_method", "schema_version",
    # Not captured by the mod; the client is not sent the opponent's deck. Projected when an
    # event log carries them anyway, so that a rebuilt database re-sends exactly what was
    # sent before - the server overwrites every column on a resend (see FIELDS in
    # analysis/submit.py).
    "opponent_deck", "opponent_decklist", "opponent_deck_wins", "opponent_deck_losses",
]


# What a battle log can be read for beyond its text. Derived, never captured - see
# match_shape_from_log().
SHAPE_FIELDS = ("turns", "went_first", "prizes_taken", "opponent_prizes_taken",
                "cards_played", "knockouts")


TURN_RE = re.compile(r"^(\S+)'s Turn\s*$")
# Both halves matter: the log says "decided to go first" OR "decided to go second", and
# only one of the two lines appears. Reading just the "first" form left who-went-first
# unknown on every match where the decider chose to go second.
FIRST_RE = re.compile(r"^(\S+) decided to go (first|second)\.")
# The log says "took a Prize card.", "took 2 Prize cards." or, for the last of them,
# "took all of their Prize cards." - which carries no number, so it is counted as whatever
# was left of the six.
PRIZE_RE = re.compile(r"^(\S+) took (\d+|a|all of their) Prize cards?")
PRIZE_CARDS = 6
KO_RE = re.compile(r"^(\S+)'s .* was Knocked Out!")
PLAYED_RE = re.compile(r"^(\S+) played ")


def match_shape_from_log(log_path, me, them):
    """How a match went, read out of the battle log the game already gave us.

    A hook was written for this first (TrackedStats(PlayerEntity, bool)) and deleted: it
    patched cleanly and never fired, because that constructor runs on the *server*. The log
    is strictly better anyway - it needs no mod at all, and it applies to every match ever
    recorded rather than only to those played after an install.

    Attribution is by name, which the log spells out on every line, so `me` and `them` have
    to be known. A knockout line names the player whose Pokemon *died*, so it scores for the
    other side - that inversion is the one thing here that is easy to get backwards.
    """
    if not log_path or not me:
        return {}
    try:
        with io.open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}

    out = {"turns": 0, "prizes_taken": 0, "opponent_prizes_taken": 0,
           "cards_played": 0, "knockouts": 0, "went_first": None}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if TURN_RE.match(line):
            out["turns"] += 1
            continue

        m = FIRST_RE.match(line)
        if m:
            decider_is_me = m.group(1) == me
            chose_first = m.group(2) == "first"
            out["went_first"] = 1 if decider_is_me == chose_first else 0
            continue

        m = PRIZE_RE.match(line)
        if m:
            if m.group(1) == me:
                side = "prizes_taken"
            elif them and m.group(1) == them:
                side = "opponent_prizes_taken"
            else:
                continue
            took = m.group(2)
            if took == "a":
                n = 1
            elif took == "all of their":
                n = max(PRIZE_CARDS - out[side], 0)
            else:
                n = int(took)
            out[side] += n
            continue

        m = KO_RE.match(line)
        if m:
            # The line names whose Pokemon was knocked out; the KO belongs to the other
            # player. Only ours is counted, and only when the loser is identifiable.
            if them and m.group(1) == them:
                out["knockouts"] += 1
            continue

        m = PLAYED_RE.match(line)
        if m and m.group(1) == me:
            out["cards_played"] += 1

    # A log that yielded no turns at all was not parsed, whatever else it produced.
    if not out["turns"]:
        return {}
    return out


# Stat fields carried over from a match_end event.
END_FIELDS = (
    "end_reason", "abbreviated", "duration_ms",
    "my_damage_dealt", "my_coin_flips_won", "my_mvp_card",
    "opponent_damage_dealt", "opponent_coin_flips_won", "opponent_mvp_card",
    "battle_log",
)


def end_stats(end):
    """The match_end fields, with the two players' stats the right way round.

    A match_end event with no "abbreviated" field picked each player's stats by who won as
    well as by which seat they held:

        mine = won ? modification.player1Stats : modification.player2Stats;

    player1Stats belongs to player 1 whoever won, so on a *lost* match those events store
    the two players' damage, coin flips and MVP card the wrong way round. Every event that
    carries "abbreviated" picks by seat alone, which makes the correction exact and
    idempotent - and it happens here, in the projection, because events.jsonl is
    append-only and is never rewritten.
    """
    out = {f: end.get(f) for f in END_FIELDS}
    if "abbreviated" not in end and end.get("result") == "loss":
        for a, b in (("my_damage_dealt", "opponent_damage_dealt"),
                     ("my_coin_flips_won", "opponent_coin_flips_won"),
                     ("my_mvp_card", "opponent_mvp_card")):
            out[a], out[b] = out[b], out[a]
    return out


# Both players are named on nearly every line of a battle log. Anchoring the name on \S+
# is load-bearing: a possessive line ("Ash's (me4_61) Metagross used Iron Head") would
# otherwise be read as a player called "Ash's".
_TURN_HEADER = re.compile(r"^(\S+)'s Turn\s*$")
_ACTION = re.compile(
    r"^(\S+) (?:chose |won the coin|lost the coin|decided to |drew |played |"
    r"took a mulligan|attached |discarded |shuffled |used |put )")

# The log writes the local player as "You" in a few summary lines ("You conceded."), and
# uses "-" to open a detail line. Neither is an account.
_NOT_A_PLAYER = {"You", "-", ""}


def find_log(path, events_path):
    """The battle log for a match, wherever it actually is on this machine.

    The event records an absolute path, which is right on the machine that wrote it and
    wrong everywhere else - so an exported bundle, unzipped on someone else's computer,
    would have all its logs and be unable to find any of them. Fall back to the folder
    beside the event log, which is exactly how a bundle is laid out.
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    beside = os.path.join(os.path.dirname(os.path.abspath(events_path)),
                          "battlelogs", os.path.basename(path))
    return beside if os.path.exists(beside) else None


def local_name_from_log(path, opponent_name):
    """Which account played this match, read out of its saved battle log.

    A match_start event without `my_display_name` has no account on it, and every such
    match would collapse into a single unnamed profile - which is exactly the mixing that
    profiles exist to stop.

    The battle log already holds the answer. It names both players throughout, and the
    opponent's name is on the match record, so the *other* name is the local player. This
    only ever answers when the log resolves to exactly one other name; two names or none
    means something unexpected, and an unattributed match is better than a wrong one.

    Derived in the projection rather than written back, for the usual reason: events.jsonl
    is append-only and is never rewritten.
    """
    if not path or not opponent_name or not os.path.exists(path):
        return None
    names = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                found = _TURN_HEADER.match(line.rstrip()) or _ACTION.match(line)
                if found:
                    names.add(found.group(1))
    except OSError:
        return None
    others = names - _NOT_A_PLAYER - {opponent_name}
    return others.pop() if len(others) == 1 else None


def tombstones(events):
    """{match_id: (hidden, at, reason)} after replaying every hide/restore in order.

    Deleting a match cannot mean rewriting the event log - it is append-only and is the
    source of truth, and a format whose history can be edited is one whose history cannot
    be trusted. So a deletion is itself an event, and the projection is what forgets: the
    row is marked hidden and nothing downstream reads it. Restoring appends the opposite
    event. Both are replayed from scratch on every ingest, so the last word wins and
    rebuilding the database from the log reproduces exactly the same state.
    """
    state = {}
    for ev in events:
        kind = ev.get("event_type")
        mid = ev.get("match_id")
        if not mid:
            continue
        if kind == "match_hidden":
            state[mid] = (1, ev.get("timestamp"), ev.get("reason"))
        elif kind == "match_restored":
            state[mid] = (0, None, None)
    return state


def read_events(path):
    """Yield parsed events, skipping malformed lines rather than aborting the run."""
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                print(f"  skipped malformed line {n}", file=sys.stderr)
    if bad:
        print(f"  {bad} malformed line(s) skipped", file=sys.stderr)


def ingest(events_path, db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.executescript(SEASON_SCHEMA)
    added = migrate(db)
    db.executescript(INDEXES)
    if added:
        print(f"  migrated: added {len(added)} column(s) -> {', '.join(added)}")

    events = list(read_events(events_path))
    starts, ends = {}, {}
    for ev in events:
        mid = ev.get("match_id")
        if not mid:
            continue
        kind = ev.get("event_type")
        if kind == "match_start":
            starts[mid] = ev
        elif kind == "match_end":
            ends[mid] = ev

    written = 0
    backfilled = 0
    for mid, start in starts.items():
        end = ends.get(mid, {})
        row = {
            "match_id":   mid,
            "started_at": start.get("timestamp"),
            "ended_at":   end.get("timestamp"),
            "result":     end.get("result"),
        }
        row.update(end_stats(end))
        for f in START_FIELDS:
            row[f] = start.get(f)

        # Which account played. The mod records it directly; an event without it is read
        # back out of the battle log (see local_name_from_log). Recorded with its source,
        # so nothing downstream presents an inference as if it were captured.
        if row.get("my_display_name"):
            row["my_name_source"] = "mod"
        else:
            inferred = local_name_from_log(find_log(row.get("battle_log"), events_path),
                                           row.get("opponent_display_name"))
            if inferred:
                row["my_display_name"] = inferred
                row["my_name_source"] = "battle log"
                backfilled += 1

        cols = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        # Later runs refresh a match that has since ended, but never blank a
        # column back out with a NULL from an incomplete record.
        updates = ", ".join(
            f"{c}=COALESCE(excluded.{c}, matches.{c})" for c in row if c != "match_id"
        )
        db.execute(
            f"INSERT INTO matches ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(match_id) DO UPDATE SET {updates}",
            row,
        )
        written += 1

    # Standing snapshots. Keyed on their own timestamp so re-ingesting the same log is
    # idempotent, exactly like match_id is for a match.
    snapshots = 0
    for ev in events:
        if ev.get("event_type") != "season_rank":
            continue
        row = {"recorded_at": ev.get("timestamp")}
        if not row["recorded_at"]:
            continue
        for f in SEASON_FIELDS:
            row[f] = ev.get(f)
        cols = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        db.execute(
            f"INSERT INTO season_rank ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(recorded_at) DO NOTHING", row)
        snapshots += 1

    # How each match went, read out of its battle log. Derived rather than captured, so it
    # lives here in the projection - same rule as local_name_from_log() and end_stats().
    # Recomputed every run: it costs a file read per match and means a fix to the parser
    # reaches every match already recorded, with nothing to migrate.
    stats_rows = 0
    for mid, log, me, them, have in db.execute(
            """SELECT match_id, battle_log, my_display_name, opponent_display_name, turns
                 FROM matches WHERE battle_log IS NOT NULL""").fetchall():
        shape = match_shape_from_log(find_log(log, events_path), me, them)
        if not shape:
            continue
        sets = ", ".join(f"{f}=?" for f in SHAPE_FIELDS) + ", shape_source='battle log'"
        db.execute(f"UPDATE matches SET {sets} WHERE match_id=?",
                   [shape.get(f) for f in SHAPE_FIELDS] + [mid])
        stats_rows += 1

    # Which ranked season each match belongs to, from the client's own cached season
    # calendar (analysis/seasons.py). Derived, so it lives here in the projection - same
    # rule as local_name_from_log() and match_shape_from_log() - and is recomputed every
    # run, which is what reaches matches recorded long before this existed.
    #
    # Every match, not only ranked ones: a casual game still happened during a season, and
    # excluding them would make "matches this season" disagree with itself depending on
    # which table you read.
    #
    # Written whatever the answer, including None, so that a run can un-say what an earlier
    # run said. And None is a real, expected outcome here rather than a failure: it is what
    # a machine with no config cache reports, and what *every* machine reports for the
    # few days after a season ends and before the client has fetched the next document. It
    # must never be read as "season 0".
    seasons.forget_cache()
    dated = 0
    for mid, started in db.execute(
            "SELECT match_id, started_at FROM matches").fetchall():
        sid = seasons.season_for(started)
        db.execute("UPDATE matches SET season_id=? WHERE match_id=?", (sid, mid))
        if sid is not None:
            dated += 1

    attached = attach_standings(db)

    # After the upserts, never as part of one: the upsert COALESCEs so it cannot blank a
    # column back out, and restoring a match has to be able to do exactly that.
    marks = tombstones(events)
    for mid, (hidden, at, reason) in marks.items():
        db.execute("UPDATE matches SET hidden=?, hidden_at=?, hidden_reason=? "
                   "WHERE match_id=?", (hidden, at, reason, mid))

    db.commit()

    total = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    unfinished = db.execute(
        "SELECT COUNT(*) FROM matches WHERE result IS NULL AND COALESCE(hidden,0)=0"
    ).fetchone()[0]
    hidden_rows = db.execute("SELECT COUNT(*) FROM matches WHERE hidden=1").fetchone()[0]
    season_span = db.execute(
        "SELECT COUNT(DISTINCT season_id) FROM matches WHERE season_id IS NOT NULL"
    ).fetchone()[0]
    db.close()

    print(f"Ingested {written} match record(s) from {events_path}")
    print(f"  {total} total in {db_path}" + (f", {unfinished} with no recorded result" if unfinished else ""))
    if backfilled:
        print(f"  {backfilled} match(es) attributed to an account from their battle log")
    if snapshots:
        print(f"  {snapshots} standing snapshot(s); {attached} match(es) carry a "
              f"post-match rating")
    if stats_rows:
        print(f"  {stats_rows} match(es) read for shape from their battle log "
              f"(turns, prizes, knockouts, who went first)")
    if dated:
        print(f"  {dated} match(es) placed in a ranked season, spanning {season_span}")
    # Said out loud rather than left to be noticed. Undated matches are expected right
    # after a season rolls over, and are the normal state on a machine with no config
    # cache - but they are also what a stale cache looks like, so the count is worth
    # seeing rather than inferring from a blank column.
    if total - dated:
        print(f"  {total - dated} match(es) could not be placed in a season "
              f"(no season config covers their date)")
    if hidden_rows:
        print(f"  {hidden_rows} hidden (deleted by hand; still in {os.path.basename(events_path)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=os.path.join(DEFAULT_DIR, "events.jsonl"))
    ap.add_argument("--db", default=os.path.join(DEFAULT_DIR, "matches.sqlite"))
    args = ap.parse_args()

    if not os.path.exists(args.events):
        print(f"No event log at {args.events}", file=sys.stderr)
        print("Play a match with the mod loaded, then run this again.", file=sys.stderr)
        return 1

    ingest(args.events, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
