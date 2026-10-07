# Changelog

### v1.0.0 - First open-source release

Records ranked Pokémon TCG Live matches from inside the client and sends them to a
leaderboard server - dialga.org by default, or your own.

- Runs in the background at login on Windows and macOS. No interface, nothing to open.
- Records both players, ratings and ladder points, your own decklist and its record, the
  result and end-of-match stats, the battle log, and your standing after each match.
- Sends each match at start and again at end; resubmission is idempotent.
- Puts itself back after a game update - at the update itself, or within a couple of minutes
  of the game closing - and opens an alert page only when it cannot.
- Installing replaces any other build of this tracker already in the game, and the watcher
  any other build registered, so two never run side by side.
- `analysis/submit.py --off` stops sending and keeps recording.
