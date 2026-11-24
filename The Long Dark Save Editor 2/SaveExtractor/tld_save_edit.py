#!/usr/bin/env python3
"""
tld_save_edit.py: change one value in a The Long Dark save and write the result to a new file.

Usage:
    python tld_save_edit.py items SAVE
    python tld_save_edit.py stacks SAVE
    python tld_save_edit.py liquids SAVE
    python tld_save_edit.py foods SAVE
    python tld_save_edit.py set-count SAVE --item GEAR_NAME --count N --out NEW_FILE [--entry N]
    python tld_save_edit.py set-condition SAVE --item GEAR_NAME --condition PERCENT --out NEW_FILE [--entry N]
    python tld_save_edit.py set-liters SAVE --item GEAR_NAME --liters LITERS --out NEW_FILE [--entry N]
    python tld_save_edit.py set-calories SAVE --item GEAR_NAME --calories CALORIES --out NEW_FILE [--entry N]

items lists everything in the player's inventory: entry number, item, count, condition.
stacks lists only the stacks, liquids only the items that hold liquid, and foods only the
items with calories. set-count changes the count of one stack. set-condition changes the
condition of one item, or of one whole stack. set-liters changes the liquid one item
holds: the water you carry, a bottle or can, or the fuel in a lamp. set-calories changes
the calories left in one food item. Add --entry when the inventory holds more than one of
that item.

set-liters and set-calories have not been tested in the game yet. Their own output says so.

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
import re
import struct
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

import tld_save_codec as codec
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number

MAX_COUNT = 9999
MIN_CONDITION = 1.0      # percent
MAX_CONDITION = 100.0    # percent
MAX_LITERS = 999         # far above anything the game holds. The limit only catches a slip of the hand
MIN_CALORIES = 1.0       # an item with nothing left is one the game removes
MAX_CALORIES = 9999.0    # the most in 14 real saves is 1750. The limit only catches a slip of the hand

# Kinds of edit that no one has loaded in the game yet. Their output says so.
NOT_TESTED_IN_GAME = frozenset({"set-liters", "set-calories"})

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


@dataclass(frozen=True)
class Item:
    entry: int                    # position in the save's inventory list, starting at 0
    prefab: str
    count: Optional[int]          # None when the item is not a stack
    condition: Optional[float]    # 0 to 100, or None when the save holds no usable value


def list_items(save: DecodedSave) -> List[Item]:
    """Everything in the player's inventory, in save order."""
    found: List[Item] = []
    for entry, item in enumerate(_inventory_items(save)):
        prefab = _prefab_of(item)
        if prefab is None:
            continue
        stack = _stack_of(item)
        found.append(Item(entry, prefab, None if stack is None else stack[1], _condition_of(item)))
    return found


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


# The game stores an amount of liquid as a whole number of billionths of a liter.
_BILLION = 1_000_000_000

# Where an inventory item keeps the liquid it holds: (the record, the amount in that record).
_LIQUID_FIELDS = (
    ("m_WaterSupplySerialized", "m_VolumeProxy"),                   # the water you carry
    ("m_LiquidItemSerialized", "m_LiquidLitersProxy"),              # bottles, cans and jerry cans
    ("m_KeroseneLampItemSerialized", "m_CurrentFuelLitersProxy"),   # the fuel in a lamp
)


@dataclass(frozen=True)
class Liquid:
    entry: int                    # position in the save's inventory list, starting at 0
    prefab: str
    liters: Optional[Decimal]     # None when the game has not set an amount: it stores a negative number then


@dataclass(frozen=True)
class _LiquidRecord:
    content: Dict[str, JsonValue]    # the record that holds the amount
    record_field: str
    amount_field: str
    literal: str                     # the amount as stored
    stored: Decimal                  # the same, as a number: billionths of a liter


def _liquid_of(item: JsonValue) -> Optional[_LiquidRecord]:
    """Where an inventory entry keeps its liquid, and how much the save says it holds.

    None when the entry has no such record, the record has no amount, or the amount is not
    a number.
    """
    gear = _gear_of(item)
    for record_field, amount_field in _LIQUID_FIELDS:
        record = gear.get(record_field) if gear is not None else None
        content = record.value if isinstance(record, NestedJson) else None
        amount = content.get(amount_field) if isinstance(content, dict) else None
        if not isinstance(content, dict) or not isinstance(amount, Number):
            continue
        try:
            stored = Decimal(amount.literal)
        except InvalidOperation:
            continue
        if stored.is_finite():
            return _LiquidRecord(content, record_field, amount_field, amount.literal, stored)
    return None


def _liters_of(stored: Decimal) -> Optional[Decimal]:
    """Liters for a stored amount, or None for the negative number that means "not set"."""
    return None if stored < 0 else stored / _BILLION


def _liters_text(liters: Optional[Decimal]) -> str:
    if liters is None:
        return "not set"
    return format(liters.normalize(), "f") if liters else "0"


def list_liquids(save: DecodedSave) -> List[Liquid]:
    """Every inventory entry that holds liquid, in save order."""
    liquids: List[Liquid] = []
    for entry, item in enumerate(_inventory_items(save)):
        found = _liquid_of(item)
        prefab = _prefab_of(item)
        if found is None or prefab is None:
            continue
        liquids.append(Liquid(entry, prefab, _liters_of(found.stored)))
    return liquids


# Where a food item keeps its calories: the record, what is left, and the figure for an untouched one.
_FOOD_RECORD = "m_FoodItemSerialized"
_CALORIES_LEFT = "m_CaloriesRemainingProxy"
_CALORIES_FULL = "m_CaloriesTotal"


@dataclass(frozen=True)
class Food:
    entry: int                    # position in the save's inventory list, starting at 0
    prefab: str
    count: Optional[int]          # None when the item is not a stack
    calories: Decimal             # what is left. For a stack, the save keeps the figure for one unit
    full: Optional[Decimal]       # the save's figure for an untouched one, or None when it holds none


@dataclass(frozen=True)
class _FoodRecord:
    content: Dict[str, JsonValue]    # the record that holds the calories
    literal: str                     # the calories left, as stored
    calories: Decimal                # the same, as a number
    full: Optional[Decimal]


def _finite(value: JsonValue) -> Optional[Decimal]:
    """A stored number. None for anything else, and for a number that is not finite."""
    if not isinstance(value, Number):
        return None
    try:
        number = Decimal(value.literal)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _food_of(item: JsonValue) -> Optional[_FoodRecord]:
    """The food record of an inventory entry, and the calories the save says are left.

    None when the entry has no such record, the record has no calories, or they are not a
    number.
    """
    gear = _gear_of(item)
    record = gear.get(_FOOD_RECORD) if gear is not None else None
    content = record.value if isinstance(record, NestedJson) else None
    left = content.get(_CALORIES_LEFT) if isinstance(content, dict) else None
    calories = _finite(left)
    if not isinstance(content, dict) or not isinstance(left, Number) or calories is None:
        return None
    return _FoodRecord(content, left.literal, calories, _finite(content.get(_CALORIES_FULL)))


def _calories_text(calories: Decimal) -> str:
    return format(calories, "f")


def list_foods(save: DecodedSave) -> List[Food]:
    """Every inventory entry with calories, in save order."""
    foods: List[Food] = []
    for entry, item in enumerate(_inventory_items(save)):
        found = _food_of(item)
        prefab = _prefab_of(item)
        if found is None or prefab is None:
            continue
        stack = _stack_of(item)
        foods.append(Food(entry, prefab, None if stack is None else stack[1], found.calories, found.full))
    return foods


def _item_at(items: List[JsonValue], prefab: str, entry: int, listing: str) -> JsonValue:
    """The inventory entry with this number, once it is confirmed to be the named item."""
    if not 0 <= entry < len(items):
        raise EditError(f"there is no inventory entry {entry}. Entries run from 0 to {len(items) - 1}")
    actual = _prefab_of(items[entry])
    if actual != prefab:
        raise EditError(f"inventory entry {entry} is {actual}, not {prefab}. Run {listing} again to see the "
                        "current entry numbers")
    return items[entry]


def _no_such_item(prefab: str, names: Sequence[Optional[str]], listing: str) -> EditError:
    close = difflib.get_close_matches(prefab, sorted({name for name in names if name}), n=3, cutoff=0.7)
    hint = f" Did you mean {' or '.join(close)}?" if close else f" Run {listing} to see the item names."
    return EditError(f"there is no {prefab} in the inventory.{hint}")


def _select_stack(items: List[JsonValue], prefab: str,
                  entry: Optional[int]) -> Tuple[int, Dict[str, JsonValue], int]:
    """The one stack that is meant: its entry number, its stack record and its count.

    Raises EditError unless exactly one stack is meant.
    """
    if entry is not None:
        found = _stack_of(_item_at(items, prefab, entry, "stacks"))
        if found is None:
            raise EditError(f"inventory entry {entry} ({prefab}) is not a stack, so it has no count to set")
        return entry, found[0], found[1]
    names = [_prefab_of(item) for item in items]
    if prefab not in names:
        raise _no_such_item(prefab, [name for name, item in zip(names, items) if _stack_of(item) is not None],
                            "stacks")
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


def _select_item(items: List[JsonValue], prefab: str, entry: Optional[int]) -> Tuple[int, JsonValue]:
    """The one inventory entry that is meant, with its entry number.

    Raises EditError unless exactly one entry is meant.
    """
    if entry is not None:
        return entry, _item_at(items, prefab, entry, "items")
    positions = [position for position, item in enumerate(items) if _prefab_of(item) == prefab]
    if not positions:
        raise _no_such_item(prefab, [_prefab_of(item) for item in items], "items")
    if len(positions) > 1:
        def shown(position: int) -> str:
            condition = _condition_of(items[position])
            return f"entry {position}" + ("" if condition is None else f" (condition {condition:.1f}%)")
        listing = ", ".join(shown(position) for position in positions[:8])
        more = f" and {len(positions) - 8} more" if len(positions) > 8 else ""
        raise EditError(f"the inventory holds {len(positions)} entries of {prefab}: {listing}{more}. "
                        "Add --entry to say which one")
    return positions[0], items[positions[0]]


def _select_liquid(items: List[JsonValue], prefab: str, entry: Optional[int]) -> Tuple[int, _LiquidRecord]:
    """The one entry that holds liquid and is meant, with its entry number.

    Raises EditError unless exactly one such entry is meant.
    """
    nothing_to_set = "the save holds no amount of liquid for it, so there are no liters to set"
    if entry is not None:
        found = _liquid_of(_item_at(items, prefab, entry, "liquids"))
        if found is None:
            raise EditError(f"inventory entry {entry} is {prefab}, and {nothing_to_set}")
        return entry, found
    names = [_prefab_of(item) for item in items]
    if prefab not in names:
        raise _no_such_item(prefab, [name for name, item in zip(names, items) if _liquid_of(item) is not None],
                            "liquids")
    liquids: List[Tuple[int, _LiquidRecord]] = []
    for position, item in enumerate(items):
        found = _liquid_of(item) if names[position] == prefab else None
        if found is not None:
            liquids.append((position, found))
    if not liquids:
        raise EditError(f"{prefab} is in the inventory, but {nothing_to_set}")
    if len(liquids) > 1:
        listing = ", ".join(f"entry {position} ({_liters_text(_liters_of(found.stored))})"
                            for position, found in liquids)
        raise EditError(f"the inventory holds {len(liquids)} entries of {prefab} with liquid: {listing}. "
                        "Add --entry to say which one")
    return liquids[0]


def _select_food(items: List[JsonValue], prefab: str, entry: Optional[int]) -> Tuple[int, _FoodRecord]:
    """The one entry with calories that is meant, with its entry number.

    Raises EditError unless exactly one such entry is meant.
    """
    nothing_to_set = "the save holds no calories for it, so there are none to set"
    if entry is not None:
        found = _food_of(_item_at(items, prefab, entry, "foods"))
        if found is None:
            raise EditError(f"inventory entry {entry} is {prefab}, and {nothing_to_set}")
        return entry, found
    names = [_prefab_of(item) for item in items]
    if prefab not in names:
        raise _no_such_item(prefab, [name for name, item in zip(names, items) if _food_of(item) is not None],
                            "foods")
    foods: List[Tuple[int, _FoodRecord]] = []
    for position, item in enumerate(items):
        found = _food_of(item) if names[position] == prefab else None
        if found is not None:
            foods.append((position, found))
    if not foods:
        raise EditError(f"{prefab} is in the inventory, but {nothing_to_set}")
    if len(foods) > 1:
        listing = ", ".join(f"entry {position} ({_calories_text(found.calories)})"
                            for position, found in foods[:8])
        more = f" and {len(foods) - 8} more" if len(foods) > 8 else ""
        raise EditError(f"the inventory holds {len(foods)} entries of {prefab} with calories: {listing}{more}. "
                        "Add --entry to say which one")
    return foods[0]


# ---------------------------------------------------------------------------
# Condition as the game stores it
# ---------------------------------------------------------------------------

_PLAIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _as_float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def float32_literal(value: float) -> str:
    """A 32-bit float written the way the game writes it: 7 digits, or 9 when 7 do not give
    the same float back. Meant for values from 0.0001 to 9999, which need no exponent."""
    target = _as_float32(value)
    text = f"{target:.7g}"
    if _as_float32(float(text)) != target:
        text = f"{target:.9g}"
    return text if "." in text else text + ".0"


def condition_literal(percent: float) -> str:
    """The stored form of a condition given in percent. The save holds a fraction of 1."""
    return float32_literal(float(Decimal(repr(percent)) / 100))


def _editable_condition(item: JsonValue, entry: int, prefab: str) -> Tuple[Dict[str, JsonValue], str]:
    """The gear record of an inventory entry and its stored condition.

    Raises EditError when the condition is missing, in an unknown form, or marks a ruined item.
    """
    gear = _gear_of(item)
    value = gear.get("m_NormalizedCondition") if gear is not None else None
    if gear is None or not isinstance(value, Number) or not _PLAIN_DECIMAL.fullmatch(value.literal) \
            or float(value.literal) > 1:
        raise EditError(f"the condition of inventory entry {entry} ({prefab}) is not stored the way this "
                        "tool expects, so it is not changed")
    if float(value.literal) == 0 or gear.get("m_WornOut") is True:
        raise EditError(f"inventory entry {entry} ({prefab}) is ruined. The game keeps more than the condition "
                        "for a ruined item, so this tool does not change it")
    return gear, value.literal


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


def _gear_path(entry: int, field: str) -> str:
    return (f"{codec.ENTRY_TABLE} > global > m_Inventory_Serialized > m_SerializedItems[{entry}] > "
            f"m_SerializedGear > {field}")


def _count_path(entry: int) -> str:
    return _gear_path(entry, "m_StackableItemSerialized > m_UnitsProxy")


def _condition_path(entry: int) -> str:
    return _gear_path(entry, "m_NormalizedCondition")


def _liquid_path(entry: int, liquid: _LiquidRecord) -> str:
    return _gear_path(entry, f"{liquid.record_field} > {liquid.amount_field}")


def _calories_path(entry: int) -> str:
    return _gear_path(entry, f"{_FOOD_RECORD} > {_CALORIES_LEFT}")


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


@dataclass(frozen=True)
class ConditionEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int
    old_condition: str        # as stored: a fraction of 1, for example 0.856
    new_condition: str
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def set_item_condition(original: bytes, prefab: str, percent: float,
                       entry: Optional[int] = None) -> ConditionEdit:
    """Return the save with the condition of one inventory entry changed. Reads and writes no file.

    The condition is given in percent. For a stack it applies to the whole stack. Raises
    EditError under the same rules as set_stack_count, and when the item is ruined.
    """
    if not MIN_CONDITION <= percent <= MAX_CONDITION:
        raise EditError(f"the condition must be a number from {MIN_CONDITION:g} to {MAX_CONDITION:g} "
                        f"(percent), not {percent:g}")
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    chosen, item = _select_item(_inventory_items(save), prefab, entry)
    gear, old = _editable_condition(item, chosen, prefab)
    new = condition_literal(percent)
    if _as_float32(float(old)) == _as_float32(float(new)):
        raise EditError(f"inventory entry {chosen} ({prefab}) already has a condition of "
                        f"{float(old) * 100:g}%. Nothing to change")
    gear["m_NormalizedCondition"] = Number(new)
    edited = codec.encode_save(save)
    entries, unchanged = _prove_single_change(original, edited, (_condition_path(chosen), old, new))
    return ConditionEdit(edited, prefab, chosen, old, new, entries, unchanged)


@dataclass(frozen=True)
class LitersEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int
    old_liters: Optional[Decimal]    # None when the game had not set an amount
    new_liters: Decimal
    old_amount: str           # as stored: billionths of a liter, for example 1904800000
    new_amount: str
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def stored_liters(liters: Decimal) -> str:
    """The stored form of an amount given in liters: a whole number of billionths of a liter."""
    if not liters.is_finite() or not 0 <= liters <= MAX_LITERS:
        raise EditError(f"the amount must be a number of liters from 0 to {MAX_LITERS}, not {liters}")
    billionths = liters * _BILLION
    if billionths != billionths.to_integral_value():
        raise EditError(f"the game stores liters to nine decimal places, and {liters} has more")
    return str(int(billionths))


def set_item_liters(original: bytes, prefab: str, liters: Decimal, entry: Optional[int] = None) -> LitersEdit:
    """Return the save with the liquid one inventory entry holds changed. Reads and writes no file.

    The amount is given in liters. Raises EditError under the same rules as set_stack_count,
    and when the save holds no amount of liquid for the entry.
    """
    new = stored_liters(liters)
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    chosen, liquid = _select_liquid(_inventory_items(save), prefab, entry)
    if liquid.stored == Decimal(new):
        raise EditError(f"inventory entry {chosen} ({prefab}) already holds {_liters_text(liters)} liters. "
                        "Nothing to change")
    liquid.content[liquid.amount_field] = Number(new)
    edited = codec.encode_save(save)
    expected = (_liquid_path(chosen, liquid), liquid.literal, new)
    entries, unchanged = _prove_single_change(original, edited, expected)
    return LitersEdit(edited, prefab, chosen, _liters_of(liquid.stored), liters, liquid.literal, new,
                      entries, unchanged)


@dataclass(frozen=True)
class CaloriesEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int
    count: Optional[int]      # units in the stack, or None when the entry is not a stack
    old_calories: str         # as stored, for example 689.2364
    new_calories: str
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def set_item_calories(original: bytes, prefab: str, calories: float,
                      entry: Optional[int] = None) -> CaloriesEdit:
    """Return the save with the calories left in one inventory entry changed. Reads and writes no file.

    For a stack the save keeps one figure, the calories of one unit, and that is what
    changes. Raises EditError under the same rules as set_stack_count, and when the save
    holds no calories for the entry.
    """
    if not MIN_CALORIES <= calories <= MAX_CALORIES:
        raise EditError(f"the calories must be a number from {MIN_CALORIES:g} to {MAX_CALORIES:g}, "
                        f"not {calories:g}")
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    items = _inventory_items(save)
    chosen, food = _select_food(items, prefab, entry)
    new = float32_literal(calories)
    if food.calories == Decimal(new):
        raise EditError(f"inventory entry {chosen} ({prefab}) already holds {new} calories. "
                        "Nothing to change")
    stack = _stack_of(items[chosen])
    food.content[_CALORIES_LEFT] = Number(new)
    edited = codec.encode_save(save)
    entries, unchanged = _prove_single_change(original, edited, (_calories_path(chosen), food.literal, new))
    return CaloriesEdit(edited, prefab, chosen, None if stack is None else stack[1], food.literal, new,
                        entries, unchanged)


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


def _run_list(path: Path, stacks_only: bool) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        save = codec.decode_save(data)
        listed: Sequence[Union[Item, Stack]] = list_stacks(save) if stacks_only else list_items(save)
        total = len(_inventory_items(save))
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max([len(row.prefab) for row in listed] + [4])
    print(f"{'entry':>5}  {'item':<{width}}  {'count':>5}  condition")
    for row in listed:
        count = "" if row.count is None else str(row.count)
        condition = "" if row.condition is None else f"{row.condition:.1f}%"
        print(f"{row.entry:>5}  {row.prefab:<{width}}  {count:>5}  {condition:>9}".rstrip())
    if stacks_only:
        print(f"{len(listed)} stacks among {total} inventory items in {path.name}.")
    else:
        print(f"{len(listed)} inventory items in {path.name}.")
    return 0


def _run_liquids(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        save = codec.decode_save(data)
        listed = list_liquids(save)
        total = len(_inventory_items(save))
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max([len(row.prefab) for row in listed] + [4])
    print(f"{'entry':>5}  {'item':<{width}}  liters")
    for row in listed:
        print(f"{row.entry:>5}  {row.prefab:<{width}}  {_liters_text(row.liters)}")
    print(f"{len(listed)} items that hold liquid among {total} inventory items in {path.name}.")
    return 0


def _run_foods(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        save = codec.decode_save(data)
        listed = list_foods(save)
        total = len(_inventory_items(save))
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max([len(row.prefab) for row in listed] + [4])
    left = [_calories_text(row.calories) for row in listed]
    wide = max([len(text) for text in left] + [8])
    print(f"{'entry':>5}  {'item':<{width}}  {'count':>5}  {'calories':>{wide}}  full")
    for row, calories in zip(listed, left):
        count = "" if row.count is None else str(row.count)
        full = "" if row.full is None else _calories_text(row.full)
        print(f"{row.entry:>5}  {row.prefab:<{width}}  {count:>5}  {calories:>{wide}}  {full}".rstrip())
    print(f"{len(listed)} items with calories among {total} inventory items in {path.name}.")
    return 0


def _percent(stored: str) -> str:
    return f"{float(stored) * 100:.1f}%"


def _run_edit(path: Path, out: Path, command: str, prefab: str, value: Union[float, Decimal],
              entry: Optional[int]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    edit: Union[CountEdit, ConditionEdit, LitersEdit, CaloriesEdit]
    try:
        check_new_file_path(out)
        if command == "set-count":
            edit = set_stack_count(data, prefab, int(value), entry)
            change = f"count {edit.old_count} -> {edit.new_count}"
        elif command == "set-liters":
            edit = set_item_liters(data, prefab, Decimal(value), entry)
            change = (f"liters {_liters_text(edit.old_liters)} -> {_liters_text(edit.new_liters)} "
                      f"(stored as {edit.old_amount} -> {edit.new_amount})")
        elif command == "set-calories":
            edit = set_item_calories(data, prefab, float(value), entry)
            change = f"calories {edit.old_calories} -> {edit.new_calories}"
            if edit.count is not None and edit.count > 1:
                change += f" per unit, in a stack of {edit.count}"
        else:
            edit = set_item_condition(data, prefab, float(value), entry)
            change = (f"condition {_percent(edit.old_condition)} -> {_percent(edit.new_condition)} "
                      f"(stored as {edit.old_condition} -> {edit.new_condition})")
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Item:     {edit.prefab}, inventory entry {edit.entry}")
    print(f"Change:   {change}")
    print("Checked:  the edited save differs from the original in exactly this one value")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if command in NOT_TESTED_IN_GAME:
        print(f"Untested: no save changed by {command} has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _liters_argument(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of liters") from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change one value in a TLD save and write the result to a new file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tld_save_edit.py items sandbox15
  python tld_save_edit.py set-count sandbox15 --item GEAR_WoodMatches --count 40 --out sandbox15.edited
  python tld_save_edit.py set-condition sandbox15 --item GEAR_Hatchet --condition 100 --out sandbox15.edited
  python tld_save_edit.py set-liters sandbox15 --item GEAR_LampFuel --liters 0.5 --out sandbox15.edited
  python tld_save_edit.py set-calories sandbox15 --item GEAR_MRE --calories 1750 --out sandbox15.edited

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
        """,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (("items", "list everything in the player's inventory"),
                       ("stacks", "list the stacks in the player's inventory"),
                       ("liquids", "list the inventory items that hold liquid"),
                       ("foods", "list the inventory items with calories")):
        listing = commands.add_parser(name, help=text)
        listing.add_argument("save", type=Path, metavar="SAVE", help="the save file to read")
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
    set_condition = commands.add_parser("set-condition",
                                        help="change the condition of one inventory item or stack")
    set_condition.add_argument("save", type=Path, metavar="SAVE",
                               help="the save file to read. It is not changed")
    set_condition.add_argument("--item", required=True, metavar="GEAR_NAME",
                               help="the item, as items prints it, for example GEAR_Hatchet")
    set_condition.add_argument("--condition", required=True, type=float, metavar="PERCENT",
                               help=f"the new condition in percent, from {MIN_CONDITION:g} "
                                    f"to {MAX_CONDITION:g}")
    set_condition.add_argument("--entry", type=int, metavar="N",
                               help="the entry number from items. Needed when the inventory holds "
                                    "more than one of the item")
    set_condition.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                               help="the file to create. It must not exist yet")
    set_liters = commands.add_parser("set-liters", help="change the liquid one inventory item holds "
                                                        "(not tested in the game yet)")
    set_liters.add_argument("save", type=Path, metavar="SAVE",
                            help="the save file to read. It is not changed")
    set_liters.add_argument("--item", required=True, metavar="GEAR_NAME",
                            help="the item, as liquids prints it, for example GEAR_WaterSupplyPotable")
    set_liters.add_argument("--liters", required=True, type=_liters_argument, metavar="LITERS",
                            help=f"the new amount in liters, from 0 to {MAX_LITERS}")
    set_liters.add_argument("--entry", type=int, metavar="N",
                            help="the entry number from liquids. Needed when the inventory holds "
                                 "more than one of the item")
    set_liters.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                            help="the file to create. It must not exist yet")
    set_calories = commands.add_parser("set-calories", help="change the calories left in one inventory item "
                                                            "(not tested in the game yet)")
    set_calories.add_argument("save", type=Path, metavar="SAVE",
                              help="the save file to read. It is not changed")
    set_calories.add_argument("--item", required=True, metavar="GEAR_NAME",
                              help="the item, as foods prints it, for example GEAR_MRE")
    set_calories.add_argument("--calories", required=True, type=float, metavar="CALORIES",
                              help=f"the new amount, from {MIN_CALORIES:g} to {MAX_CALORIES:g}. "
                                   "For a stack, the calories of one unit")
    set_calories.add_argument("--entry", type=int, metavar="N",
                              help="the entry number from foods. Needed when the inventory holds "
                                   "more than one of the item")
    set_calories.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                              help="the file to create. It must not exist yet")
    args = parser.parse_args(argv)
    if args.command in ("items", "stacks"):
        return _run_list(args.save, stacks_only=args.command == "stacks")
    if args.command == "liquids":
        return _run_liquids(args.save)
    if args.command == "foods":
        return _run_foods(args.save)
    value: Union[float, Decimal]
    if args.command == "set-count":
        value = args.count
    elif args.command == "set-liters":
        value = args.liters
    elif args.command == "set-calories":
        value = args.calories
    else:
        value = args.condition
    return _run_edit(args.save, args.out, args.command, args.item, value, args.entry)


if __name__ == "__main__":
    sys.exit(main())
