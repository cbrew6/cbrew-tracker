# cbrew Tracker

Records your Pokémon TCG Live matches from inside the game client and sends them to a
leaderboard - the shared ladder at **dialga.org** by default, or one you run yourself.

It runs in the background on Windows and macOS and has no interface. Everything it records
is also kept on your own machine in plain, documented formats, so you can build your own
tools on top of it.

> **This modifies the Pokémon TCG Live client.** Two files are added to the game folder and
> two of the game's own manifests get one entry each. That is against the game's terms of
> service, and the risk of running it is yours to accept. This project is not affiliated
> with The Pokémon Company International or Nintendo.

- [For players](#for-players): [install](#install), [what it sends](#what-it-sends),
  [your data](#where-your-data-is-kept), [turning it off](#turning-it-off),
  [troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Data formats](#data-formats): [events.jsonl](#eventsjsonl), [battle logs](#battle-logs),
  [matches.sqlite](#matchessqlite), [other files](#other-files)
- [Running your own leaderboard](#running-your-own-leaderboard)
- [What the game tells the client](#what-the-game-tells-the-client)
- [Building from source](#building-from-source), [tests](#tests),
  [repository layout](#repository-layout), [license](#license)

---

## For players

### Install

**Windows** - download this repository (Code → Download ZIP), unzip it somewhere it can
stay, double-click **`Install-Windows.cmd`**, then launch the game and play. If the machine
has no Python 3.9 or newer, the installer downloads the official embeddable build from
python.org into `~/.ptcgl-tracker/python`, checks its hash, and uses that. Nothing else on
the PC changes.

**macOS** - unzip it, double-click **`Install-macOS.command`**, then launch the game and
play. If macOS refuses to open it because it "is from an unidentified developer",
right-click the file and choose *Open*; that answer is remembered. Python is not downloaded
on a Mac: every Mac has one, and if it is missing the installer says to run
`xcode-select --install`.

The game must be closed while installing. Nothing opens afterwards - that is normal. If the
tracker ever stops recording, a page opens in your browser to say so and what to do.

Installing again is always safe: it repairs whatever is wrong and replaces any other build
of this tracker already in the game.

### What it sends

Every match, **when it starts and again when it ends**:

- Both players' display names, ladder points and, in the top league, Elo
- The game mode and type (Ranked, Casual, ...), and the result
- **Your own decklist**, and its win/loss record as the game reports it
- How the opponent's deck was made (imported, copied, built) and how often it was edited
- End-of-match stats: duration, damage dealt, coin flips won and MVP card for each player
- **The battle log** - the turn-by-turn record the game itself can export
- Your rating and ladder points after the match
- A random ID generated at install, identifying this installation

Because the first send happens at match start, a match is on the server while it is still
being played. The opponent's decklist is never sent: the game does not give it to the
client. All that is known about their deck is what they play, which arrives in the battle
log once the match is over.

Nothing is sent to Pokémon or to any Pokémon service. The tracker never displays match data
to you, reads nothing outside the game's own match data and season calendar, and does not
touch your account credentials.

### Where your data is kept

Under `~/.ptcgl-tracker/` (`C:\Users\<you>\.ptcgl-tracker` on Windows):

| | |
|---|---|
| `events.jsonl` | every match recorded, append-only - the source of truth |
| `matches.sqlite` | a working copy built from the event log; delete it and it rebuilds |
| `battlelogs/` | one text file per match |
| `submit.json` | your installation ID, the server address, and whether sending is on |
| `submitted.json` | a fingerprint of each match as it was last sent |
| `tracker.log` | what the in-game part did at the last game launch |
| `watcher.log` | what the background process has been doing |
| `install.log` | what each install run did, in detail |
| `install.json` | where the tracker was installed |
| `mod/` | a copy of the tracker's files, used to put it back after a game update |
| `relauncher.txt` / `cbrew Tracker.app` | what a game update hands over to (Windows / macOS) |
| `relauncher.log` | what the relauncher did at the last game update |
| `python/` | Windows only, and only if the installer had to fetch Python |

Deleting `~/.ptcgl-tracker/` removes everything the tracker ever kept on your machine.

### Turning it off

```
python analysis/submit.py --off       stop sending; keep recording locally (--on resumes)
scripts/uninstall-windows.ps1         remove it from the game and stop the watcher (Windows)
scripts/uninstall-macos.sh            remove it from the game and stop the watcher (macOS)
```

Uninstalling leaves your match data alone.

### Troubleshooting

```
scripts/check.ps1   (Windows)   or   scripts/check.sh   (macOS)
```

prints whether the tracker is installed and recording; it changes nothing and is safe while
the game is open. Beyond that:

- **`tracker.log`** is rewritten at every game launch. A working install lists one
  `patched ...` line per hook - `OnMatchCreation`, `StartVersusScene`, `LoadEndBattleScreen`
  and `UpdateInfoCache`, plus `Process.Start` on Windows and macOS.
- **`watcher.log`** says what was ingested, sent, repaired, or could not be.
- **`install.log`** has every step of every install. Run the installer with `-Detail`
  (Windows) or `--detail` (macOS) to see it on screen too.

**Game updates.** An update replaces the whole game, which takes the tracker out. It puts
itself back: when the game hands an update to Pokémon's updater, the tracker changes what
the updater opens afterwards to its own relauncher, which re-installs the tracker and then
opens the game - so the updated game's first launch already records. Pokémon's updater
itself is not modified. If that hand-off cannot happen, the watcher puts the tracker back
within a couple of minutes of the game closing. Either way, run the installer again only if
the alert page tells you to.

**macOS code signing.** A macOS app is a *sealed* bundle - its signature covers every file
inside it - so after adding two files the installer re-signs the game with an ad-hoc
signature, carrying the game's own entitlements across unchanged (one of them is what lets
Mono run on Apple Silicon). The health check verifies that signature every two minutes and
repairs it if needed; the uninstaller re-signs on the way out too.

---

## How it works

```
 Pokémon TCG Live (Unity / Mono)
 └─ CbrewTracker.dll  ── Harmony postfixes ──▶  ~/.ptcgl-tracker/events.jsonl
                                                ~/.ptcgl-tracker/battlelogs/*.txt
 watcher (scripts/watch.py, runs at login)
 ├─ ingest/ingest.py    events.jsonl ──▶ matches.sqlite   (+ derived fields)
 ├─ analysis/submit.py  matches.sqlite ──▶ HTTPS POST ──▶ leaderboard
 └─ every 2 min: scripts/healthcheck.py ──▶ scripts/modinstall.py repair ──▶ alert page
```

### Loading into the game

The tracker is a .NET Framework 4.7.2 assembly that Unity loads as if it shipped with the
game. The installer (`scripts/modinstall.py`):

1. copies `CbrewTracker.dll` and `0Harmony.dll` into the game's `Managed` folder
   (`<game>\Pokemon TCG Live_Data\Managed` on Windows,
   `<App>.app/Contents/Resources/Data/Managed` on macOS);
2. appends both to `ScriptingAssemblies.json` (`names[]`, with type `16` in the parallel
   `types[]`);
3. appends one entry to `RuntimeInitializeOnLoads.json`:
   `{"assemblyName": "CbrewTracker", "nameSpace": "CbrewTracker", "className": "Bootstrap",
   "methodName": "Init", "loadTypes": 1, "isUnityClass": false}`, so Unity calls
   `Bootstrap.Init()` before the first scene loads;
4. on macOS, re-signs the bundle ad-hoc with the original entitlements.

Removal deletes exactly those entries. No BepInEx and no Doorstop: Unity 6's player resolves
Mono's symbols through `dlsym` rather than importing them, so Doorstop's symbol rebinding has
nothing to hook. Nothing replaces or proxies a game library.

### The hooks

All in `mod/CbrewTracker/Hooks/`. Every match hook is a read-only Harmony **postfix**: it
observes what the client has already computed and never changes it, and the tracker never
calls out to Pokémon's servers.

| Target | Writes | Notes |
|---|---|---|
| `NetworkMatchController.OnMatchCreation()` | `match_start` | Every match passes through it, ranked matchmaking included. Reads `currentMatch.players` (`PlayerDetails[]`) and `isPlayer1`. |
| `NetworkMatchController.StartVersusScene(PlayerDetails[])` | `match_start` | Direct matches only; whichever hook fires first records the match. |
| `EndGameHandler.LoadEndBattleScreen(bool useAbbreviatedResults, string gameEndReasonLocID, EndGameModification)` | `match_end`, battle log | The fork both results screens pass through, so abbreviated ends (concede timeouts, offline) are not missed. Reads the private `_battleLogExportData` and `_matchTimeStopWatch`. |
| `RainierClientSDK.source.SeasonRank.SeasonRankQuery.UpdateInfoCache()` | `season_rank` | Your standing as the server reports it after a match. Optional: if a game update moves it, only post-match ratings are lost. |
| `System.Diagnostics.Process.Start(string, string)` | `handoff.json` | The only hook that changes anything, and only the game's own call to start its updater (macOS and Windows). See below. |

**The update hand-off.** The game starts its updater with
`--installPath "<game>" ... --launchAppAt "<game>"` and quits; the updater copies the new
version in - overwriting both manifests - and opens `--launchAppAt` when Play is clicked. A
prefix on `Process.Start` rewrites `--launchAppAt` to the tracker's relauncher (on Windows,
`pythonw.exe` plus an added `--launchAppWithArgs "scripts\relaunch.py"`), which re-installs
the tracker from `~/.ptcgl-tracker/mod/` and then starts the game. It only does this when
the relauncher exists, and any failure leaves the updater's arguments exactly as they were.
The string handling lives in `Hooks/UpdaterHandoff.cs` and is unit-tested against Windows'
own command-line parser.

### The watcher

`scripts/watch.py` runs at login (a Scheduled Task named `CbrewTrackerWatcher` on Windows, a
LaunchAgent `com.cbrew.tracker.watcher` on macOS). It tails `events.jsonl`; on every
`match_start` or `match_end` it runs `ingest/ingest.py` and then `analysis/submit.py`, each
in its own process so a failure can never stop recording. Every two minutes it runs
`scripts/healthcheck.py`; if a game update has removed the tracker it repairs it from the
stash once the game is closed, and if it cannot, it writes `tracker-alert.html` and opens it.

```
python scripts/watch.py [--interval 3] [--log FILE] [--quiet] [--no-submit]
python scripts/healthcheck.py [--json]
```

---

## Data formats

### events.jsonl

One JSON object per line, appended by the mod and never rewritten. Fields with no value are
omitted rather than written as `null`. Timestamps are UTC ISO 8601 with milliseconds
(`2026-10-06T23:41:48.100Z`). `schema_version` is `1` and `capture_method` is `"mod"` on
every event; `client_version` is the game's version.

**`match_start`**

| Field | Type | Meaning |
|---|---|---|
| `match_id` | string | The game's match id, `<hex>:<uuid>`. Stable; the key for everything. |
| `game_mode` | string | e.g. `Standard`. |
| `gameplay_type` | string | `Ranked`, `Casual`, `Friend`, `Offline`, ... |
| `i_am_player1` | bool | Your seat. |
| `my_display_name`, `opponent_display_name` | string | As shown in game. |
| `my_id_hash`, `opponent_id_hash` | string | Salted per install (`~/.ptcgl-tracker/salt`), so useless across installs. Never sent. |
| `my_elo`, `opponent_elo` | int | Only in the top league; absent elsewhere. |
| `my_exp`, `opponent_exp` | int | Ladder points (see [below](#what-the-game-tells-the-client)). |
| `my_deck` | string | Your deck's name. |
| `my_decklist` | string | A JSON array of `[card_id, name, count, section]`, e.g. `[["sv1_196","Ultra Ball",4,"Item"], ...]`. `section` is `Pokemon`, `Item`, `Supporter`, `Stadium`, `Tool`, `Energy`, or `Trainer` when the subtype is unknown; `name`/`section` are `null` if the card database could not resolve them. |
| `my_deck_wins`, `my_deck_losses` | int | The deck's lifetime record, as the server tracks it. |
| `my_deck_source`, `opponent_deck_source` | string | How the deck was made: `Import`, `ImportRarest`, `ImportUnowned`, `Copy`, `New`. |
| `my_deck_iterations`, `opponent_deck_iterations` | int | How many times the deck has been edited. |

**`match_end`**

| Field | Type | Meaning |
|---|---|---|
| `match_id` | string | Pairs with the `match_start`. |
| `result` | string | `win` or `loss`. |
| `end_reason` | string | The game's localisation id, e.g. `match_results_victory_reason_opponent_concede`. |
| `abbreviated` | bool | True when the game skipped the full results screen; those ends carry no per-player stats. |
| `duration_ms` | int | Match length as the game times it. |
| `my_damage_dealt`, `opponent_damage_dealt` | int | |
| `my_coin_flips_won`, `opponent_coin_flips_won` | int | |
| `my_mvp_card`, `opponent_mvp_card` | string | Card id. |
| `battle_log` | string | Absolute path of the saved battle log. |

**`season_rank`** - written when the client refreshes your standing, deduplicated.

| Field | Type | Meaning |
|---|---|---|
| `match_id` | string | The last match recorded this launch, or absent. A snapshot only describes that match if its `timestamp` is after the match's `match_end`. |
| `elo`, `elo_default` | int | Rating for Standard. `elo_default` (1500) is what an unplaced player reports. |
| `exp`, `highest_exp` | int | Ladder points now, and the season's best. |
| `wins`, `losses`, `season_matches` | int | This season. |
| `consecutive_wins` | int | The streak the game is counting. |
| `previous_match_exp_delta` | int | Points the last match was worth. |
| `season` | int | Season number. |

**Hiding a match.** Append `{"event_type": "match_hidden", "match_id": "...", "timestamp":
"...", "reason": "..."}`; append `match_restored` to undo it. Ingest replays these in order
and sets `hidden` on the row; the event log itself is never edited.

### Battle logs

`battlelogs/<match_id with ':' replaced by '_'>.txt`, UTF-8, the same text the game's own
Export button produces:

```
Setup
Ash chose tails for the opening coin flip.
Misty won the coin toss.
Misty decided to go first.
Ash drew 7 cards for the opening hand.
- 7 drawn cards.
   - (mee_6) Basic Fighting Energy, (me1_75) Solrock, (sv1_196) Ultra Ball, ...
Misty drew 7 cards for the opening hand.
- 7 drawn cards.

Misty's Turn
Misty drew a card.
Misty played (me2-5_46) Snorunt to the Bench.
...
Ash's (me1_75) Solrock was Knocked Out!
Misty took a Prize card.
```

Phases are separated by a blank line and open with `<name>'s Turn` (or `Setup`). Every
action names its player first; detail lines start with `- `, and their contents with
`   - `. Cards appear as `(<card_id>) <name>`. Your own hidden draws are listed; the
opponent's are only counted. A knockout line names the player whose Pokémon was knocked
out. Prize lines read `took a Prize card`, `took N Prize cards`, or `took all of their
Prize cards`. Player names contain no spaces, which is what makes `^(\S+)` a reliable way
to attribute a line. `match_shape_from_log()` in `ingest/ingest.py` is a worked parser.

### matches.sqlite

Built by `ingest/ingest.py`, safe to delete (it rebuilds from `events.jsonl`) and safe to
re-run. Table **`matches`** has one row per `match_id`: every `match_start` and `match_end`
field above, plus fields derived during ingest:

| Column | From |
|---|---|
| `started_at`, `ended_at` | the two events' timestamps |
| `season_id` | the game's cached season calendar (`analysis/seasons.py`) |
| `my_name_source` | `mod`, or `battle log` if the account had to be inferred |
| `my_elo_after`, `my_exp_after`, `exp_delta`, `consecutive_wins` | the first `season_rank` snapshot after the match ended |
| `turns`, `went_first`, `prizes_taken`, `opponent_prizes_taken`, `cards_played`, `knockouts`, `shape_source` | parsed from the battle log (`cards_played` and `knockouts` are yours) |
| `hidden`, `hidden_at`, `hidden_reason` | `match_hidden` / `match_restored` events |

Booleans are stored as `0`/`1`. `opponent_deck`, `opponent_decklist`,
`opponent_deck_wins` and `opponent_deck_losses` exist for compatibility with older event
logs; the game never sends this build that information. Table **`season_rank`** holds every
snapshot, keyed by `recorded_at`.

### Other files

- `submit.json` - `{"key": "<64 hex chars>", "endpoint": "<url>", "enabled": true}`, created
  on first run.
- `submitted.json` - `{"<match_id>": "<sha256 of the match object as sent>"}`. A match is
  re-sent whenever its fingerprint changes.
- `install.json` - `{"game_data": "<path>", "installed_at": "...", "platform": "...",
  "repaired_at": "..."}`.
- `handoff.json` - written by the mod at an update hand-off: what the updater would have
  opened.

---

## Running your own leaderboard

### Point a tracker at your server

Set `"endpoint"` in `~/.ptcgl-tracker/submit.json` to your URL; the watcher uses it from the
next send. For a one-off run: `python analysis/submit.py --endpoint <url>`. To change the
default for everyone who installs your fork, edit `DEFAULT_ENDPOINT` in
`analysis/submit.py`.

`examples/receiver.py` is a complete, standard-library receiver to start from:

```
python examples/receiver.py --port 8765 --db leaderboard.sqlite
python analysis/submit.py --endpoint http://127.0.0.1:8765/submit.php
python analysis/submit.py --endpoint http://127.0.0.1:8765/submit.php --verify
```

### The protocol

**Request** - `POST <endpoint>` with

```
Content-Type: application/json
X-Tracker-Key: <the install's 64 lowercase hex characters>
User-Agent: ptcgl-tracker
```

and a body of

```json
{
  "payload_version": 1,
  "client": {"version": "1.0.0"},
  "account": {"display_name": "Ash"},
  "matches": [
    {
      "match_id": "0a1b2c3d4e5f...:9e8d7c6b-...",
      "started_at": "2026-10-06T23:41:48.100Z",
      "ended_at": "2026-10-06T23:47:47.922Z",
      "season_id": 54,
      "game_mode": "Standard",
      "gameplay_type": "Ranked",
      "i_am_player1": 1,
      "my_display_name": "Ash",
      "my_exp": 306, "my_exp_after": 319, "exp_delta": 13, "consecutive_wins": 3,
      "opponent_display_name": "Misty",
      "opponent_exp": 461,
      "result": "win",
      "end_reason": "match_results_victory_reason_opponent_concede",
      "turns": 5, "went_first": 0, "prizes_taken": 2, "opponent_prizes_taken": 3,
      "battle_log": "Setup\nAsh chose tails for the opening coin flip.\n...",
      "client_version": "1.43.0",
      "...": "every other column of the matches table that has a value"
    }
  ]
}
```

Each match object is its row from `matches.sqlite` - every column listed in `FIELDS` in
`analysis/submit.py` that is not null - plus `match_id`, plus `battle_log` as the log's
**text** rather than its path. `my_id_hash` and `opponent_id_hash` are never sent. Values
keep their SQLite types, so booleans arrive as `0`/`1`.

**Response** - HTTP 200 with a JSON object containing `"ok": true`. Anything else - another
status, `"ok": false`, or a body that is not JSON - is a failure; an `"error"` string is
logged if present.

**What a server can rely on, and must handle:**

- **Every match arrives at least twice**: at `match_start` with no result, and again when it
  ends. Later sends also follow any change - a `season_rank` snapshot attaching
  `my_elo_after`, a hidden match, a fixed parser. Upsert on `(X-Tracker-Key, match_id)`.
- **Each send is the whole match as it now stands.** Replace the stored match rather than
  merging into it, or a value the client has cleared will never clear on the server.
- **Requests are batched** up to about 2 MB of match objects. A batch is tried three times
  (waiting 1 s, then 3 s); on failure the client stops and retries everything unsent at the
  next match or watcher restart. Resubmission must be idempotent.
- **The key identifies an installation, not a person.** One machine can hold several game
  accounts (`my_display_name` tells them apart) and one person can have several machines.
  The key is what lets you refuse posts from an install you have blocked, and count how many
  installs corroborate the same match.
- A new install sends its whole history on first run, so the first request can be large.

**Verify (optional)** - `python analysis/submit.py --verify` sends
`GET <endpoint with "submit.php" replaced by "verify.php">` with the `X-Tracker-Key` header,
and expects

```json
{"ok": true, "known": true, "matches": 120, "submissions": 9,
 "span": {"profiles": 1}, "columns": {"match_id": 120, "result": 114, "...": 0},
 "hidden_true": 0}
```

where `columns` holds the non-null count of each field for that key (an empty string counts
as null) and `hidden_true` how many of its matches are hidden. It compares those with the
local database column by column, which catches fields silently dropped in transit. A server
may hold *more* hidden matches than the client, if it lets matches be hidden on the site;
only fewer counts as a mismatch.
Answer `{"ok": true, "known": false}` for a key you have never seen.

---

## What the game tells the client

Things worth knowing when building on this data, all observed from the client itself:

- **The opponent's deck is not sent.** Only the local player's `deckInfo` carries `cards`,
  `deckName` and the server's win/loss record. The opponent's carries `creationSource`,
  `iterationCount` and cosmetics (sleeve, deck box, coin).
- **`playerExp` is ladder points, not account XP.** It is the number the versus screen shows
  below the top league: +10 for a win plus a +3 streak bonus, and 0 in casual play.
- **`competitiveElo` exists only in the top league.** The client only fills it when
  `seasonLeagueNumber == 5`, so it reads 0 everywhere else; the tracker records that as
  absent. The season-rank cache reports `competitiveEloDefault` (1500) for an unplaced
  player, which cannot be told apart from a real 1500 by value alone - `ingest.py` only
  trusts a post-match Elo when the match started with one. In the cache `competitiveElo` is
  a per-game-mode dictionary; the tracker records the `Standard` entry.
- **Ratings are only in the match at its start.** A match's own effect on your rating comes
  from the next season-rank refresh, which carries `previousMatchExpDelta` and the streak.
- **Seasons** are cached as plain JSON in the client's config-cache
  (`~/AppData/LocalLow/pokemon/Pokemon TCG Live/config-cache` on Windows,
  `~/Library/Application Support/com.pokemon.pokemontcgl/config-cache` on macOS), one
  `season_<NNNN>_<version>.json` per season. Use `endDate` only: season N runs from season
  N-1's `endDate` to its own. `seasonTitleDate` is the month a season is *named* for, not
  when it starts. A date past the newest cached `endDate` is unknown, not the latest season.
- **Card names and sections** come from the client's own card database
  (`ManagerSingleton<CardDatabaseManager>.instance.cardDatabase`, `GetCardById(id)`, then
  `EnglishCardName`). `GetTrainerType` returns a bare int whose scheme is Item 0, Stadium 1,
  Supporter 2, Tool 3 - not the numbering of the game's `TrainerType` enum.
- **Per-match statistics are computed on the server.** `TrackedStats` lives in an assembly
  shared with the server, and a hook on its constructor never fires in the client; turns,
  prizes and knockouts come from the battle log instead.
- **End-of-match stats belong to a seat.** `player1Stats` is player 1's whoever won, so pick
  by `isPlayer1` alone.

---

## Building from source

The repository ships a built `mod/dist/CbrewTracker.dll`, so installing needs no .NET
toolchain. To build it yourself you need the .NET SDK (6 or later, on Windows or macOS - it
fetches the .NET Framework 4.7.2 reference assemblies from NuGet on the first build) and an
installed copy of the game, whose assemblies the mod compiles against but which cannot be
redistributed:

```
python scripts/copy-game-libs.py "<path to the game>"     # fills mod/libs/game/
dotnet build mod/CbrewTracker -c Release -o mod/build
```

With no path, `copy-game-libs.py` uses the game the installer found. The installers build
from source automatically whenever `dotnet` and `mod/libs/game/` are both present, and fall
back to `mod/dist/` otherwise; copy `mod/build/CbrewTracker.dll` over `mod/dist/` to ship
your build. Harmony (`mod/libs/harmony/0Harmony.dll`) is bundled.

If a game update renames a hook target, `tracker.log` says which hook failed and lists the
candidate types it can see. `ingest/clrmeta.py` reads type and method tables straight out of
the game's assemblies, without a decompiler, for comparing two game versions.

## Tests

```
python -m unittest discover -s tests -v                     # Windows: relauncher, locking, repair
dotnet run --project mod/tests/UpdaterHandoffTests -c Release   # Windows: hand-off argument rewriting
```

Both run against throwaway folders and never touch the real game or data folder.

## Repository layout

```
Install-Windows.cmd, Install-macOS.command   double-click installers
mod/CbrewTracker/          the in-game assembly (C#)
  Bootstrap.cs             entry point, applies the hooks
  Hooks/                   match, season-rank and updater hand-off hooks
  Emit/                    event log, battle-log writer, JSON, log file
  Discovery/               card database lookup, symbol diagnostics
mod/dist/                  the shipped build
mod/libs/harmony/          Harmony
mod/tests/                 tests for the updater hand-off
ingest/ingest.py           events.jsonl -> matches.sqlite
ingest/clrmeta.py          minimal .NET metadata reader
analysis/submit.py         matches.sqlite -> leaderboard
analysis/seasons.py        season calendar from the client's cache
scripts/                   installers, watcher, health check, repair, relauncher
examples/receiver.py       a minimal leaderboard server
tests/                     Windows hand-off and repair tests
```

## License

MIT - see [LICENSE](LICENSE). Harmony is MIT-licensed by Andreas Pardeike; see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
