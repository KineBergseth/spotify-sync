"""
Add or rename sub-playlists in one step.

Today, adding or renaming a sub means hand-editing config.json (the "subs"
entry AND every master's "include" list that mentions it), plus
playlist_ids.json, and it's easy to forget one of them or let "in_masters"
drift out of sync with what "include" actually says. This script does the
whole edit as one operation, and a "validate" command lets you check for
drift at any time (e.g. after a manual edit, or before committing).

Usage:

    # Add a new sub-playlist, wiring it into one or more existing masters.
    # Creates the Spotify playlist if it doesn't already exist under that
    # exact name (adopts an existing one of yours with the same name).
    python manage_playlists.py add-sub "JAZZ · Vocal & Standards" \\
        --masters "M · VINTAGE SWING & JAZZ"

    # A holding-pen sub that feeds no master yet:
    python manage_playlists.py add-sub "TEMP · Triage"

    # Rename a sub everywhere it's referenced: config.json ("subs[].name"
    # and every master's "include[]"), playlist_ids.json, descriptions.json
    # cache keys, and (by default) the actual Spotify playlist name.
    python manage_playlists.py rename-sub "OLD NAME" "NEW NAME"

    # Preview any command without writing anything or calling Spotify writes:
    python manage_playlists.py add-sub "..." --dry-run
    python manage_playlists.py rename-sub "..." "..." --dry-run

    # Check config.json / playlist_ids.json / descriptions.json agree with
    # each other. Read-only, no Spotify calls. Run this any time.
    python manage_playlists.py validate

    # Don't want to remember flags/quoting? Run with no arguments at all for
    # an interactive menu that lists your actual subs/masters to pick from:
    python manage_playlists.py

After add-sub or rename-sub, run `python sync.py` so masters and playlist
descriptions catch up (a renamed sub's own description cache entry is moved
for you, but any master description that lists it by name is left for
sync.py to refresh, since that requires live track counts).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from spotify_api import Spotify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
IDS_PATH = "playlist_ids.json"
DESC_PATH = "descriptions.json"


# ------------------------------------------------------------------ io


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path: str, data, indent: int) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------- validation


def validate(config: dict, ids: dict) -> list[str]:
    """Structural consistency checks. Returns a list of problems (empty = clean)."""
    problems: list[str] = []

    sub_names = [s["name"] for s in config["subs"]]
    master_names = [m["name"] for m in config["masters"]]

    dup_subs = {n for n in sub_names if sub_names.count(n) > 1}
    if dup_subs:
        problems.append(f"Duplicate sub name(s) in config.json: {sorted(dup_subs)}")

    dup_masters = {n for n in master_names if master_names.count(n) > 1}
    if dup_masters:
        problems.append(f"Duplicate master name(s) in config.json: {sorted(dup_masters)}")

    sub_set = set(sub_names)
    for master in config["masters"]:
        for ref in master.get("include", []):
            if ref not in sub_set:
                problems.append(
                    f"Master {master['name']!r} includes unknown sub {ref!r} "
                    "(renamed sub whose master entry wasn't updated?)"
                )

    # in_masters (denormalized, informational) vs. what "include" actually says
    feeds: dict[str, set[str]] = {n: set() for n in sub_names}
    for master in config["masters"]:
        for ref in master.get("include", []):
            if ref in feeds:
                feeds[ref].add(master["name"])
    for sub in config["subs"]:
        declared = set(sub.get("in_masters", []))
        actual = feeds.get(sub["name"], set())
        if declared != actual:
            problems.append(
                f"Sub {sub['name']!r}: in_masters says {sorted(declared)}, "
                f"but master include[] lists say {sorted(actual)}"
            )

    # config.json vs playlist_ids.json
    for name, entry_id in [(s["name"], s["id"]) for s in config["subs"]] + \
                          [(m["name"], m["id"]) for m in config["masters"]] + \
                          [(config["inbox"]["name"], config["inbox"]["id"])]:
        if name not in ids:
            problems.append(f"{name!r} is in config.json but missing from playlist_ids.json")
        elif ids[name] != entry_id:
            problems.append(
                f"{name!r} has id {entry_id!r} in config.json but {ids[name]!r} in playlist_ids.json"
            )

    return problems


def cmd_validate(_args) -> int:
    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("%s not found.", CONFIG_PATH)
        return 2
    ids = load_json(IDS_PATH, {})

    problems = validate(config, ids)
    if not problems:
        log.info("OK — config.json, playlist_ids.json, and in_masters all agree.")
        return 0

    log.error("%d problem(s) found:", len(problems))
    for p in problems:
        log.error("  - %s", p)
    return 1


# -------------------------------------------------------------- add-sub


def cmd_add_sub(args) -> int:
    name = args.name.strip()
    requested_masters = [m.strip() for m in (args.masters or "").split(",") if m.strip()]

    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        return 2
    ids = load_json(IDS_PATH, {})
    desc_cache = load_json(DESC_PATH, {})

    existing_sub = next((s for s in config["subs"] if s["name"] == name), None)

    master_names = {m["name"] for m in config["masters"]}
    unknown = [m for m in requested_masters if m not in master_names]
    if unknown:
        log.error("Unknown master(s): %s", unknown)
        log.error("Known masters: %s", sorted(master_names))
        return 2

    if existing_sub and set(existing_sub.get("in_masters", [])) >= set(requested_masters):
        log.info("Sub %r already exists and is already wired to the requested masters.", name)
        log.info("Nothing to do. (Use rename-sub to rename it, or edit config.json's "
                 "include[] by hand to remove it from a master.)")
        return 0

    # Resolve/create the Spotify playlist. Idempotent: reuses an id we already
    # recorded, or an existing playlist of ours with this exact name, before
    # ever creating a new one.
    pid = ids.get(name) or (existing_sub or {}).get("id")
    created = False

    if args.dry_run:
        log.info("[dry-run] add-sub %r, masters=%s", name, requested_masters or "(none)")
        if not pid:
            log.info("[dry-run] would create/adopt the Spotify playlist")
        log.info("[dry-run] would update config.json and playlist_ids.json")
        return 0

    sp = Spotify.from_env()
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    if not pid:
        live = sp.my_playlists()
        if name in live:
            pid = live[name]
            log.info("Adopting existing Spotify playlist %r (%s)", name, pid)
        else:
            desc = (
                f"Feeds: {', '.join(sorted(requested_masters))}"
                if requested_masters else "Holding pen. Feeds no master."
            )
            pid = sp.create_playlist(name, desc, public=args.public)
            created = True
            log.info("Created playlist %r -> %s", name, pid)

    ids[name] = pid

    if existing_sub:
        sub_entry = existing_sub
    else:
        sub_entry = {"name": name, "id": pid, "enabled": True, "in_masters": []}
        config["subs"].append(sub_entry)
        config["subs"].sort(key=lambda s: s["name"])

    sub_entry["id"] = pid
    in_masters = set(sub_entry.get("in_masters", []))

    for master in config["masters"]:
        if master["name"] in requested_masters:
            include = master.setdefault("include", [])
            if name not in include:
                include.append(name)
                include.sort()
            in_masters.add(master["name"])

    sub_entry["in_masters"] = sorted(in_masters)

    problems = validate(config, ids)
    if problems:
        log.error("Refusing to write: resulting config.json would be inconsistent:")
        for p in problems:
            log.error("  - %s", p)
        return 1

    save_json_atomic(IDS_PATH, ids, indent=2)
    save_json_atomic(CONFIG_PATH, config, indent=2)
    log.info("%s %r (%s), feeding: %s",
             "Created" if created else "Wired up", name, pid, sorted(in_masters) or "(none)")
    log.info("Wrote %s and %s. Run: python sync.py", IDS_PATH, CONFIG_PATH)
    return 0


# ----------------------------------------------------------- rename-sub


def cmd_rename_sub(args) -> int:
    old, new = args.old.strip(), args.new.strip()
    if old == new:
        log.error("Old and new name are identical.")
        return 2

    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        return 2
    ids = load_json(IDS_PATH, {})
    desc_cache = load_json(DESC_PATH, {})

    sub = next((s for s in config["subs"] if s["name"] == old), None)
    if sub is None:
        log.error("No sub named %r in config.json.", old)
        return 2
    if any(s["name"] == new for s in config["subs"]):
        log.error("A sub named %r already exists.", new)
        return 2

    if args.dry_run:
        affected = [m["name"] for m in config["masters"] if old in m.get("include", [])]
        log.info("[dry-run] rename-sub %r -> %r", old, new)
        log.info("[dry-run] would update config.json (subs[].name, in_masters, and "
                 "include[] in: %s)", affected or "(no masters reference it)")
        log.info("[dry-run] would update playlist_ids.json key")
        log.info("[dry-run] would move descriptions.json cache entries")
        if not args.no_rename_on_spotify:
            log.info("[dry-run] would rename the Spotify playlist itself")
        return 0

    sp = None
    if not args.no_rename_on_spotify:
        sp = Spotify.from_env()
        log.info("Authenticated as: %s", sp.me().get("display_name"))
        sp.rename_playlist(sub["id"], new)
        log.info("Renamed Spotify playlist %s -> %r", sub["id"], new)

    sub["name"] = new
    for master in config["masters"]:
        include = master.get("include", [])
        if old in include:
            include.remove(old)
            include.append(new)
            include.sort()

    if old in ids:
        ids[new] = ids.pop(old)
    else:
        ids[new] = sub["id"]

    for key_old, key_new in [(old, new), (f"sub:{old}", f"sub:{new}")]:
        if key_old in desc_cache:
            desc_cache[key_new] = desc_cache.pop(key_old)

    problems = validate(config, ids)
    if problems:
        log.error("Refusing to write: resulting config.json would be inconsistent:")
        for p in problems:
            log.error("  - %s", p)
        return 1

    save_json_atomic(IDS_PATH, ids, indent=2)
    save_json_atomic(CONFIG_PATH, config, indent=2)
    save_json_atomic(DESC_PATH, desc_cache, indent=1)

    log.info("Renamed %r -> %r in config.json, playlist_ids.json, descriptions.json.", old, new)
    log.info("Any master description listing this sub by name will refresh on the next: python sync.py")
    return 0


# ------------------------------------------------------------- interactive


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def _ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _pick_many(items: list[str], label: str) -> list[str]:
    """Numbered multi-select. Accepts comma/space-separated numbers, or blank for none."""
    if not items:
        return []
    print(f"\n{label}:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    raw = input("Enter number(s), comma-separated (blank for none): ").strip()
    if not raw:
        return []
    chosen = []
    for part in raw.replace(",", " ").split():
        try:
            idx = int(part)
            if 1 <= idx <= len(items):
                chosen.append(items[idx - 1])
        except ValueError:
            print(f"  (ignoring {part!r}, not a number)")
    return chosen


def _pick_one(items: list[str], label: str) -> str | None:
    if not items:
        return None
    print(f"\n{label}:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    raw = input("Enter a number: ").strip()
    try:
        idx = int(raw)
        if 1 <= idx <= len(items):
            return items[idx - 1]
    except ValueError:
        pass
    print("  Not a valid choice.")
    return None


class _Args:
    """Tiny stand-in so interactive flows can call the cmd_* functions directly."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def interactive() -> int:
    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        return 2

    print("What do you want to do?")
    print("  1. Add a new sub-playlist")
    print("  2. Rename an existing sub-playlist")
    print("  3. Validate (check files agree with each other)")
    print("  4. Quit")
    choice = input("> ").strip()

    if choice == "1":
        name = _ask("New sub-playlist name (must match Spotify exactly if it already exists)")
        if not name:
            print("No name entered, cancelling.")
            return 1
        master_names = sorted(m["name"] for m in config["masters"])
        chosen_masters = _pick_many(master_names, "Which masters should it feed? (optional)")
        public = _ask_yes_no("Make it public? (default private)", default=False)
        dry = _ask_yes_no("Preview only (dry-run)?", default=True)
        args = _Args(name=name, masters=",".join(chosen_masters), public=public, dry_run=dry)
        rc = cmd_add_sub(args)
        if dry and rc == 0:
            if _ask_yes_no("Look good — apply it for real now?", default=True):
                args.dry_run = False
                rc = cmd_add_sub(args)
        return rc

    if choice == "2":
        sub_names = sorted(s["name"] for s in config["subs"])
        old = _pick_one(sub_names, "Which sub do you want to rename?")
        if not old:
            return 1
        new = _ask(f"New name for {old!r}")
        if not new:
            print("No new name entered, cancelling.")
            return 1
        already_renamed = _ask_yes_no(
            "Already renamed it in Spotify yourself? (if yes, skip renaming it there)", default=False
        )
        dry = _ask_yes_no("Preview only (dry-run)?", default=True)
        args = _Args(old=old, new=new, no_rename_on_spotify=already_renamed, dry_run=dry)
        rc = cmd_rename_sub(args)
        if dry and rc == 0:
            if _ask_yes_no("Look good — apply it for real now?", default=True):
                args.dry_run = False
                rc = cmd_rename_sub(args)
        return rc

    if choice == "3":
        return cmd_validate(None)

    return 0


# -------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=False)

    p_add = sub.add_parser("add-sub", help="create/adopt a sub-playlist and wire it into masters")
    p_add.add_argument("name", help="exact sub-playlist name, e.g. 'JAZZ · Vocal & Standards'")
    p_add.add_argument("--masters", default="", help="comma-separated master names to add it to")
    p_add.add_argument("--public", action="store_true", help="create as public (default: private)")
    p_add.add_argument("--dry-run", action="store_true")
    p_add.set_defaults(func=cmd_add_sub)

    p_ren = sub.add_parser("rename-sub", help="rename a sub everywhere it's referenced")
    p_ren.add_argument("old", help="current exact sub-playlist name")
    p_ren.add_argument("new", help="new exact sub-playlist name")
    p_ren.add_argument("--no-rename-on-spotify", action="store_true",
                       help="update the repo's files only; leave the Spotify playlist's own name as-is")
    p_ren.add_argument("--dry-run", action="store_true")
    p_ren.set_defaults(func=cmd_rename_sub)

    p_val = sub.add_parser("validate", help="check config.json / playlist_ids.json / in_masters agree")
    p_val.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    if args.command is None:
        return interactive()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())