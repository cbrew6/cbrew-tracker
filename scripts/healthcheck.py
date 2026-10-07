#!/usr/bin/env python3
"""Is the tracker still actually installed in the game?

Every game update replaces the app, which removes the two assemblies and both manifest
entries. The game then launches and plays perfectly normally and records nothing. This turns
that into something loud: the watcher calls it on a timer, repairs what it can, and opens
an alert page for what it cannot.

Checks the copy the installer actually wrote (recorded in ~/.ptcgl-tracker/install.json)
rather than re-guessing where the game lives.

    python3 scripts/healthcheck.py          # human-readable status, exit 1 if broken
    python3 scripts/healthcheck.py --json
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

DATA = os.path.expanduser("~/.ptcgl-tracker")
RECEIPT = os.path.join(DATA, "install.json")
TRACKER_LOG = os.path.join(DATA, "tracker.log")

ASSEMBLIES = ("CbrewTracker.dll", "0Harmony.dll")

# The three detours match capture cannot do without, named as the mod logs them. Checked by
# name rather than counted, so an install that lost one cannot pass, and a failure can say
# which one has gone - the only part anybody can act on.
REQUIRED_HOOKS = ("OnMatchCreation", "StartVersusScene", "LoadEndBattleScreen")

# Bootstrap catches this one's failure on purpose - a game update that moves the
# season-rank types costs live Elo and must not take match capture down with it - so it is
# reported here and never failed on.
ELO_HOOK = "UpdateInfoCache"


def _when(text):
    """One of the ISO stamps the mod and the installer write, as a datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _log_session():
    """(when the game last launched, [hooks it attached]) from the mod's log.

    The mod truncates this file at every launch and stamps it with the time it opened, so it
    describes exactly one launch. That timestamp is the point of reading it at all: without
    it, "every hook attached" is a claim about *some* launch, and the most likely candidate
    is the one before the game update - the mod is not loaded to rewrite its own log, so a
    game update leaves the last happy log sitting there untouched, describing a world that
    no longer exists.
    """
    try:
        with open(TRACKER_LOG, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None, []
    head = re.search(r"opened (\S+)", text)
    return (_when(head.group(1)) if head else None,
            [m.split(".")[-1] for m in re.findall(r"    patched (\S+)", text)])


def _receipt():
    # utf-8-sig, not utf-8: PowerShell's Set-Content -Encoding utf8 writes a BOM on
    # Windows, and json.load rejects it. utf-8-sig strips one if present and is
    # harmless on the macOS installer's plain UTF-8.
    try:
        with open(RECEIPT, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _app_bundle(game_data):
    """The .app `game_data` sits in, or None. Mirrors modinstall.app_bundle()."""
    resources = os.path.dirname(game_data)
    contents = os.path.dirname(resources)
    bundle = os.path.dirname(contents)
    if (os.path.basename(game_data) == "Data"
            and os.path.basename(resources) == "Resources"
            and os.path.basename(contents) == "Contents"
            and bundle.endswith(".app")):
        return bundle
    return None


def check():
    """Return a dict describing the install. `ok` is False only for a real problem."""
    out = {"ok": False, "installed": False, "problems": [], "game_data": None,
           "hooks": None, "hooks_at": None, "hooks_confirmed": None, "note": None}

    receipt = _receipt()
    if not receipt or not receipt.get("game_data"):
        out["problems"].append(
            "No install receipt found. Run the installer once to set the tracker up.")
        return out

    data = receipt["game_data"]
    out["game_data"] = data

    if not os.path.isdir(data):
        out["problems"].append(
            f"The game folder recorded at install time is gone:\n    {data}\n"
            "The game was moved, reinstalled, or updated into a new location.")
        return out

    managed = os.path.join(data, "Managed")
    missing = [d for d in ASSEMBLIES if not os.path.exists(os.path.join(managed, d))]
    if missing:
        out["problems"].append(
            f"{' and '.join(missing)} missing from the game's Managed folder — "
            "a game update replaced it.")

    # Both manifests are plain JSON the installer appends one entry to.
    try:
        with open(os.path.join(data, "ScriptingAssemblies.json"), encoding="utf-8") as fh:
            names = json.load(fh).get("names", [])
        if not all(d in names for d in ASSEMBLIES):
            out["problems"].append(
                "ScriptingAssemblies.json no longer lists the tracker's assemblies.")
    except (OSError, ValueError):
        out["problems"].append("ScriptingAssemblies.json is missing or unreadable.")

    try:
        with open(os.path.join(data, "RuntimeInitializeOnLoads.json"), encoding="utf-8") as fh:
            root = json.load(fh).get("root", [])
        if not any(e.get("assemblyName") == "CbrewTracker" for e in root):
            out["problems"].append(
                "RuntimeInitializeOnLoads.json no longer starts the tracker, so the "
                "game will not load it.")
    except (OSError, ValueError):
        out["problems"].append("RuntimeInitializeOnLoads.json is missing or unreadable.")

    # macOS only: a .app is a *sealed* bundle. Contents/_CodeSignature records a hash of
    # every file in it, so copying two assemblies into Managed/ leaves a signature that no
    # longer describes what is on disk. The installer re-signs to match; this is where that
    # is confirmed to have held.
    #
    # It is worth a subprocess every two minutes because of which way this fails. Every
    # other problem here means the game runs and records nothing. A broken seal means the
    # game may not start at all - and nobody whose game stopped launching would think to
    # look at a match tracker for the reason.
    if sys.platform == "darwin":
        bundle = _app_bundle(data)
        if bundle:
            try:
                signed = subprocess.run(["codesign", "--verify", "--strict", bundle],
                                        capture_output=True, text=True, timeout=120)
                if signed.returncode != 0:
                    out["problems"].append(
                        "The game's signature no longer matches its contents, so macOS "
                        "may refuse to launch it.")
            except Exception:
                # Could not run codesign at all. Not evidence of a problem, and inventing
                # one here would send the watcher into a repair loop it cannot win.
                pass

    out["installed"] = not out["problems"]

    # The install can be intact while the hooks still failed to attach - that is what a
    # changed game version looks like, and no amount of re-copying the same assembly fixes
    # it. Only ever reported, never repaired.
    opened, patched = _log_session()
    out["hooks"] = len(patched) or None
    out["hooks_at"] = opened.isoformat() if opened else None

    # **The evidence has to be newer than the install it vouches for.** A game update takes
    # the mod out, so the mod is not running to say anything about it; the log still holds
    # the last good launch, from before the update. Reading that as confirmation is how a
    # repair gets declared working without a single game having been launched since - the
    # files are back, the stale log says every hook attached, and nothing has actually
    # been tested. Whether the repair held is not knowable until the game next starts, and
    # saying so is better than a confident answer drawn from a stale file.
    stamped = [t for t in (_when((receipt or {}).get("installed_at")),
                           _when((receipt or {}).get("repaired_at"))) if t]
    fresh = bool(opened and (not stamped or opened >= max(stamped)))
    out["hooks_confirmed"] = None

    if out["installed"]:
        if not fresh:
            out["note"] = ("Installed. The game has not been launched since, so nothing "
                           "has confirmed the tracker actually loads yet.")
        else:
            missing = [h for h in REQUIRED_HOOKS if h not in patched]
            out["hooks_confirmed"] = not missing
            if missing:
                out["problems"].append(
                    "The tracker loaded on the last launch but could not attach "
                    + ", ".join(missing) + ". A game update moved what it patches, so "
                    "matches will record incompletely or not at all. This needs a newer "
                    "build of the tracker - putting the same one back cannot fix it.")
            elif ELO_HOOK not in patched:
                out["note"] = ("Installed and recording matches. The season-rank hook did "
                               "not attach, so live Elo is not being captured.")
            else:
                out["note"] = "Installed, and every hook attached on the last launch."

    out["ok"] = not out["problems"]
    return out


def describe(status):
    """One-line summary plus the problems, for a log."""
    if status["ok"]:
        return status["note"] or "Tracker install looks healthy."
    return "TRACKER NOT WORKING — " + " ".join(status["problems"])


# Problems the watcher can undo by itself, from the copy the installer stashed. Every one of
# these is a thing a *game update* does; none of them is a machine that changed underneath us.
_REPAIRABLE = (
    "missing from the game's Managed folder",
    "no longer lists the tracker's assemblies",
    "no longer starts the tracker",
    "ScriptingAssemblies.json is missing or unreadable",
    "RuntimeInitializeOnLoads.json is missing or unreadable",
    # macOS: re-sealing the bundle is part of apply(), so a repair fixes this by doing
    # exactly what it already does for everything else on this list.
    "signature no longer matches its contents",
)


def repairable(status):
    """Can `modinstall.repair()` plausibly fix EVERY problem here?

    All of them, not any of them. A run that fixes three problems and leaves a fourth has
    not repaired the install - it has made the remaining fault harder to read, because the
    log now says a repair happened.

    Two things are deliberately not repairable, because copying files would not address them
    and pretending otherwise would loop every two minutes forever:

      * **No receipt, or the game folder has moved.** Finding the game again is the
        installer's job - it holds the search paths, and guessing here risks writing into a
        different copy of the game.
      * **The tracker loaded but attached too few hooks.** The files are all present and
        correct; the game changed shape underneath them. That needs a new build of the mod,
        which no amount of re-copying the old one will produce.
    """
    if status["ok"] or not status["game_data"]:
        return False
    return bool(status["problems"]) and all(
        any(sig in problem for sig in _REPAIRABLE) for problem in status["problems"])


ALERT_PATH = os.path.join(DATA, "tracker-alert.html")

# What the page can say. A game update breaks the install in a way the watcher repairs by
# itself, so "nothing is being recorded" and "you have to go and fix this" are two different
# pieces of news, and the page has to keep them apart. Telling everybody to re-run the
# installer would be wrong for the commonest breakage there is - and it is advice people act
# on, long after the tracker has put itself back.
PENDING, MANUAL, RESOLVED = "pending", "manual", "resolved"

# The page is a file, and whoever reads it is looking at a browser tab opened minutes ago.
# PENDING resolves itself within a couple of minutes of the game closing, so that tab must
# not still be giving the old instructions once it has.
_REFRESH = {PENDING: 30, MANUAL: 60, RESOLVED: 600}

_COPY = {
    PENDING: {
        "tone": "alarm",
        "badge": "Not recording",
        "head": "Your matches are not being tracked",
        "lead": "A game update removed the tracker, which is what every game update does. "
                "The game still runs normally, which is exactly why this is easy to miss. "
                "<b>You do not need the installer for this one</b> - the tracker keeps a "
                "copy of itself on this machine and puts it back on its own.",
        "steps_head": "What to do",
        "steps": ["Quit Pok&eacute;mon TCG Live.",
                  "Leave it a couple of minutes. The tracker cannot write into the game "
                  "folder while the game is running, which is the only thing it is "
                  "waiting for.",
                  "Start the game again. This page says when that is done, so there is "
                  "nothing to watch for."],
        "foot": "Nothing is recorded in the meantime, including any match played before "
                "the game is closed. If this turns out not to be repairable after all, "
                "this page changes to say so.",
    },
    MANUAL: {
        "tone": "alarm",
        "badge": "Not recording",
        "head": "Your matches are not being tracked",
        "lead": "The game still runs normally, which is exactly why this is easy to miss. "
                "Nothing is being recorded until this is fixed - and this is one the "
                "tracker could not put right on its own.",
        "steps_head": "How to fix it",
        "steps": ["Quit Pok&eacute;mon TCG Live if it is open.",
                  "Run <code>{installer}</code> again - the same file you used to set "
                  "this up.",
                  "Start the game, play a match, and this page will stop appearing."],
        "foot": "A game update removes the tracker every time, and the tracker normally "
                "puts itself back within a couple of minutes of the game closing - you "
                "would never see this page for that. Something here needed a hand: usually "
                "the game moved, or a new version of the tracker is needed.",
    },
    RESOLVED: {
        "tone": "ok",
        "badge": "Recording again",
        "head": "The tracker is back",
        "lead": "{how} There is nothing further to do, and no installer was involved.",
        "steps_head": "One thing left",
        "steps": ["Start Pok&eacute;mon TCG Live - or restart it if it is open, because "
                  "the game only loads the tracker at launch."],
        "foot": "Matches played while it was missing were not recorded, and there is no "
                "way to recover those. Everything from here on is.",
    },
}

_HOW = {
    True: "A game update had removed it. It has been put back from the copy kept on this "
          "machine.",
    False: "Whatever was wrong with the install has been put right.",
}

_ALERT = """<title>{title}</title>
<meta http-equiv="refresh" content="{refresh}">
<style>
:root{{--ground:#FBF4F2;--card:#FFFFFF;--ink:#1A1113;--ink-2:#5B4A4E;
  --alarm:#B03350;--ok:#2F7D5B;--line:#E7D5D8}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
  --ground:#170F11;--card:#211619;--ink:#F3E9EB;--ink-2:#B49CA2;
  --alarm:#F0748E;--ok:#63C295;--line:#3A272C}}}}
:root[data-theme="dark"]{{--ground:#170F11;--card:#211619;--ink:#F3E9EB;
  --ink-2:#B49CA2;--alarm:#F0748E;--ok:#63C295;--line:#3A272C}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;line-height:1.6}}
.wrap{{max-width:640px;margin:0 auto;padding:56px 24px 80px}}
.badge{{display:inline-block;background:var(--{tone});color:#fff;font-size:11px;
  font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:5px 11px;border-radius:2px}}
h1{{font-size:clamp(26px,5vw,38px);letter-spacing:-.02em;margin:18px 0 8px;
  text-wrap:balance}}
p.lead{{color:var(--ink-2);margin:0 0 26px;font-size:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--alarm);
  border-radius:3px;padding:18px 20px;margin:0 0 14px}}
.card h2{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--alarm);margin:0 0 8px}}
.card p{{margin:0;white-space:pre-wrap}}
.fix{{background:var(--card);border:1px solid var(--line);border-radius:3px;
  padding:20px;margin-top:26px}}
.fix h2{{font-size:13px;letter-spacing:.06em;text-transform:uppercase;margin:0 0 10px}}
.fix ol{{margin:0;padding-left:20px}}
.fix li{{margin:7px 0}}
code{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13px;
  background:var(--ground);border:1px solid var(--line);padding:2px 6px;border-radius:2px}}
footer{{margin-top:26px;color:var(--ink-2);font-size:13px}}
</style>
<div class="wrap">
<span class="badge">{badge}</span>
<h1>{head}</h1>
<p class="lead">{lead}</p>
{problems}
<div class="fix">
<h2>{steps_head}</h2>
<ol>{steps}</ol>
</div>
<footer>{foot}</footer>
</div>
"""


def alert_html(status, mode=MANUAL, repaired=False):
    """A standalone page saying whether anything is being recorded, and what to do about it.

    `mode` is the difference between the three things this can be: a breakage the watcher is
    already dealing with (PENDING), one that needs the person (MANUAL), and the news that it
    is over (RESOLVED). They are one page rather than three because it is one *file*, and a
    browser tab may already be showing it - that tab has to be able to become the next thing
    the page says.
    """
    import html as _html

    copy = _COPY[mode]
    if mode == RESOLVED:
        cards = ""
        lead = copy["lead"].format(how=_HOW[bool(repaired)])
    else:
        cards = "".join(
            f'<div class="card"><h2>Problem</h2><p>{_html.escape(p)}</p></div>'
            for p in status.get("problems") or [])
        cards = cards or '<div class="card"><p>Unknown problem.</p></div>'
        lead = copy["lead"]

    installer = "Install-Windows.cmd" if os.name == "nt" else "Install-macOS.command"
    steps = "".join("<li>%s</li>" % s.format(installer=_html.escape(installer))
                    for s in copy["steps"])
    return _ALERT.format(
        title=copy["head"], refresh=_REFRESH[mode], tone=copy["tone"], badge=copy["badge"],
        head=copy["head"], lead=lead, problems=cards, steps_head=copy["steps_head"],
        steps=steps, foot=copy["foot"])


def write_alert(status, path=ALERT_PATH, mode=MANUAL, repaired=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(alert_html(status, mode, repaired))
    os.replace(tmp, path)
    return path


def clear_alert(path=ALERT_PATH, repaired=False):
    """Turn a standing alert into the news that it is over. Returns the path, or None.

    Only ever rewrites a page that is already there: somebody has that tab open and the
    instructions on it are now wrong. It does not *create* one, because a page nobody asked
    for saying everything is fine is pure noise - and for the same reason it is never opened
    in a browser. The tab refreshes into it by itself.
    """
    if not os.path.exists(path):
        return None
    return write_alert({"problems": []}, path, RESOLVED, repaired)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    status = check()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(describe(status))
        if status["game_data"]:
            print(f"  game: {status['game_data']}")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
