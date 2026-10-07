#!/usr/bin/env python3
"""Copy the game assemblies the mod compiles against into mod/libs/game.

    python scripts/copy-game-libs.py                      # the game the installer found
    python scripts/copy-game-libs.py "<path to the game>"  # or point at it

Only needed to build the mod from source. They are Pokemon's assemblies and are not
redistributable, which is why they are not in the repository; this copies them from your
own install.

The path can be the game's folder, its .exe, its "<Name>_Data" folder, the macOS .app, or
the Data folder inside it. With no path, the install receipt the installer wrote
(~/.ptcgl-tracker/install.json) says where the game is.
"""

import glob
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "mod", "libs", "game")
RECEIPT = os.path.expanduser("~/.ptcgl-tracker/install.json")

# Every game assembly mod/CbrewTracker/CbrewTracker.csproj references.
NEEDED = (
    "TPCI.RainierClient.dll",
    "SharedLogicUtils.dll",
    "RainierClientSDK.dll",
    "MatchLogic.dll",
    "UnityEngine.dll",
    "UnityEngine.CoreModule.dll",
)


def managed_folder(path):
    """The game's Managed folder for any of the paths described above, or None."""
    path = os.path.abspath(os.path.expanduser(path))
    candidates = [path]
    if path.lower().endswith(".exe"):
        candidates.append(path[:-4] + "_Data")
    candidates.append(os.path.join(path, "Contents", "Resources", "Data"))
    candidates += glob.glob(os.path.join(path, "*_Data"))
    candidates += glob.glob(os.path.join(path, "*.app", "Contents", "Resources", "Data"))
    for data in candidates:
        managed = os.path.join(data, "Managed")
        if os.path.isdir(managed):
            return managed
    return None


def main(argv):
    if argv:
        source = argv[0]
    else:
        try:
            with open(RECEIPT, encoding="utf-8-sig") as fh:
                source = json.load(fh).get("game_data")
        except (OSError, ValueError):
            source = None
        if not source:
            print("No install receipt. Pass the game's location:\n"
                  "  python scripts/copy-game-libs.py \"<path to the game>\"")
            return 1

    managed = managed_folder(source)
    if not managed:
        print("No Managed folder found at or under %s" % source)
        return 1

    missing = [n for n in NEEDED if not os.path.isfile(os.path.join(managed, n))]
    if missing:
        print("The game at %s is missing %s - has it been renamed in a game update?"
              % (managed, ", ".join(missing)))
        return 1

    os.makedirs(DEST, exist_ok=True)
    for name in NEEDED:
        shutil.copy2(os.path.join(managed, name), os.path.join(DEST, name))
        print("copied %s" % name)
    print("from %s\n  to %s" % (managed, DEST))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
