#!/usr/bin/env python3
r"""
tld_save_apply.py: put an edited save in place of the live one after a checked backup, or
put a backup back.

Usage:
    python tld_save_apply.py apply SAVE --from EDITED_FILE [--backup-dir FOLDER] [--dry-run]
    python tld_save_apply.py restore SAVE --from BACKUP_FILE [--backup-dir FOLDER] [--dry-run]
    python tld_save_apply.py backups SAVE [--backup-dir FOLDER]

SAVE is the live save file in the game's save folder, for example
...\Hinterland\TheLongDark\Survival\sandbox15. EDITED_FILE is a file that tld_save_edit.py
wrote. This is the only tool in this folder that changes a save file.

apply refuses unless all of this holds:
    1. The game is not running.
    2. SAVE and EDITED_FILE both pass the exact rebuild check (tld_save_codec.py verify).
    3. EDITED_FILE was made from SAVE as it is now: the same save, not written by the game
       since. Otherwise putting it in place would undo what the game saved later.
Then it works in this order and stops at the first step that fails:
    4. Copy SAVE into the backup folder under a new name, and read the copy back.
    5. Put the edited content in place of SAVE in one step, and read SAVE back.

restore puts a backup back the same way. Whatever SAVE holds at that moment is backed up
first, so a restore can be undone too. backups lists the backups of one save, newest first.

Backups go to FOLDER. Without --backup-dir that is TLD-save-backups in your home folder.
It must be outside the game's save folder, because Steam Cloud syncs everything in there.
--dry-run runs every check, prints what would happen, and changes nothing.

Exit codes: 0 done, 1 refused or stopped (the reason is printed), 2 a file was not found
or an option was wrong.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import tld_save_codec as codec
import tld_save_edit as edit
from tld_save_codec import DecodedSave
from tld_save_edit import Difference

GAME_PROCESS = "tld"                         # the game runs as tld.exe on Windows
DEFAULT_BACKUP_FOLDER = "TLD-save-backups"   # inside the home folder
BACKUP_MARK = ".backup-"                     # a backup of sandbox15 is sandbox15.backup-20261124-051500
SHOWN_CHANGES = 10
SWAP_ATTEMPTS = 5                            # see _swap_in
SWAP_PAUSE = 0.2                             # seconds between attempts


class ApplyError(ValueError):
    """The request was refused or stopped. The message says why and what state the files are in."""


# ---------------------------------------------------------------------------
# Is the game running?
# ---------------------------------------------------------------------------

def running_process_names() -> List[str]:
    """The names of all running programs, as the system lists them."""
    windows = sys.platform == "win32"
    command = ["tasklist", "/FO", "CSV", "/NH"] if windows else ["ps", "-A", "-o", "comm="]
    try:
        listing = subprocess.run(command, capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise ApplyError("could not check whether the game is running: "
                         f"{command[0]} failed ({error})") from error
    text = listing.stdout.decode("utf-8", errors="replace")
    if windows:
        names = [row[0] for row in csv.reader(text.splitlines()) if row]
    else:
        names = [line.strip() for line in text.splitlines() if line.strip()]
    if not names:
        raise ApplyError(f"could not check whether the game is running: {command[0]} listed no programs")
    return names


def game_is_running(names: Sequence[str]) -> bool:
    """True when one of the program names is the game: tld.exe, or tld with any other ending."""
    for name in names:
        base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base.split(".", 1)[0] == GAME_PROCESS:
            return True
    return False


def _require_game_closed() -> None:
    if game_is_running(running_process_names()):
        raise ApplyError("the game is running. Close it, then run this again")


# ---------------------------------------------------------------------------
# Reading and checking the files
# ---------------------------------------------------------------------------

def _read_file(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ApplyError(f"could not read {what} {path}: {error}") from error


def _decode_exact(data: bytes, name: str) -> DecodedSave:
    """Decode a save, once it is confirmed to pass the exact rebuild check."""
    check = codec.verify_save(data)
    if not check.identical:
        raise ApplyError(f"{name} does not pass the exact rebuild check: {check.detail}")
    return codec.decode_save(data)


def _saved_at(save: DecodedSave) -> str:
    """When the game last saved, as the save itself records it."""
    value = save.slot.get("m_Timestamp")
    return value if isinstance(value, str) else "an unknown time"


def _header(save: DecodedSave) -> List[Tuple[str, str]]:
    """Every field of a save outside its entry table, in order, as written."""
    return [(name, codec.render_json(value)) for name, value in save.slot.items() if name != codec.ENTRY_TABLE]


def _require_save_name(live: Path) -> None:
    if not codec.SAVE_NAME.fullmatch(live.name):
        raise ApplyError(f"{live.name} is not named like a save file (sandbox1, challenge3, ...), "
                         "so it is not replaced")


def _require_backup_folder_outside(backup_dir: Path, live: Path) -> None:
    folder = backup_dir.resolve()
    save_folder = live.resolve().parent
    if folder == save_folder or save_folder in folder.parents:
        raise ApplyError(f"the backup folder {backup_dir} is inside the game's save folder. Steam Cloud syncs "
                         "everything in there, so choose a backup folder outside it")


def _require_copy_of(source: DecodedSave, source_name: str, live: Path) -> None:
    """A file is only put in place of the save it came from."""
    name = source.slot.get("m_InternalName")
    if name != live.name:
        shown = name if isinstance(name, str) else "a save with no name"
        raise ApplyError(f"{source_name} is a copy of {shown}, not of {live.name}. A file is only put in "
                         "place of the save it came from")


def _require_same_game_save(current: DecodedSave, edited: DecodedSave, live: Path, edited_name: str) -> None:
    """The edited file must come from the live save as it is now.

    The game stamps the time on a save whenever it writes it, and tld_save_edit.py changes
    nothing outside the entry table. So equal fields outside the entry table mean that the
    game has not written the live save since the edit was made.
    """
    before, after = _header(current), _header(edited)
    if before == after:
        return
    if _saved_at(current) != _saved_at(edited):
        raise ApplyError(f"the game last saved {live.name} at {_saved_at(current)}, but {edited_name} was "
                         f"made from the save of {_saved_at(edited)}. Putting it in place would undo "
                         "everything the game saved since. Make the edit again from the current save")
    changed = sorted({name for name, _ in set(before) ^ set(after)})
    raise ApplyError(f"{edited_name} was not made from {live.name} as it is now. These fields differ: "
                     + (", ".join(changed) or "the order of the fields"))


# ---------------------------------------------------------------------------
# Working out what to do. Nothing in this section changes a file.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """What apply or restore is about to do."""
    live: Path
    source: Path                     # the edited file, or the backup that is put back
    new_data: bytes                  # what the live save will hold afterward
    new_saved_at: str                # the game's own time stamp in that content
    old_data: Optional[bytes]        # what the live save holds now. None when it does not exist
    old_saved_at: Optional[str]      # None when the live save does not exist or cannot be read as a save
    backup: Optional[Path]           # where old_data is copied first. None when there is nothing to copy
    changes: Tuple[Difference, ...]  # apply: every value that differs. Empty for restore


def _backup_path(backup_dir: Path, live: Path, now: Optional[datetime]) -> Path:
    """A name for a new backup: the save's name, then the date and time.

    When that name is taken, which takes two backups within one second, a counter is added:
    sandbox15.backup-20261124-051500, then sandbox15.backup-20261124-051500-2.
    """
    stamp = (now if now is not None else datetime.now()).strftime("%Y%m%d-%H%M%S")
    first = backup_dir / f"{live.name}{BACKUP_MARK}{stamp}"
    candidate, number = first, 1
    while candidate.exists():
        number += 1
        candidate = first.with_name(f"{first.name}-{number}")
    return candidate


def plan_apply(live: Path, edited: Path, backup_dir: Path, now: Optional[datetime] = None) -> Plan:
    """Run every check for putting an edited file in place of the live save.

    Raises ApplyError when a check fails. Changes nothing.
    """
    _require_game_closed()
    _require_save_name(live)
    _require_backup_folder_outside(backup_dir, live)
    if edited.resolve() == live.resolve():
        raise ApplyError(f"{edited} is the live save itself. Name the edited file that tld_save_edit.py wrote")
    old_data = _read_file(live, "the live save")
    new_data = _read_file(edited, "the edited file")
    current = _decode_exact(old_data, live.name)
    proposed = _decode_exact(new_data, edited.name)
    _require_copy_of(proposed, edited.name, live)
    _require_same_game_save(current, proposed, live, edited.name)
    changes = tuple(edit.differences(current.slot, proposed.slot))
    if not changes:
        raise ApplyError(f"{edited.name} holds exactly what {live.name} already holds. Nothing to put in place")
    return Plan(live, edited, new_data, _saved_at(proposed), old_data, _saved_at(current),
                _backup_path(backup_dir, live, now), changes)


def plan_restore(live: Path, backup: Path, backup_dir: Path, now: Optional[datetime] = None) -> Plan:
    """Run every check for putting a backup back in place of the live save.

    The live save may be missing or damaged: that is when a backup is needed most. Raises
    ApplyError when a check fails. Changes nothing.
    """
    _require_game_closed()
    _require_save_name(live)
    _require_backup_folder_outside(backup_dir, live)
    if not live.parent.is_dir():
        raise ApplyError(f"the save folder {live.parent} does not exist")
    if backup.resolve() == live.resolve():
        raise ApplyError(f"{backup} is the live save itself. Name the backup to put back")
    new_data = _read_file(backup, "the backup")
    kept = _decode_exact(new_data, backup.name)
    _require_copy_of(kept, backup.name, live)
    if not live.exists():
        return Plan(live, backup, new_data, _saved_at(kept), None, None, None, ())
    old_data = _read_file(live, "the live save")
    if old_data == new_data:
        raise ApplyError(f"{live.name} already holds exactly what {backup.name} holds. Nothing to put back")
    try:
        old_saved_at: Optional[str] = _saved_at(codec.decode_save(old_data))
    except codec.SaveFormatError:
        old_saved_at = None
    return Plan(live, backup, new_data, _saved_at(kept), old_data, old_saved_at,
                _backup_path(backup_dir, live, now), ())


# ---------------------------------------------------------------------------
# Changing the live save. carry_out is the only way in.
# ---------------------------------------------------------------------------

def carry_out(plan: Plan) -> None:
    """Back up what the live save holds, then put the new content in its place.

    Stops at the first step that fails. The ApplyError says what state the files are in.
    """
    if plan.old_data is None or plan.backup is None:
        if plan.live.exists():
            raise ApplyError(f"{plan.live.name} appeared while this was running, so it was left alone")
    else:
        _write_backup(plan.backup, plan.old_data)
        if _read_file(plan.live, "the live save") != plan.old_data:
            raise ApplyError(f"{plan.live.name} changed while this was running, so it was left alone. "
                             f"The backup {plan.backup} holds what it held a moment ago")
    _replace(plan.live, plan.new_data, plan.backup)


def _write_backup(path: Path, data: bytes) -> None:
    """Create the backup as a new file and read it back. Never overwrites anything."""
    if path.exists():
        raise ApplyError(f"a file named {path.name} appeared in {path.parent} while this was running. "
                         "Run this again. Nothing was changed")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        edit.write_new_file(path, data)
    except OSError as error:
        raise ApplyError(f"could not create the backup folder {path.parent}: {error}. "
                         "Nothing was changed") from error
    except edit.EditError as error:
        raise ApplyError(f"the backup was not made, so nothing was changed: {error}") from error


def _replace(live: Path, data: bytes, backup: Optional[Path]) -> None:
    """Put data in place of the live save in one step, then read the save back."""
    kept = f" The backup is {backup}" if backup is not None else ""
    before = "It holds what it held before." if live.exists() else "It still does not exist."
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(dir=live.parent, prefix=live.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if live.exists():
            shutil.copymode(live, temporary)
        _swap_in(temporary, live)
    except OSError as error:
        if temporary is not None:
            _remove_quietly(temporary)
        raise ApplyError(f"could not replace {live.name}: {error}. {before}{kept}") from error
    if _read_file(live, "the live save") != data:
        undo = f" To put the backup back: {_restore_command(live, backup)}" if backup is not None else ""
        raise ApplyError(f"{live.name} was replaced, but it did not read back as written.{undo}")


def _swap_in(temporary: Path, live: Path) -> None:
    """Move the finished file over the live save in one step.

    Windows refuses the move while another program has the new file open, and a virus
    scanner opens every new file for a moment. So a refusal is tried again a few times.
    """
    for attempt in range(1, SWAP_ATTEMPTS + 1):
        try:
            os.replace(temporary, live)
            return
        except PermissionError:
            if attempt == SWAP_ATTEMPTS:
                raise
            time.sleep(SWAP_PAUSE)


def _remove_quietly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)   # Windows does not delete a read-only file
        path.unlink()
    except OSError:
        pass


def default_backup_dir() -> Path:
    return Path.home() / DEFAULT_BACKUP_FOLDER


def _restore_command(live: Path, backup: Path) -> str:
    """The command that puts a backup back. It keeps using the folder that the backup is in."""
    command = f'python tld_save_apply.py restore "{live}" --from "{backup}"'
    try:
        usual = backup.parent == default_backup_dir()
    except RuntimeError:
        usual = False
    return command if usual else f'{command} --backup-dir "{backup.parent}"'


# ---------------------------------------------------------------------------
# Listing backups
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Backup:
    path: Path
    size: int
    saved_at: Optional[str]   # the game's own time stamp in the file. None when it cannot be read


def _saved_at_in_file(path: Path) -> Optional[str]:
    try:
        slot = json.loads(codec.lzf_decompress(path.read_bytes()))
    except (OSError, ValueError):
        return None
    value = slot.get("m_Timestamp") if isinstance(slot, dict) else None
    return value if isinstance(value, str) else None


def _backup_order(ending: str) -> Tuple[str, int]:
    """Sort key for the part of a backup name after the mark: 20261124-051500 or 20261124-051500-2."""
    parts = ending.split("-")
    if len(parts) == 3 and parts[2].isdigit():
        return "-".join(parts[:2]), int(parts[2])
    return ending, 1


def list_backups(save_name: str, backup_dir: Path) -> List[Backup]:
    """The backups of one save in a folder, newest first. The time in the name gives the order."""
    if not backup_dir.is_dir():
        return []
    prefix = save_name + BACKUP_MARK
    found = [path for path in backup_dir.iterdir() if path.is_file() and path.name.startswith(prefix)]
    found.sort(key=lambda path: _backup_order(path.name[len(prefix):]), reverse=True)
    backups: List[Backup] = []
    for path in found:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        backups.append(Backup(path, size, _saved_at_in_file(path)))
    return backups


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _print_plan(plan: Plan, command: str) -> None:
    if plan.old_data is None:
        print(f"Save:       {plan.live.name} does not exist now")
    else:
        when = (f"last saved by the game at {plan.old_saved_at}" if plan.old_saved_at is not None
                else "not readable as a save")
        print(f"Save:       {plan.live.name}, {len(plan.old_data):,} bytes, {when}")
    if command == "apply":
        print(f"Edited:     {plan.source}, {len(plan.new_data):,} bytes")
        count = len(plan.changes)
        print(f"Changes:    {count} value{'' if count == 1 else 's'} differ{'s' if count == 1 else ''}")
        for path, old, new in plan.changes[:SHOWN_CHANGES]:
            print(f"              {path}: {old} -> {new}")
        if count > SHOWN_CHANGES:
            print(f"              and {count - SHOWN_CHANGES} more")
        print("Checked:    the game is not running")
        print("Checked:    both files pass the exact rebuild check")
        print("Checked:    the edited file was made from the save as it is now")
    else:
        print(f"Restoring:  {plan.source}, {len(plan.new_data):,} bytes, the save as of {plan.new_saved_at}")
        print("Checked:    the game is not running")
        print(f"Checked:    that file passes the exact rebuild check and is a copy of {plan.live.name}")


def _run_change(command: str, live: Path, source: Path, backup_dir: Path, dry_run: bool) -> int:
    if command == "apply" and not live.is_file():
        print(f"Not found: {live}", file=sys.stderr)
        return 2
    if not source.is_file():
        print(f"Not found: {source}", file=sys.stderr)
        return 2
    try:
        plan = (plan_apply(live, source, backup_dir) if command == "apply"
                else plan_restore(live, source, backup_dir))
    except (ApplyError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was changed.", file=sys.stderr)
        return 1
    _print_plan(plan, command)
    if dry_run:
        if plan.backup is None:
            would = "created from the backup"
        else:
            would = f"copied to {plan.backup} and then replaced"
        print(f"Dry run:    nothing was changed. Without --dry-run, {live.name} would be {would}.")
        return 0
    try:
        carry_out(plan)
    except ApplyError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    if plan.backup is not None:
        print(f"Backup:     {plan.backup}, read back and identical")
    content = "the edited save" if command == "apply" else "the restored save"
    print(f"Replaced:   {live.name} now holds {content}, read back and identical")
    if plan.backup is not None:
        print(f"To undo:    {_restore_command(live, plan.backup)}")
    return 0


def _run_backups(live: Path, backup_dir: Path) -> int:
    if not codec.SAVE_NAME.fullmatch(live.name):
        print(f"Refused: {live.name} is not named like a save file (sandbox1, challenge3, ...)",
              file=sys.stderr)
        return 1
    found = list_backups(live.name, backup_dir)
    if not found:
        print(f"No backups of {live.name} in {backup_dir}.")
        return 0
    width = max(len(backup.path.name) for backup in found)
    print(f"{'backup':<{width}}  {'bytes':>11}  the save as of")
    for backup in found:
        when = backup.saved_at if backup.saved_at is not None else "not readable as a save"
        print(f"{backup.path.name:<{width}}  {backup.size:>11,}  {when}")
    print(f"{len(found)} backup{'' if len(found) == 1 else 's'} of {live.name} in {backup_dir}, newest first.")
    print(f"To put one back: {_restore_command(live, backup_dir / 'BACKUP_NAME')}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Put an edited TLD save in place after a checked backup, or put a backup back.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tld_save_apply.py apply sandbox15 --from sandbox15.edited
  python tld_save_apply.py backups sandbox15
  python tld_save_apply.py restore sandbox15 --from sandbox15.backup-20261124-051500

Exit codes: 0 done, 1 refused or stopped (the reason is printed), 2 a file was not found
or an option was wrong.
        """,
    )
    usual = f"Default: {DEFAULT_BACKUP_FOLDER} in your home folder"
    folder_help = f"where backups go. It must be outside the game's save folder. {usual}"
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text, metavar, source_help in (
            ("apply", "put an edited file in place of the live save, after a backup", "EDITED_FILE",
             "the file that tld_save_edit.py wrote"),
            ("restore", "put a backup back in place of the live save", "BACKUP_FILE",
             "the backup to put back, as backups lists it")):
        change = commands.add_parser(name, help=text)
        change.add_argument("save", type=Path, metavar="SAVE", help="the live save file")
        change.add_argument("--from", dest="source", required=True, type=Path, metavar=metavar,
                            help=source_help)
        change.add_argument("--backup-dir", type=Path, metavar="FOLDER", help=folder_help)
        change.add_argument("--dry-run", action="store_true",
                            help="run every check, print what would happen, and change nothing")
    listing = commands.add_parser("backups", help="list the backups of one save, newest first")
    listing.add_argument("save", type=Path, metavar="SAVE", help="the live save file, or just its name")
    listing.add_argument("--backup-dir", type=Path, metavar="FOLDER", help=f"where the backups are. {usual}")
    args = parser.parse_args(argv)
    backup_dir = args.backup_dir
    if backup_dir is None:
        try:
            backup_dir = default_backup_dir()
        except RuntimeError:
            print("Could not find your home folder. Say where backups go with --backup-dir.", file=sys.stderr)
            return 2
    if args.command == "backups":
        return _run_backups(args.save, backup_dir)
    return _run_change(args.command, args.save, args.source, backup_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
