#!/usr/bin/env python3
"""
tld_save_edit.py: change one value in a The Long Dark save and write the result to a new file.

Usage:
    python tld_save_edit.py stacks SAVE
    python tld_save_edit.py set-count SAVE --item GEAR_NAME --count N --out NEW_FILE [--entry N]

stacks lists the stacks in the player's inventory: entry number, item, count, condition.
set-count changes the count of one stack. Add --entry when the inventory holds more than
one stack of that item.

The save you name is only read. The result goes to NEW_FILE, which must not exist yet and
must not be named like a live save (sandbox1, challenge3, ...). Putting it in place of the
original is a separate step that you do yourself, after a backup.

Three checks stand between an edit and a written file:
    1. The original must pass the exact rebuild check (tld_save_codec.py verify).
    2. The edited save, decoded again, must differ from the original in exactly the one
       value that was asked for, and every other entry must be byte for byte unchanged.
    3. The file on disk is read back and compared with what was meant to be written.
If a check fails, nothing is left on disk.

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import tld_save_codec as codec
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number

MAX_COUNT = 9999

Difference = Tuple[str, str, str]


class EditError(ValueError):
    """The edit was refused. The message says why. Nothing was written."""


# ---------------------------------------------------------------------------
# The player's inventory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stack:
    entry: int                    # position in the save's inventory list, starting at 0
    prefab: str
    count: int
    condition: Optional[float]    # 0 to 100, or None when the save holds no usable value


def _inventory_items(save: DecodedSave) -> List[JsonValue]:
    document = save.entries.get("global")
    inventory = document.get("m_Inventory_Serialized") if isinstance(document, dict) else None
    content = inventory.value if isinstance(inventory, NestedJson) else None
    items = content.get("m_SerializedItems") if isinstance(content, dict) else None
    if not isinstance(items, list):
        raise EditError("the inventory in this save is not stored the way this tool expects")
    return items


def _prefab_of(item: JsonValue) -> Optional[str]:
    name = item.get("m_PrefabName") if isinstance(item, dict) else None
    return name if isinstance(name, str) else None


def _gear_of(item: JsonValue) -> Optional[Dict[str, JsonValue]]:
    gear = item.get("m_SerializedGear") if isinstance(item, dict) else None
    content = gear.value if isinstance(gear, NestedJson) else None
    return content if isinstance(content, dict) else None


def _stack_of(item: JsonValue) -> Optional[Tuple[Dict[str, JsonValue], int]]:
    """The stack record of an inventory entry and its count.

    None when the entry is not a stack, or its count is not a plain whole number.
    """
    gear = _gear_of(item)
    stack = gear.get("m_StackableItemSerialized") if gear is not None else None
    content = stack.value if isinstance(stack, NestedJson) else None
    if not isinstance(content, dict):
        return None
    units = content.get("m_UnitsProxy")
    if not isinstance(units, Number) or not units.literal.isdigit():
        return None
    return content, int(units.literal)


def _condition_of(item: JsonValue) -> Optional[float]:
    gear = _gear_of(item)
    value = gear.get("m_NormalizedCondition") if gear is not None else None
    if not isinstance(value, Number):
        return None
    try:
        return float(value.literal) * 100
    except ValueError:
        return None


def list_stacks(save: DecodedSave) -> List[Stack]:
    """Every stack in the player's inventory, in save order."""
    stacks: List[Stack] = []
    for entry, item in enumerate(_inventory_items(save)):
        found = _stack_of(item)
        prefab = _prefab_of(item)
        if found is None or prefab is None:
            continue
        stacks.append(Stack(entry, prefab, found[1], _condition_of(item)))
    return stacks


def _select_stack(items: List[JsonValue], prefab: str,
                  entry: Optional[int]) -> Tuple[int, Dict[str, JsonValue], int]:
    """The one stack that is meant: its entry number, its stack record and its count.

    Raises EditError unless exactly one stack is meant.
    """
    if entry is not None:
        if not 0 <= entry < len(items):
            raise EditError(f"there is no inventory entry {entry}. Entries run from 0 to {len(items) - 1}")
        actual = _prefab_of(items[entry])
        if actual != prefab:
            raise EditError(f"inventory entry {entry} is {actual}, not {prefab}. Run stacks again to see the "
                            "current entry numbers")
        found = _stack_of(items[entry])
        if found is None:
            raise EditError(f"inventory entry {entry} ({prefab}) is not a stack, so it has no count to set")
        return entry, found[0], found[1]
    names = [_prefab_of(item) for item in items]
    if prefab not in names:
        stack_names = sorted({name for name, item in zip(names, items) if name and _stack_of(item) is not None})
        close = difflib.get_close_matches(prefab, stack_names, n=3, cutoff=0.7)
        hint = f" Did you mean {' or '.join(close)}?" if close else " Run stacks to see the item names."
        raise EditError(f"there is no {prefab} in the inventory.{hint}")
    stacks: List[Tuple[int, Tuple[Dict[str, JsonValue], int]]] = []
    for position, item in enumerate(items):
        found = _stack_of(item) if names[position] == prefab else None
        if found is not None:
            stacks.append((position, found))
    if not stacks:
        raise EditError(f"{prefab} is in the inventory, but it is not a stack, so it has no count to set")
    if len(stacks) > 1:
        listing = ", ".join(f"entry {position} (count {found[1]})" for position, found in stacks)
        raise EditError(f"the inventory holds {len(stacks)} stacks of {prefab}: {listing}. "
                        "Add --entry to say which one")
    position, found = stacks[0]
    return position, found[0], found[1]


# ---------------------------------------------------------------------------
# Comparing two decoded saves
# ---------------------------------------------------------------------------

def _show(value: JsonValue) -> str:
    if isinstance(value, Number):
        return value.literal
    if isinstance(value, str):
        return json.dumps(value if len(value) <= 40 else value[:40] + "...")
    if isinstance(value, NestedJson):
        return "a nested document"
    if isinstance(value, dict):
        return f"an object with {len(value)} fields"
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return type(value).__name__


def differences(before: JsonValue, after: JsonValue, path: str = "") -> Iterator[Difference]:
    """Every place where two decoded values differ, as (path, before, after)."""
    if isinstance(before, NestedJson) and isinstance(after, NestedJson):
        yield from differences(before.value, after.value, path)
    elif isinstance(before, dict) and isinstance(after, dict):
        if list(before) != list(after):
            yield (path, "fields " + ", ".join(before), "fields " + ", ".join(after))
            return
        for key in before:
            yield from differences(before[key], after[key], f"{path} > {key}" if path else key)
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            yield (path, f"{len(before)} items", f"{len(after)} items")
            return
        for position, (left, right) in enumerate(zip(before, after)):
            yield from differences(left, right, f"{path}[{position}]")
    elif type(before) is not type(after) or before != after:
        yield (path, _show(before), _show(after))


def _count_path(entry: int) -> str:
    return (f"{codec.ENTRY_TABLE} > global > m_Inventory_Serialized > m_SerializedItems[{entry}] > "
            "m_SerializedGear > m_StackableItemSerialized > m_UnitsProxy")


def _prove_single_change(original: bytes, edited: bytes, expected: Difference) -> Tuple[int, int]:
    """Decode both saves afresh and confirm that they differ in exactly the expected value.

    Returns (entries in the save, entries that are byte for byte unchanged).
    """
    recheck = codec.verify_save(edited)
    if not recheck.identical:
        raise EditError(f"safety check failed: the edited save does not rebuild exactly ({recheck.detail})")
    found = list(differences(codec.decode_save(original).slot, codec.decode_save(edited).slot))
    if found != [expected]:
        shown = "; ".join(f"{path}: {old} -> {new}" for path, old, new in found[:5]) or "no difference at all"
        raise EditError("safety check failed: the edit did not change exactly the one value asked for. "
                        f"Found: {shown}")
    table_before = json.loads(codec.lzf_decompress(original))[codec.ENTRY_TABLE]
    table_after = json.loads(codec.lzf_decompress(edited))[codec.ENTRY_TABLE]
    changed = [name for name, encoded in table_before.items() if table_after.get(name) != encoded]
    if list(table_before) != list(table_after) or changed != ["global"]:
        raise EditError("safety check failed: entries other than global changed: " + ", ".join(changed))
    return len(table_before), len(table_before) - len(changed)


# ---------------------------------------------------------------------------
# The edit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CountEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int
    old_count: int
    new_count: int
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def set_stack_count(original: bytes, prefab: str, count: int, entry: Optional[int] = None) -> CountEdit:
    """Return the save with the count of one inventory stack changed. Reads and writes no file.

    Raises EditError when the save fails the exact rebuild check, when the stack cannot be
    picked out unambiguously, or when the result differs from the original in anything but
    that one count.
    """
    if not 1 <= count <= MAX_COUNT:
        raise EditError(f"the count must be a whole number from 1 to {MAX_COUNT}, not {count}")
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    chosen, stack, old_count = _select_stack(_inventory_items(save), prefab, entry)
    if old_count == count:
        raise EditError(f"inventory entry {chosen} ({prefab}) already has a count of {count}. "
                        "Nothing to change")
    stack["m_UnitsProxy"] = Number(str(count))
    edited = codec.encode_save(save)
    expected = (_count_path(chosen), str(old_count), str(count))
    entries, unchanged = _prove_single_change(original, edited, expected)
    return CountEdit(edited, prefab, chosen, old_count, count, entries, unchanged)


# ---------------------------------------------------------------------------
# Writing the new file
# ---------------------------------------------------------------------------

def check_new_file_path(path: Path) -> None:
    """Refuse an output path that exists, has no folder, or is named like a live save."""
    if codec.SAVE_NAME.fullmatch(path.name):
        raise EditError(f"{path.name} is named like a live save. Choose another name, for example "
                        f"{path.name}.edited, and put it in place yourself after a backup")
    if path.exists():
        raise EditError(f"{path} already exists. This tool never overwrites a file, so choose a new name")
    if not path.parent.is_dir():
        raise EditError(f"the folder {path.parent} does not exist")


def write_new_file(path: Path, data: bytes) -> None:
    """Create a file that does not exist yet, then read it back. Never overwrites anything."""
    check_new_file_path(path)
    try:
        handle = open(path, "xb")
    except FileExistsError:
        raise EditError(f"{path} already exists. This tool never overwrites a file, "
                        "so choose a new name") from None
    except OSError as error:
        raise EditError(f"could not create {path}: {error}") from error
    # From here on the file is one this call created, so removing it after a failure loses nothing.
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        written = path.read_bytes()
    except OSError as error:
        _remove_quietly(path)
        raise EditError(f"could not write {path}: {error}") from error
    if written != data:
        _remove_quietly(path)
        raise EditError(f"{path} did not read back as written, so it was removed")


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _read_save(path: Path) -> Optional[bytes]:
    if not path.is_file():
        print(f"Not found: {path}", file=sys.stderr)
        return None
    try:
        return path.read_bytes()
    except OSError as error:
        print(f"Could not read {path}: {error}", file=sys.stderr)
        return None


def _run_stacks(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        save = codec.decode_save(data)
        stacks = list_stacks(save)
        total = len(_inventory_items(save))
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max([len(stack.prefab) for stack in stacks] + [4])
    print(f"{'entry':>5}  {'item':<{width}}  {'count':>5}  condition")
    for stack in stacks:
        condition = "" if stack.condition is None else f"{stack.condition:.1f}%"
        print(f"{stack.entry:>5}  {stack.prefab:<{width}}  {stack.count:>5}  {condition:>9}")
    print(f"{len(stacks)} stacks among {total} inventory items in {path.name}.")
    return 0


def _run_set_count(path: Path, prefab: str, count: int, entry: Optional[int], out: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = set_stack_count(data, prefab, count, entry)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Item:     {edit.prefab}, inventory entry {edit.entry}")
    print(f"Change:   count {edit.old_count} -> {edit.new_count}")
    print("Checked:  the edited save differs from the original in exactly this one value")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change one value in a TLD save and write the result to a new file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tld_save_edit.py stacks sandbox15
  python tld_save_edit.py set-count sandbox15 --item GEAR_WoodMatches --count 40 --out sandbox15.edited

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
        """,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    stacks = commands.add_parser("stacks", help="list the stacks in the player's inventory")
    stacks.add_argument("save", type=Path, metavar="SAVE", help="the save file to read")
    set_count = commands.add_parser("set-count", help="change the count of one inventory stack")
    set_count.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    set_count.add_argument("--item", required=True, metavar="GEAR_NAME",
                           help="the item, as stacks prints it, for example GEAR_WoodMatches")
    set_count.add_argument("--count", required=True, type=int, metavar="N",
                           help=f"the new count, from 1 to {MAX_COUNT}")
    set_count.add_argument("--entry", type=int, metavar="N",
                           help="the entry number from stacks. Needed when there are several "
                                "stacks of the item")
    set_count.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                           help="the file to create. It must not exist yet")
    args = parser.parse_args(argv)
    if args.command == "stacks":
        return _run_stacks(args.save)
    return _run_set_count(args.save, args.item, args.count, args.entry, args.out)


if __name__ == "__main__":
    sys.exit(main())
