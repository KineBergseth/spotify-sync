"""
One menu for the whole toolchain. Doesn't replace any script — it just runs
them for you, in a separate process each, exactly as if you'd typed the
command yourself. Every script still works standalone with its own flags;
this is purely a convenience front door for the common paths.

Usage:
    python cli.py
"""

from __future__ import annotations

import subprocess
import sys


def run(*args: str) -> int:
    print(f"\n$ python {' '.join(args)}\n")
    return subprocess.call([sys.executable, *args])


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


MENU = """
What do you want to do?

  Weekly
  1. Look at what's in INBOX right now (read-only)
  2. Generate an AI filing prompt for INBOX, then apply the recommendations
  3. Sync masters

  Monthly
  4. Export playlists for AI coherence/duplicate review
  5. Apply AI review flags (after you've reviewed the export)

  Structure
  6. Manage sub-playlists (add / rename / validate)
  7. Manage master playlists (create / rename / set feeders / validate)

  0. Quit
"""


def main() -> int:
    print(MENU)
    choice = input("> ").strip()

    if choice == "1":
        want_csv = ask_yes_no("Also write a CSV to review/inbox_export.csv?", default=False)
        return run("inbox_export.py", *(["--csv"] if want_csv else []))

    if choice == "2":
        rc = run("inbox_export.py", "--prompt")
        if rc != 0:
            return rc
        input(
            "\nPaste review/inbox_placement_prompt.md into a conversation, save the "
            "reply as spotify_inbox_placement_recommendations.csv, then press Enter..."
        )
        csv_path = ask("Recommendation CSV", "spotify_inbox_placement_recommendations.csv")
        rc = run("apply_inbox_recommendations.py", "--csv", csv_path, "--dry-run")
        if rc == 0 and ask_yes_no("Preview looked right — apply for real?", default=False):
            return run("apply_inbox_recommendations.py", "--csv", csv_path)
        return rc

    if choice == "3":
        return run("sync.py")

    if choice == "4":
        return run("export.py")

    if choice == "5":
        pattern = ask("Review flag file(s) (glob ok)", "review/review_flags_*.csv")
        dup = ask("Duplicate flags file", "review/duplicate_flags.csv")
        holding = ask("Holding playlist ID (blank = use SPOTIFY_REVIEW_HOLDING_ID)")
        args = ["review_apply.py", pattern, dup]
        if holding:
            args += ["--holding-id", holding]
        rc = run(*args)
        if rc == 0 and ask_yes_no("Preview looked right — apply for real?", default=False):
            return run(*args, "--apply")
        return rc

    if choice == "6":
        return run("manage_playlists.py")

    if choice == "7":
        return run("manage_masters.py")

    if choice in ("0", ""):
        return 0

    print("Not a valid choice.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
