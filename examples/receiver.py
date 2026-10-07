#!/usr/bin/env python3
"""A minimal leaderboard receiver: the smallest server the tracker will send to.

    python examples/receiver.py --port 8765 --db leaderboard.sqlite

Then point a tracker at it, either for one run:

    python analysis/submit.py --endpoint http://127.0.0.1:8765/submit.php

or for good, by setting "endpoint" in ~/.ptcgl-tracker/submit.json.

Standard library only, single-threaded, no TLS and no authentication beyond recording which
install key sent what - a starting point to read and build on, not something to put on the
internet as it is. Everything it implements is described in README.md under "Running your
own leaderboard".

It does what any receiver has to do:

  * accept POSTed JSON in payload_version 1 and answer {"ok": true} - anything else, an HTML
    error page included, makes the tracker retry later;
  * upsert on (install key, match_id), because every match arrives at least twice: once
    when it starts and again when it ends;
  * keep the match exactly as it arrived, so anything can be derived from it later.

And the optional verify endpoint behind `submit.py --verify`: a GET to the same URL with
"submit.php" replaced by "verify.php", answering non-null counts per field.
"""

import argparse
import datetime
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 8 * 1024 * 1024
KEY = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    install_key  TEXT NOT NULL,
    match_id     TEXT NOT NULL,
    display_name TEXT,
    received_at  TEXT NOT NULL,
    body         TEXT NOT NULL,          -- the match object exactly as it arrived
    PRIMARY KEY (install_key, match_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    install_key    TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    client_version TEXT,
    match_count    INTEGER
);
"""


class Receiver(BaseHTTPRequestHandler):
    db = None  # set in main()

    def reply(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def key(self):
        key = (self.headers.get("X-Tracker-Key") or "").strip().lower()
        return key if KEY.match(key) else None

    def do_POST(self):
        key = self.key()
        if not key:
            return self.reply(401, {"ok": False, "error": "missing or malformed X-Tracker-Key"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self.reply(413, {"ok": False, "error": "empty or oversized body"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return self.reply(400, {"ok": False, "error": "body is not JSON"})
        if not isinstance(payload, dict) or payload.get("payload_version") != 1:
            return self.reply(400, {"ok": False, "error": "unsupported payload_version"})

        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        who = (payload.get("account") or {}).get("display_name")
        matches = [m for m in payload.get("matches") or []
                   if isinstance(m, dict) and isinstance(m.get("match_id"), str)]
        with self.db:
            for m in matches:
                # Overwrite, never merge: the client always sends the whole match as it now
                # stands, so the newest copy is the truth - including a field it has cleared.
                self.db.execute(
                    "INSERT INTO matches (install_key, match_id, display_name, received_at, body)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(install_key, match_id) DO UPDATE SET"
                    " display_name = excluded.display_name,"
                    " received_at = excluded.received_at, body = excluded.body",
                    (key, m["match_id"], m.get("my_display_name") or who, now,
                     json.dumps(m, ensure_ascii=False)))
            self.db.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?)",
                (key, now, (payload.get("client") or {}).get("version"), len(matches)))
        self.reply(200, {"ok": True, "accepted": len(matches)})

    def do_GET(self):
        if not self.path.rstrip("/").endswith("verify.php"):
            return self.reply(200, {"ok": True, "service": "ptcgl-tracker",
                                    "accepts_payload_version": [1]})
        key = self.key()
        if not key:
            return self.reply(401, {"ok": False, "error": "missing or malformed X-Tracker-Key"})
        rows = [json.loads(b) for (b,) in self.db.execute(
            "SELECT body FROM matches WHERE install_key = ?", (key,))]
        if not rows:
            return self.reply(200, {"ok": True, "known": False, "matches": 0, "columns": {}})
        columns = {}
        for row in rows:
            for field, value in row.items():
                columns.setdefault(field, 0)
                if value is not None:
                    columns[field] += 1
        submissions = self.db.execute(
            "SELECT COUNT(*) FROM submissions WHERE install_key = ?", (key,)).fetchone()[0]
        self.reply(200, {
            "ok": True, "known": True, "matches": len(rows), "submissions": submissions,
            "span": {"profiles": len({r.get("my_display_name") for r in rows})},
            "columns": columns,
            "hidden_true": sum(1 for r in rows if r.get("hidden") in (1, True)),
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="leaderboard.sqlite")
    args = ap.parse_args()

    Receiver.db = sqlite3.connect(args.db, check_same_thread=False)
    Receiver.db.executescript(SCHEMA)
    print("listening on http://%s:%d - POST matches to any path" % (args.host, args.port))
    HTTPServer((args.host, args.port), Receiver).serve_forever()


if __name__ == "__main__":
    main()
