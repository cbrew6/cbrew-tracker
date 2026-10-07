#!/usr/bin/env python3
"""Send recorded matches to a leaderboard server (dialga.org by default).

Reads the SQLite projection - **never `events.jsonl`** - builds a payload, and posts it.
Everything downstream of the event log already reads SQLite, and a submitter that parsed the
log itself would be a second, drifting interpretation of the same facts.

Five properties, each of which is load-bearing:

**Failing to send is never failing to record.** Nothing here can raise into its caller. The
ladder history on disk is the product; the leaderboard is a view of it. A dead server, no
network, or a bug in this file must cost nothing but a later retry.

**Resubmission is free.** `match_id` is stable and unique, and the server upserts on it, so
sending the same match twice updates one row. That means this never has to be *sure* it
succeeded - when in doubt, send again.

**What was sent is remembered outside the projection.** `submitted.json` sits next to the
database rather than inside it, because `matches.sqlite` is explicitly disposable: delete it
and it rebuilds. If submission state lived there, a rebuild would re-upload every match and
every battle log. Storing a *hash* of what was sent, rather than a flag, also means an edit
- a corrected value, a deleted match - is noticed and re-sent with no special handling.

**Batching is by bytes, not by count.** A match with a battle log is ~10KB and one without
is a few hundred, so a fixed count would produce wildly uneven requests and eventually one
that a server-side limit refuses.

**The response may not be JSON.** A security layer in front of a web server can answer
some requests with an HTML error page. Anything unparseable is treated as a retryable
failure rather than crashing.

Only the salted id hashes are withheld. They are per-install and therefore useless for
matching anyone across installs.

The wire format is documented in README.md, under "Running your own leaderboard".
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DATA_DIR = os.path.expanduser("~/.ptcgl-tracker")
SETTINGS = os.path.join(DATA_DIR, "submit.json")
STATE = os.path.join(DATA_DIR, "submitted.json")

DEFAULT_ENDPOINT = "https://dialga.org/tracker/submit.php"
PAYLOAD_VERSION = 1

# Well under PHP's usual 8M post_max_size and most servers' request limits, with room for
# the JSON envelope on top of the match data itself.
MAX_BATCH_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT = 30.0
ATTEMPTS = 3

# The columns copied straight across from the database into each match object.
#
# Two omissions, both deliberate. `my_id_hash` and `opponent_id_hash` are not sent at all -
# they are salted per-install and so cannot match anyone up across installs. `battle_log` IS
# sent but is not in this list, because the database column holds a *path* and the payload
# carries the *text*; collect() reads the file instead of copying the value. So a match
# object carries FIELDS + {"match_id", "battle_log"}, each only when it has a value.
#
# **A column that holds data cannot leave this list.** The reference server's upsert
# overwrites every column with what arrives, so a column dropped from here is blanked on the
# server the next time a match is re-sent - and any change to this list re-sends every match,
# because it changes every digest.
FIELDS = (
    "started_at", "ended_at", "season_id", "game_mode", "gameplay_type", "i_am_player1",
    "my_display_name", "my_name_source", "my_elo", "my_elo_after", "my_exp", "my_exp_after",
    "exp_delta", "consecutive_wins", "my_deck", "my_decklist",
    "my_deck_wins", "my_deck_losses", "my_deck_source", "my_deck_iterations",
    "opponent_display_name", "opponent_elo", "opponent_exp",
    "opponent_deck_source", "opponent_deck_iterations",
    "result", "end_reason", "abbreviated", "duration_ms",
    "turns", "went_first", "prizes_taken", "opponent_prizes_taken", "cards_played",
    "knockouts", "shape_source",
    "my_damage_dealt", "my_coin_flips_won", "my_mvp_card",
    "opponent_damage_dealt", "opponent_coin_flips_won", "opponent_mvp_card",
    "client_version", "capture_method", "schema_version",
    "hidden", "hidden_at", "hidden_reason",
    # Not captured by the mod (the client is not sent the opponent's deck). Listed so that a
    # match whose event log carries them anyway is re-sent with them, never without.
    "opponent_deck", "opponent_decklist", "opponent_deck_wins", "opponent_deck_losses",
)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    """Write via a temp file and replace, so an interrupted write cannot truncate state."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def settings():
    """Submission settings, creating them (and the install key) on first use.

    The key is 32 random bytes as hex. It identifies an *install*, not a player - player
    identity is the PTCGL display name carried on every match, which is why two profiles on
    one machine, and one person on two machines, both come out right on the leaderboard.
    What the key buys is authenticity (nobody can post results as somebody else), the
    ability to block a bad install, and the corroboration count on each leaderboard row.

    Generated silently on first run. There is nothing to set up and nothing to answer.
    """
    conf = _read_json(SETTINGS, {})
    changed = False
    if not isinstance(conf.get("key"), str) or len(conf.get("key", "")) != 64:
        conf["key"] = hashlib.sha256(os.urandom(32)).hexdigest()
        changed = True
    if not conf.get("endpoint"):
        conf["endpoint"] = DEFAULT_ENDPOINT
        changed = True
    if "enabled" not in conf:
        conf["enabled"] = True
        changed = True
    if changed:
        _write_json(SETTINGS, conf)
    return conf


def find_log(path, db_path):
    """The battle log for a match, wherever it actually is on this machine.

    The recorded path is absolute and belongs to the machine that wrote it, so an imported
    bundle has every log and can find none of them. Same fallback as ingest.find_log().
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    beside = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                          "battlelogs", os.path.basename(path))
    return beside if os.path.exists(beside) else None


def read_log(path, db_path):
    found = find_log(path, db_path)
    if not found:
        return None
    try:
        with open(found, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def digest(match):
    """A stable hash of one match as it will be sent.

    Hashing the content rather than recording a flag is what makes an edit re-send itself:
    correct an archetype or delete a match and the hash moves, so the next run notices.
    """
    blob = json.dumps(match, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def collect(db_path):
    """Every match, as payload dicts. Empty list rather than an exception on any problem."""
    if not os.path.exists(db_path):
        return []
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        have = {r[1] for r in db.execute("PRAGMA table_info(matches)")}
        cols = [c for c in ("match_id", "battle_log") + FIELDS if c in have]
        rows = db.execute(f"SELECT {', '.join(cols)} FROM matches").fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass

    out = []
    for row in rows:
        match = {"match_id": row["match_id"]}
        for field in FIELDS:
            if field in have:
                value = row[field]
                if value is not None:
                    match[field] = value
        # The column holds a path; the payload carries the text.
        if "battle_log" in have:
            text = read_log(row["battle_log"], db_path)
            if text:
                match["battle_log"] = text
        out.append(match)
    return out


def batches(matches, max_bytes=MAX_BATCH_BYTES):
    """Split into requests small enough to be accepted, sized by actual bytes."""
    batch, size = [], 0
    for match in matches:
        weight = len(json.dumps(match, ensure_ascii=False).encode("utf-8")) + 2
        if batch and size + weight > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(match)
        size += weight
    if batch:
        yield batch


def _brief(text):
    """One short line fit for a log.

    The security layer in front of PHP answers with full HTML error pages, and pasting a
    stylesheet into watcher.log helps nobody. Reduce markup to its title, and collapse
    whitespace either way so a failure is always exactly one line.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    if "<html" in flat.lower() or "<!doctype" in flat.lower():
        title = re.search(r"<title>\s*(.*?)\s*</title>", flat, re.I | re.S)
        return f"HTML error page: {title.group(1)}" if title else "HTML error page"
    return flat[:180]


def post(endpoint, key, payload, timeout=HTTP_TIMEOUT):
    """POST one batch. Returns (ok, detail). Never raises."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Tracker-Key": key,
            "User-Agent": "ptcgl-tracker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = _brief(exc.read().decode("utf-8", "replace")) if exc.fp else ""
        return False, f"HTTP {exc.code} {body}".strip()
    except Exception as exc:                       # offline, DNS, TLS, timeout, anything
        return False, _brief(str(exc))

    # A security layer sits in front of PHP and answers some requests with an HTML error
    # page, so a 200 does not guarantee JSON.
    try:
        answer = json.loads(raw)
    except ValueError:
        return False, f"non-JSON reply: {_brief(raw)}"
    if not isinstance(answer, dict) or not answer.get("ok"):
        return False, _brief(str(answer.get("error") if isinstance(answer, dict) else answer))
    return True, answer


def send(db_path=None, force=False, verbose=True, endpoint=None):
    """Send anything new or changed. Returns (sent, skipped, failed). Never raises."""
    db_path = db_path or os.path.join(DATA_DIR, "matches.sqlite")
    try:
        conf = settings()
    except Exception as exc:
        if verbose:
            print(f"  submit: could not read settings ({exc})")
        return 0, 0, 0

    if not conf.get("enabled") and not force:
        return 0, 0, 0

    target = endpoint or conf["endpoint"]
    matches = collect(db_path)
    if not matches:
        return 0, 0, 0

    already = {} if force else _read_json(STATE, {})
    pending, unchanged = [], 0
    for match in matches:
        if already.get(match["match_id"]) == digest(match):
            unchanged += 1
        else:
            pending.append(match)

    if not pending:
        return 0, unchanged, 0

    whoami = None
    for match in reversed(matches):
        if match.get("my_display_name"):
            whoami = match["my_display_name"]
            break

    sent = failed = 0
    for batch in batches(pending):
        payload = {
            "payload_version": PAYLOAD_VERSION,
            "client": {"version": _version()},
            "account": {"display_name": whoami},
            "matches": batch,
        }
        ok, detail = _with_retry(target, conf["key"], payload, verbose)
        if ok:
            for match in batch:
                already[match["match_id"]] = digest(match)
            sent += len(batch)
        else:
            failed += len(batch)
            if verbose:
                print(f"  submit: {len(batch)} match(es) not sent - {detail}")
            break            # a failing server will fail the next batch too; stop and retry later

    if sent:
        try:
            _write_json(STATE, already)
        except OSError as exc:
            # The matches did arrive; only the record of it failed. Harmless - they will be
            # re-sent next run and the server will upsert them onto the same rows.
            if verbose:
                print(f"  submit: sent {sent} but could not record it ({exc})")
    return sent, unchanged, failed


def _with_retry(endpoint, key, payload, verbose):
    delay = 1.0
    detail = "not attempted"
    for attempt in range(1, ATTEMPTS + 1):
        ok, detail = post(endpoint, key, payload)
        if ok:
            return True, detail
        if attempt < ATTEMPTS:
            time.sleep(delay)
            delay *= 3
    return False, detail


def _version():
    """What this client calls itself, for the server's records: the VERSION file one level
    above this one, or "unknown" without it."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        stamp = os.path.join(os.path.dirname(here), "VERSION")
        with open(stamp, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def verify(db_path, endpoint=None):
    """Compare what the server holds against the local database, column by column.

    Row counts alone would pass even if every optional field were being dropped in transit,
    so this compares the non-null count of each column at both ends. `COUNT(col)` means the
    same thing in SQLite and MySQL, which is what makes the two sides directly comparable.
    """
    conf = settings()
    target = (endpoint or conf["endpoint"]).replace("submit.php", "verify.php")

    request = urllib.request.Request(
        target, headers={"X-Tracker-Key": conf["key"], "User-Agent": "ptcgl-tracker"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            remote = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"Could not reach the server: {_brief(str(exc))}")
        return 1
    if not remote.get("ok"):
        print(f"Server said: {remote.get('error')}")
        return 1
    if not remote.get("known"):
        print("The server has never seen this install key. Nothing has been sent yet.")
        return 1

    db = sqlite3.connect(db_path)
    local_cols = [r[1] for r in db.execute("PRAGMA table_info(matches)")]
    local_rows = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    local = {c: db.execute(f"SELECT COUNT({c}) FROM matches").fetchone()[0] for c in local_cols}
    db_hidden = db.execute(
        "SELECT COUNT(*) FROM matches WHERE COALESCE(hidden, 0) = 1").fetchone()[0]
    db.close()

    server = remote.get("columns", {})
    # Only what is sent can be compared. Anything else in the local table - the id hashes,
    # or a column an older database still carries - is listed and skipped.
    sent = set(FIELDS) | {"match_id", "battle_log"}

    print(f"matches      local {local_rows}   server {remote['matches']}"
          f"   {'OK' if local_rows == remote['matches'] else '*** MISMATCH ***'}")
    print(f"submissions  {remote.get('submissions')}   profiles "
          f"{(remote.get('span') or {}).get('profiles')}")
    print()

    problems = 0
    for col in local_cols:
        if col not in sent:
            print(f"  {col:<24} {local[col]:>5} local   (not sent)")
            continue
        # `hidden` is nullable here and NOT NULL DEFAULT 0 on the server, so its non-null
        # counts are not comparable - the server correctly has a value in every row.
        # Compare how many are actually hidden, which is the fact that matters.
        if col == "hidden":
            mine = db_hidden
            theirs = remote.get("hidden_true")
            if theirs is None:
                print(f"  {col:<24} {mine:>5} local   (server too old to report)")
            elif mine != theirs:
                print(f"  {col:<24} {mine:>5} local  {theirs:>5} server  *** DIFFERS ***")
                problems += 1
            continue
        if col not in server:
            if local[col]:
                print(f"  {col:<24} {local[col]:>5} local   *** NOT ON SERVER ***")
                problems += 1
            continue
        if local[col] != server[col]:
            print(f"  {col:<24} {local[col]:>5} local  {server[col]:>5} server  *** DIFFERS ***")
            problems += 1

    if problems:
        print(f"\n{problems} column(s) differ. Run with --force to resend.")
        return 1
    print("Every column matches. Everything local is on the server.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "matches.sqlite"))
    ap.add_argument("--endpoint", help="override the configured endpoint")
    ap.add_argument("--force", action="store_true",
                    help="resend everything, ignoring what is recorded as already sent")
    ap.add_argument("--status", action="store_true", help="show settings and stop")
    ap.add_argument("--verify", action="store_true",
                    help="compare the server against the local database, column by column")
    ap.add_argument("--off", action="store_true", help="stop submitting from this install")
    ap.add_argument("--on", action="store_true", help="resume submitting")
    args = ap.parse_args()

    conf = settings()
    if args.off or args.on:
        conf["enabled"] = bool(args.on)
        _write_json(SETTINGS, conf)
        print(f"Submitting is now {'on' if conf['enabled'] else 'off'}.")
        return

    if args.verify:
        sys.exit(verify(args.db, args.endpoint))

    if args.status:
        state = _read_json(STATE, {})
        print(f"endpoint : {conf['endpoint']}")
        print(f"enabled  : {conf['enabled']}")
        print(f"key      : {conf['key'][:8]}... ({SETTINGS})")
        print(f"sent     : {len(state)} match(es) recorded as submitted")
        return

    sent, unchanged, failed = send(args.db, force=args.force, endpoint=args.endpoint)
    print(f"Sent {sent}, unchanged {unchanged}, failed {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
