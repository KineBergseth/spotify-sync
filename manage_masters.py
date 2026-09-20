"""
Create, rename, and wire Spotify master playlists safely.

Masters are derived views. This tool edits their structure; sync.py remains the
only thing that should populate their tracks.

Commands:

    # Create/adopt a union master. Repeat --sub once per feeder.
    python manage_masters.py add-master "M · LATIN" \
        --sub "LATIN · Latin Pop" \
        --sub "LATIN · Reggaeton & Latin Urban" \
        --sub "LATIN · Salsa, Cumbia & Tropical"

    # Rename a master everywhere, including Spotify by default.
    python manage_masters.py rename-master "M · OLD" "M · NEW"

    # Replace a union master's complete feeder list atomically.
    python manage_masters.py set-feeders "M · LATIN" \
        --sub "LATIN · Latin Pop" \
        --sub "LATIN · Reggaeton & Latin Urban"

    # Intentionally give a union master no feeders.
    python manage_masters.py set-feeders "M · LATIN" --clear

    # Preview any change without file writes or Spotify writes.
    python manage_masters.py add-master "..." --dry-run
    python manage_masters.py rename-master "..." "..." --dry-run
    python manage_masters.py set-feeders "..." --sub "..." --dry-run

    # Read-only consistency check.
    python manage_masters.py validate

Run with no arguments for an interactive menu. After structural changes, run
python sync.py so the derived master contents and Spotify descriptions catch up.
"""

from __future__ import annotations

import argparse
import copy
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


# ------------------------------------------------------------- structure


def rebuild_in_masters(config: dict) -> None:
    """Rebuild the denormalized subs[].in_masters lists from master include[]."""
    feeds: dict[str, set[str]] = {s["name"]: set() for s in config["subs"]}
    for master in config["masters"]:
        for sub_name in master.get("include", []):
            if sub_name in feeds:
                feeds[sub_name].add(master["name"])
    for sub in config["subs"]:
        sub["in_masters"] = sorted(feeds[sub["name"]])


def validate(config: dict, ids: dict) -> list[str]:
    """Structural consistency checks. Empty list means the repo wiring is clean."""
    problems: list[str] = []

    sub_names = [s["name"] for s in config.get("subs", [])]
    master_names = [m["name"] for m in config.get("masters", [])]

    dup_subs = sorted({n for n in sub_names if sub_names.count(n) > 1})
    dup_masters = sorted({n for n in master_names if master_names.count(n) > 1})
    if dup_subs:
        problems.append(f"Duplicate sub name(s) in config.json: {dup_subs}")
    if dup_masters:
        problems.append(f"Duplicate master name(s) in config.json: {dup_masters}")

    sub_set = set(sub_names)
    for master in config.get("masters", []):
        if master.get("type", "union") == "union":
            include = master.get("include", [])
            if len(include) != len(set(include)):
                problems.append(f"Master {master['name']!r} has duplicate feeder(s)")
            for ref in include:
                if ref not in sub_set:
                    problems.append(f"Master {master['name']!r} includes unknown sub {ref!r}")

    feeds: dict[str, set[str]] = {n: set() for n in sub_names}
    for master in config.get("masters", []):
        for ref in master.get("include", []):
            if ref in feeds:
                feeds[ref].add(master["name"])
    for sub in config.get("subs", []):
        declared = set(sub.get("in_masters", []))
        actual = feeds.get(sub["name"], set())
        if declared != actual:
            problems.append(
                f"Sub {sub['name']!r}: in_masters says {sorted(declared)}, "
                f"but master include[] lists say {sorted(actual)}"
            )

    inbox = config.get("inbox", {})
    known = [(s["name"], s["id"]) for s in config.get("subs", [])]
    known += [(m["name"], m["id"]) for m in config.get("masters", [])]
    if inbox.get("name") and inbox.get("id"):
        known.append((inbox["name"], inbox["id"]))

    known_names = {name for name, _ in known}
    for name, entry_id in known:
        if name not in ids:
            problems.append(f"{name!r} is in config.json but missing from playlist_ids.json")
        elif ids[name] != entry_id:
            problems.append(
                f"{name!r} has id {entry_id!r} in config.json but "
                f"{ids[name]!r} in playlist_ids.json"
            )

    for name in ids:
        if name not in known_names:
            problems.append(
                f"{name!r} is in playlist_ids.json but not in config.json at all "
                "(orphaned playlist)"
            )

    return problems


def _load_repo() -> tuple[dict, dict, dict] | None:
    config = load_json(CONFIG_PATH, None)
    if config is None:
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        return None
    return config, load_json(IDS_PATH, {}), load_json(DESC_PATH, {})


def _enabled_sub_names(config: dict) -> list[str]:
    return sorted(s["name"] for s in config["subs"] if s.get("enabled", True))


def _validate_requested_subs(config: dict, requested: list[str]) -> list[str]:
    enabled = set(_enabled_sub_names(config))
    all_subs = {s["name"]: s for s in config["subs"]}
    problems: list[str] = []

    if len(requested) != len(set(requested)):
        problems.append("The feeder list contains the same sub more than once.")

    for name in requested:
        if name not in all_subs:
            problems.append(f"Unknown sub-playlist: {name!r}")
        elif name not in enabled:
            problems.append(f"Sub-playlist is disabled and cannot feed a master: {name!r}")
    return problems


def _name_collision(config: dict, name: str, *, ignore_master: str | None = None) -> str | None:
    inbox_name = config.get("inbox", {}).get("name")
    if name == inbox_name:
        return "the INBOX"
    if any(s["name"] == name for s in config["subs"]):
        return "a sub-playlist"
    if any(m["name"] == name and m["name"] != ignore_master for m in config["masters"]):
        return "another master"
    return None


# -------------------------------------------------------------- validate


def cmd_validate(_args) -> int:
    loaded = _load_repo()
    if loaded is None:
        return 2
    config, ids, _ = loaded

    problems = validate(config, ids)
    if not problems:
        log.info("OK — config.json, playlist_ids.json, and in_masters all agree.")
        return 0

    log.error("%d problem(s) found:", len(problems))
    for p in problems:
        log.error("  - %s", p)
    return 1


# ----------------------------------------------------------- add-master


def cmd_add_master(args) -> int:
    name = args.name.strip()
    feeders = sorted(dict.fromkeys(args.sub or []))

    if not name:
        log.error("Master name cannot be blank.")
        return 2

    loaded = _load_repo()
    if loaded is None:
        return 2
    config, ids, _desc_cache = loaded

    collision = _name_collision(config, name)
    if collision:
        existing = next((m for m in config["masters"] if m["name"] == name), None)
        if existing:
            log.error("Master %r already exists. Use set-feeders or rename-master.", name)
        else:
            log.error("Name %r is already used by %s.", name, collision)
        return 2

    feeder_problems = _validate_requested_subs(config, feeders)
    if feeder_problems:
        for p in feeder_problems:
            log.error(p)
        return 2

    pid = ids.get(name)

    # Validate the prospective repo state before any Spotify write. Use a
    # placeholder id when the playlist does not exist yet; the real id is
    # substituted after create/adopt. This keeps add-master fail-closed.
    preview_config = copy.deepcopy(config)
    preview_ids = dict(ids)
    preview_pid = pid or "__PENDING_MASTER_ID__"
    preview_ids[name] = preview_pid
    preview_config["masters"].append(
        {
            "name": name,
            "id": preview_pid,
            "enabled": True,
            "type": "union",
            "include": feeders,
        }
    )
    preview_config["masters"].sort(key=lambda m: m["name"])
    rebuild_in_masters(preview_config)
    preview_problems = validate(preview_config, preview_ids)
    if preview_problems:
        log.error("Refusing to add master: the resulting structure would be inconsistent:")
        for problem in preview_problems:
            log.error("  - %s", problem)
        return 1

    if args.dry_run:
        log.info("[dry-run] add-master %r", name)
        log.info("[dry-run] feeders (%d): %s", len(feeders), feeders or "(none)")
        if pid:
            log.info("[dry-run] would adopt playlist id already recorded in playlist_ids.json: %s", pid)
        else:
            log.info("[dry-run] would adopt an existing Spotify playlist with this exact name, or create it")
        log.info("[dry-run] would update config.json, playlist_ids.json, and subs[].in_masters")
        return 0

    # Only authenticate if we actually need to resolve/create a Spotify playlist.
    created = False
    if not pid:
        sp = Spotify.from_env()
        log.info("Authenticated as: %s", sp.me().get("display_name"))
        live = sp.my_playlists()
        if name in live:
            pid = live[name]
            log.info("Adopting existing Spotify playlist %r (%s)", name, pid)
        else:
            description = "Synced master. Do not edit by hand."
            pid = sp.create_playlist(name, description, public=args.public)
            created = True
            log.info("Created master playlist %r -> %s", name, pid)

    new_config = copy.deepcopy(config)
    new_ids = dict(ids)
    new_ids[name] = pid
    new_config["masters"].append(
        {
            "name": name,
            "id": pid,
            "enabled": True,
            "type": "union",
            "include": feeders,
        }
    )
    new_config["masters"].sort(key=lambda m: m["name"])
    rebuild_in_masters(new_config)

    problems = validate(new_config, new_ids)
    if problems:
        log.error("Refusing to write: resulting structure would be inconsistent:")
        for p in problems:
            log.error("  - %s", p)
        return 1

    save_json_atomic(IDS_PATH, new_ids, indent=2)
    save_json_atomic(CONFIG_PATH, new_config, indent=2)
    log.info(
        "%s master %r (%s) with %d feeder(s).",
        "Created" if created else "Added",
        name,
        pid,
        len(feeders),
    )
    log.info("Run: python sync.py")
    return 0


# -------------------------------------------------------- rename-master


def cmd_rename_master(args) -> int:
    old, new = args.old.strip(), args.new.strip()
    if not old or not new:
        log.error("Old and new names must both be non-empty.")
        return 2
    if old == new:
        log.error("Old and new name are identical.")
        return 2

    loaded = _load_repo()
    if loaded is None:
        return 2
    config, ids, desc_cache = loaded

    master = next((m for m in config["masters"] if m["name"] == old), None)
    if master is None:
        log.error("No master named %r in config.json.", old)
        return 2

    collision = _name_collision(config, new, ignore_master=old)
    if collision:
        log.error("Cannot rename to %r: that name is already used by %s.", new, collision)
        return 2
    if new in ids and ids[new] != master["id"]:
        log.error("playlist_ids.json already contains %r with a different id.", new)
        return 2

    new_config = copy.deepcopy(config)
    new_ids = dict(ids)
    new_desc = dict(desc_cache)
    new_master = next(m for m in new_config["masters"] if m["name"] == old)
    new_master["name"] = new
    new_config["masters"].sort(key=lambda m: m["name"])
    rebuild_in_masters(new_config)

    if old in new_ids:
        new_ids[new] = new_ids.pop(old)
    else:
        new_ids[new] = master["id"]

    for key_old, key_new in ((old, new), (f"master:{old}", f"master:{new}")):
        if key_old in new_desc:
            new_desc[key_new] = new_desc.pop(key_old)

    problems = validate(new_config, new_ids)
    if problems:
        log.error("Refusing to rename: resulting structure would be inconsistent:")
        for p in problems:
            log.error("  - %s", p)
        return 1

    if args.dry_run:
        affected = sorted(s["name"] for s in config["subs"] if old in s.get("in_masters", []))
        log.info("[dry-run] rename-master %r -> %r", old, new)
        log.info("[dry-run] %d sub in_masters list(s) would reflect the new name", len(affected))
        log.info("[dry-run] would update config.json, playlist_ids.json, descriptions.json")
        if not args.no_rename_on_spotify:
            log.info("[dry-run] would rename the Spotify playlist itself")
        return 0

    if not args.no_rename_on_spotify:
        sp = Spotify.from_env()
        log.info("Authenticated as: %s", sp.me().get("display_name"))
        sp.rename_playlist(master["id"], new)
        log.info("Renamed Spotify playlist %s -> %r", master["id"], new)

    save_json_atomic(IDS_PATH, new_ids, indent=2)
    save_json_atomic(CONFIG_PATH, new_config, indent=2)
    save_json_atomic(DESC_PATH, new_desc, indent=1)
    log.info("Renamed %r -> %r everywhere in the repo structure.", old, new)
    log.info("Run: python sync.py")
    return 0


# ---------------------------------------------------------- set-feeders


def cmd_set_feeders(args) -> int:
    name = args.name.strip()
    requested = [] if args.clear else list(args.sub or [])
    feeders = sorted(dict.fromkeys(s.strip() for s in requested if s.strip()))

    if not args.clear and not args.sub:
        log.error("No feeders supplied. Repeat --sub, or pass --clear intentionally.")
        return 2

    loaded = _load_repo()
    if loaded is None:
        return 2
    config, ids, _desc_cache = loaded

    master = next((m for m in config["masters"] if m["name"] == name), None)
    if master is None:
        log.error("No master named %r in config.json.", name)
        return 2
    if master.get("type", "union") != "union":
        log.error(
            "%r is a %s master. set-feeders only applies to union masters.",
            name,
            master.get("type"),
        )
        return 2

    feeder_problems = _validate_requested_subs(config, feeders)
    if feeder_problems:
        for p in feeder_problems:
            log.error(p)
        return 2

    current = sorted(master.get("include", []))
    if current == feeders:
        log.info("%r already has exactly this feeder list. Nothing to do.", name)
        return 0

    added = sorted(set(feeders) - set(current))
    removed = sorted(set(current) - set(feeders))

    new_config = copy.deepcopy(config)
    new_master = next(m for m in new_config["masters"] if m["name"] == name)
    new_master["include"] = feeders
    rebuild_in_masters(new_config)

    problems = validate(new_config, ids)
    if problems:
        log.error("Refusing to write: resulting structure would be inconsistent:")
        for p in problems:
            log.error("  - %s", p)
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    log.info("%sset-feeders %r: %d -> %d", prefix, name, len(current), len(feeders))
    if added:
        log.info("%s  add: %s", prefix, added)
    if removed:
        log.info("%s  remove: %s", prefix, removed)
    if not added and not removed:
        log.info("%s  no changes", prefix)

    if args.dry_run:
        log.info("[dry-run] would update config.json and all affected subs[].in_masters")
        return 0

    save_json_atomic(CONFIG_PATH, new_config, indent=2)
    log.info("Updated %r. Run: python sync.py", name)
    return 0


# ------------------------------------------------------------- interactive


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def _ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


def _pick_one(items: list[str], label: str) -> str | None:
    if not items:
        return None
    print(f"\n{label}:")
    for i, item in enumerate(items, 1):
        print(f"  {i:>2}. {item}")
    raw = input("Enter a number: ").strip()
    try:
        idx = int(raw)
    except ValueError:
        idx = 0
    if 1 <= idx <= len(items):
        return items[idx - 1]
    print("  Not a valid choice.")
    return None


def _parse_numbers(raw: str, count: int) -> set[int] | None:
    """Parse '1 3-5,8' into zero-based indexes. None means invalid."""
    chosen: set[int] = set()
    raw = raw.replace(",", " ")
    for token in raw.split():
        if "-" in token:
            left, sep, right = token.partition("-")
            if not sep:
                return None
            try:
                a, b = int(left), int(right)
            except ValueError:
                return None
            if a > b:
                a, b = b, a
            if a < 1 or b > count:
                return None
            chosen.update(range(a - 1, b))
        else:
            try:
                n = int(token)
            except ValueError:
                return None
            if not 1 <= n <= count:
                return None
            chosen.add(n - 1)
    return chosen


def _edit_feeders(sub_names: list[str], current: set[str] | None = None) -> list[str]:
    """Interactive toggle editor, starting with current feeders selected."""
    selected = set(current or set())
    while True:
        print("\nFeeder sub-playlists (current selection marked [x]):")
        for i, name in enumerate(sub_names, 1):
            mark = "x" if name in selected else " "
            print(f"  {i:>2}. [{mark}] {name}")
        print("\nToggle numbers/ranges (e.g. 1 4-7), 'all', 'none', or Enter when done.")
        raw = input("> ").strip().lower()
        if not raw:
            return sorted(selected)
        if raw == "all":
            selected = set(sub_names)
            continue
        if raw == "none":
            selected.clear()
            continue
        indexes = _parse_numbers(raw, len(sub_names))
        if indexes is None:
            print("  Invalid selection.")
            continue
        for idx in indexes:
            name = sub_names[idx]
            if name in selected:
                selected.remove(name)
            else:
                selected.add(name)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def interactive() -> int:
    loaded = _load_repo()
    if loaded is None:
        return 2
    config, _ids, _desc = loaded

    print("What do you want to do?")
    print("  1. Create/adopt a new union master")
    print("  2. Rename an existing master")
    print("  3. Set a union master's feeder list")
    print("  4. Validate structure")
    print("  5. Quit")
    choice = input("> ").strip()

    if choice == "1":
        name = _ask("New master name")
        if not name:
            print("No name entered, cancelling.")
            return 1
        feeders = _edit_feeders(_enabled_sub_names(config), current=set())
        public = _ask_yes_no("Make it public? (default private)", default=False)
        args = _Args(name=name, sub=feeders, public=public, dry_run=True)
        rc = cmd_add_master(args)
        if rc == 0 and _ask_yes_no("Look good — apply it for real now?", default=True):
            args.dry_run = False
            rc = cmd_add_master(args)
        return rc

    if choice == "2":
        old = _pick_one(sorted(m["name"] for m in config["masters"]), "Which master do you want to rename?")
        if not old:
            return 1
        new = _ask(f"New name for {old!r}")
        if not new:
            print("No new name entered, cancelling.")
            return 1
        already_renamed = _ask_yes_no(
            "Already renamed it in Spotify yourself? (yes = repo files only)", default=False
        )
        args = _Args(old=old, new=new, no_rename_on_spotify=already_renamed, dry_run=True)
        rc = cmd_rename_master(args)
        if rc == 0 and _ask_yes_no("Look good — apply it for real now?", default=True):
            args.dry_run = False
            rc = cmd_rename_master(args)
        return rc

    if choice == "3":
        union_masters = sorted(
            m["name"] for m in config["masters"] if m.get("type", "union") == "union"
        )
        name = _pick_one(union_masters, "Which union master do you want to edit?")
        if not name:
            return 1
        master = next(m for m in config["masters"] if m["name"] == name)
        feeders = _edit_feeders(
            _enabled_sub_names(config), current=set(master.get("include", []))
        )
        args = _Args(name=name, sub=feeders, clear=(len(feeders) == 0), dry_run=True)
        rc = cmd_set_feeders(args)
        if rc == 0 and _ask_yes_no("Look good — apply it for real now?", default=True):
            args.dry_run = False
            rc = cmd_set_feeders(args)
        return rc

    if choice == "4":
        return cmd_validate(None)

    return 0


# -------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=False)

    p_add = sub.add_parser("add-master", help="create/adopt a union master and optionally wire feeders")
    p_add.add_argument("name", help="exact master playlist name")
    p_add.add_argument(
        "--sub",
        action="append",
        default=[],
        help="feeder sub-playlist; repeat this flag once per feeder",
    )
    p_add.add_argument("--public", action="store_true", help="create as public (default: private)")
    p_add.add_argument("--dry-run", action="store_true")
    p_add.set_defaults(func=cmd_add_master)

    p_ren = sub.add_parser("rename-master", help="rename a master everywhere it is referenced")
    p_ren.add_argument("old", help="current exact master name")
    p_ren.add_argument("new", help="new exact master name")
    p_ren.add_argument(
        "--no-rename-on-spotify",
        action="store_true",
        help="update repo files only; leave Spotify playlist name as-is",
    )
    p_ren.add_argument("--dry-run", action="store_true")
    p_ren.set_defaults(func=cmd_rename_master)

    p_feed = sub.add_parser("set-feeders", help="replace a union master's complete feeder list")
    p_feed.add_argument("name", help="exact union master name")
    p_feed.add_argument(
        "--sub",
        action="append",
        default=[],
        help="feeder sub-playlist; repeat this flag once per feeder",
    )
    p_feed.add_argument(
        "--clear",
        action="store_true",
        help="intentionally set the feeder list to empty (cannot be combined with --sub)",
    )
    p_feed.add_argument("--dry-run", action="store_true")
    p_feed.set_defaults(func=cmd_set_feeders)

    p_val = sub.add_parser("validate", help="check config.json / playlist_ids.json / in_masters agree")
    p_val.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    if args.command is None:
        return interactive()
    if getattr(args, "clear", False) and getattr(args, "sub", []):
        ap.error("--clear cannot be combined with --sub")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
