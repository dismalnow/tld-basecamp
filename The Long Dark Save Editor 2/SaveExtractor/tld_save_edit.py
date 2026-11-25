#!/usr/bin/env python3
"""
tld_save_edit.py: change a value in a The Long Dark save and write the result to a new file.

Usage:
    python tld_save_edit.py items SAVE
    python tld_save_edit.py stacks SAVE
    python tld_save_edit.py liquids SAVE
    python tld_save_edit.py foods SAVE
    python tld_save_edit.py weapons SAVE
    python tld_save_edit.py set-count SAVE --item GEAR_NAME --count N --out NEW_FILE [--entry N]
    python tld_save_edit.py set-condition SAVE --item GEAR_NAME --condition PERCENT --out NEW_FILE [--entry N]
    python tld_save_edit.py set-liters SAVE --item GEAR_NAME --liters LITERS --out NEW_FILE [--entry N]
    python tld_save_edit.py set-calories SAVE --item GEAR_NAME --calories CALORIES --out NEW_FILE [--entry N]
    python tld_save_edit.py set-rounds SAVE --item GEAR_NAME --rounds N --out NEW_FILE [--entry N]
    python tld_save_edit.py repair-all SAVE --out NEW_FILE
    python tld_save_edit.py remove-item SAVE --item GEAR_NAME --out NEW_FILE [--entry N]
    python tld_save_edit.py copy-item SAVE --item GEAR_NAME --out NEW_FILE [--entry N]

items lists everything in the player's inventory: entry number, item, count, condition.
stacks lists only the stacks, liquids only the items that hold liquid, foods only the
items with calories, and weapons only the items that can hold rounds. set-count changes
the count of one stack. set-condition changes the condition of one item, or of one whole
stack. set-liters changes the liquid one item holds: the water you carry, a bottle or
can, or the fuel in a lamp. set-calories changes the calories left in one food item.
set-rounds changes the rounds loaded in one rifle, revolver or distress pistol. Add
--entry when the inventory holds more than one of that item. repair-all sets the
condition of every item in the inventory to 100%, except ruined items and torches and
flares that have burned. remove-item takes one item, or one whole stack, out of the
inventory. copy-item adds another one of an item the inventory holds.

set-liters, set-calories, set-rounds, repair-all, remove-item and copy-item have not been
tested in the game yet. Their own output says so.

The save you name is only read. The result goes to NEW_FILE, which must not exist yet and
must not be named like a live save (sandbox1, challenge3, ...). Putting it in place of the
original is a separate step that you do yourself, after a backup.

Three checks stand between an edit and a written file:
    1. The original must pass the exact rebuild check (tld_save_codec.py verify).
    2. The edited save, decoded again, must differ from the original in exactly what was
       asked for: one value, for repair-all one condition for each repaired item, for
       remove-item one inventory entry less, and for copy-item one more. Every other
       entry must be byte for byte unchanged.
    3. The file on disk is read back and compared with what was meant to be written.
If a check fails, nothing is left on disk.

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
"""

from __future__ import annotations

import argparse
import copy
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
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

import tld_save_codec as codec
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number

MAX_COUNT = 9999
MIN_CONDITION = 1.0      # percent
MAX_CONDITION = 100.0    # percent
MAX_LITERS = 999         # far above anything the game holds. The limit only catches a slip of the hand
MIN_CALORIES = 1.0       # an item with nothing left is one the game removes
MAX_CALORIES = 9999.0    # the most in 14 real saves is 1750. The limit only catches a slip of the hand

# Rounds a firearm holds at once. A save does not say. The rifle and the revolver are from the
# game's wiki, and real saves agree: a rifle with 10 rounds, and revolvers whose rounds and spent
# casings add up to 6. The named variants are taken to hold what the plain weapon holds, and the
# distress pistol its one shell.
ROUNDS_HELD: Dict[str, int] = {
    "GEAR_Rifle": 10, "GEAR_Rifle_Barbs": 10, "GEAR_Rifle_Curators": 10, "GEAR_Rifle_Trader": 10,
    "GEAR_Rifle_Vaughns": 10,
    "GEAR_Revolver": 6, "GEAR_RevolverFancy": 6, "GEAR_RevolverGreen": 6, "GEAR_RevolverStubNosed": 6,
    "GEAR_FlareGun": 1,
}

# Kinds of edit that no one has loaded in the game yet. Their output says so.
NOT_TESTED_IN_GAME = frozenset({"set-liters", "set-calories", "set-rounds", "repair-all", "remove-item",
                                "copy-item"})

Difference = Tuple[str, str, str]


class EditError(ValueError):
    """The edit was refused. The message says why. Nothing was written."""


class _RuinedItem(EditError):
    """The item is ruined. An edit of many items needs to tell that apart from the other refusals."""


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


# Where a weapon keeps its rounds: the record, the loaded rounds, and the spent casings still in it.
_WEAPON_RECORD = "m_WeaponItemSerialized"
_CLIP = "m_Clip"
_SPENT_CASINGS = "m_SpentCasingsInClip"

# A loaded round is one number in the save. Every loaded round in 14 real saves is stored as 100.
_NEW_ROUND = "100"


@dataclass(frozen=True)
class Weapon:
    entry: int                    # position in the save's inventory list, starting at 0
    prefab: str
    rounds: int                   # rounds loaded
    spent: int                    # spent casings still in it. A revolver keeps them until it is reloaded
    holds: Optional[int]          # rounds it holds at once, or None when this tool has no number for it


@dataclass(frozen=True)
class _WeaponRecord:
    content: Dict[str, JsonValue]    # the record that holds the rounds
    clip: List[Number]               # one number for each loaded round
    spent: int


def _whole(value: JsonValue) -> Optional[int]:
    """A stored whole number that is not negative. None for anything else."""
    return int(value.literal) if isinstance(value, Number) and value.literal.isdigit() else None


def _weapon_of(item: JsonValue) -> Optional[_WeaponRecord]:
    """The weapon record of an inventory entry, with its loaded rounds and its spent casings.

    None when the entry has no such record, the record has no list of rounds, or the rounds
    or the spent casings are not stored as whole numbers.
    """
    gear = _gear_of(item)
    record = gear.get(_WEAPON_RECORD) if gear is not None else None
    content = record.value if isinstance(record, NestedJson) else None
    clip = content.get(_CLIP) if isinstance(content, dict) else None
    if not isinstance(content, dict) or not isinstance(clip, list):
        return None
    rounds = [value for value in clip if isinstance(value, Number) and _whole(value) is not None]
    spent = _whole(content[_SPENT_CASINGS]) if _SPENT_CASINGS in content else 0
    if len(rounds) != len(clip) or spent is None:
        return None
    return _WeaponRecord(content, rounds, spent)


def _count_text(count: int, one: str) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {one}s"


def list_weapons(save: DecodedSave) -> List[Weapon]:
    """Every inventory entry that can hold rounds, in save order."""
    weapons: List[Weapon] = []
    for entry, item in enumerate(_inventory_items(save)):
        found = _weapon_of(item)
        prefab = _prefab_of(item)
        if found is None or prefab is None:
            continue
        weapons.append(Weapon(entry, prefab, len(found.clip), found.spent, ROUNDS_HELD.get(prefab)))
    return weapons


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


def _select_weapon(items: List[JsonValue], prefab: str, entry: Optional[int]) -> Tuple[int, _WeaponRecord]:
    """The one entry that can hold rounds and is meant, with its entry number.

    Raises EditError unless exactly one such entry is meant.
    """
    nothing_to_set = "the save holds no rounds for it, so there are none to set"
    if entry is not None:
        found = _weapon_of(_item_at(items, prefab, entry, "weapons"))
        if found is None:
            raise EditError(f"inventory entry {entry} is {prefab}, and {nothing_to_set}")
        return entry, found
    names = [_prefab_of(item) for item in items]
    if prefab not in names:
        raise _no_such_item(prefab, [name for name, item in zip(names, items) if _weapon_of(item) is not None],
                            "weapons")
    weapons: List[Tuple[int, _WeaponRecord]] = []
    for position, item in enumerate(items):
        found = _weapon_of(item) if names[position] == prefab else None
        if found is not None:
            weapons.append((position, found))
    if not weapons:
        raise EditError(f"{prefab} is in the inventory, but {nothing_to_set}")
    if len(weapons) > 1:
        listing = ", ".join(f"entry {position} ({_count_text(len(found.clip), 'round')})"
                            for position, found in weapons)
        raise EditError(f"the inventory holds {len(weapons)} entries of {prefab} that can hold rounds: "
                        f"{listing}. Add --entry to say which one")
    return weapons[0]


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

    Raises EditError when the condition is missing or in an unknown form, and _RuinedItem, which
    is an EditError too, for a ruined item.
    """
    gear = _gear_of(item)
    value = gear.get("m_NormalizedCondition") if gear is not None else None
    if gear is None or not isinstance(value, Number) or not _PLAIN_DECIMAL.fullmatch(value.literal) \
            or float(value.literal) > 1:
        raise EditError(f"the condition of inventory entry {entry} ({prefab}) is not stored the way this "
                        "tool expects, so it is not changed")
    if float(value.literal) == 0 or gear.get("m_WornOut") is True:
        raise _RuinedItem(f"inventory entry {entry} ({prefab}) is ruined. The game keeps more than the "
                          "condition for a ruined item, so this tool does not change it")
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


def differences(before: JsonValue, after: JsonValue, path: str = "",
                matched: Optional[Tuple[str, Sequence[Tuple[int, int]]]] = None) -> Iterator[Difference]:
    """Every place where two decoded values differ, as (path, before, after).

    A list is compared position by position, and a list whose length changed is one
    difference. matched names one list by its path and says which of its positions belong
    together, as pairs of a position in before and a position in after. Only those pairs
    are compared in that list, each under its position in before.
    """
    if isinstance(before, NestedJson) and isinstance(after, NestedJson):
        yield from differences(before.value, after.value, path, matched)
    elif isinstance(before, dict) and isinstance(after, dict):
        if list(before) != list(after):
            yield (path, "fields " + ", ".join(before), "fields " + ", ".join(after))
            return
        for key in before:
            yield from differences(before[key], after[key], f"{path} > {key}" if path else key, matched)
    elif isinstance(before, list) and isinstance(after, list):
        if matched is not None and path == matched[0]:
            for left, right in matched[1]:
                yield from differences(before[left], after[right], f"{path}[{left}]")
            return
        if len(before) != len(after):
            yield (path, f"{len(before)} items", f"{len(after)} items")
            return
        for position, (left, right) in enumerate(zip(before, after)):
            yield from differences(left, right, f"{path}[{position}]", matched)
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


def _clip_path(entry: int) -> str:
    return _gear_path(entry, f"{_WEAPON_RECORD} > {_CLIP}")


def _wrong_changes(found: Sequence[Difference], expected: Sequence[Difference]) -> str:
    """What to say when two saves do not differ in exactly the expected values."""
    def shown(some: Sequence[Difference]) -> str:
        return "; ".join(f"{path}: {old} -> {new}" for path, old, new in some[:5])

    if len(expected) == 1:
        return ("safety check failed: the edit did not change exactly the one value asked for. "
                f"Found: {shown(found) or 'no difference at all'}")
    extra = [difference for difference in found if difference not in expected]
    missing = [difference for difference in expected if difference not in found]
    parts = [f"{len(extra)} not asked for ({shown(extra)})"] if extra else []
    if missing:
        parts.append(f"{len(missing)} asked for and not made ({shown(missing)})")
    return (f"safety check failed: the edit did not change exactly the {len(expected)} values asked for. "
            f"Found {_count_text(len(found), 'difference')}: "
            + (", ".join(parts) or "the same values in another order"))


def _prove_changes(original: bytes, edited: bytes, expected: Sequence[Difference]) -> Tuple[int, int]:
    """Decode both saves afresh and confirm that they differ in exactly the expected values, in save order.

    Returns (entries in the save, entries that are byte for byte unchanged).
    """
    recheck = codec.verify_save(edited)
    if not recheck.identical:
        raise EditError(f"safety check failed: the edited save does not rebuild exactly ({recheck.detail})")
    found = list(differences(codec.decode_save(original).slot, codec.decode_save(edited).slot))
    if found != list(expected):
        raise EditError(_wrong_changes(found, expected))
    table_before = json.loads(codec.lzf_decompress(original))[codec.ENTRY_TABLE]
    table_after = json.loads(codec.lzf_decompress(edited))[codec.ENTRY_TABLE]
    changed = [name for name, encoded in table_before.items() if table_after.get(name) != encoded]
    if list(table_before) != list(table_after) or changed != ["global"]:
        raise EditError("safety check failed: entries other than global changed: " + ", ".join(changed))
    return len(table_before), len(table_before) - len(changed)


def _prove_single_change(original: bytes, edited: bytes, expected: Difference) -> Tuple[int, int]:
    """Decode both saves afresh and confirm that they differ in exactly the expected value.

    Returns (entries in the save, entries that are byte for byte unchanged).
    """
    return _prove_changes(original, edited, [expected])


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


@dataclass(frozen=True)
class RoundsEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int
    old_rounds: int
    new_rounds: int
    holds: int                # rounds the weapon holds at once
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def _prove_clip(edited: bytes, entry: int, expected: List[str]) -> None:
    """Decode the edited save afresh and confirm that the weapon holds exactly the expected rounds.

    The comparison of two saves reports a list that changed its length as one difference and
    does not look inside it. So the rounds themselves are checked here.
    """
    weapon = _weapon_of(_inventory_items(codec.decode_save(edited))[entry])
    found = None if weapon is None else [value.literal for value in weapon.clip]
    if found != expected:
        raise EditError("safety check failed: the rounds in the edited save are not the ones asked for. "
                        f"Found: {found}")


def set_weapon_rounds(original: bytes, prefab: str, rounds: int, entry: Optional[int] = None) -> RoundsEdit:
    """Return the save with the rounds loaded in one inventory weapon changed. Reads and writes no file.

    A round that is added is stored the way real saves store a loaded round. Rounds that are
    taken out come off the end of the list. Raises EditError under the same rules as
    set_stack_count, when the save holds no rounds for the entry, when this tool has no
    number for what the weapon holds, and when the rounds do not fit next to the spent
    casings still in the weapon.
    """
    if rounds < 0:
        raise EditError(f"the number of rounds must be a whole number from 0 up, not {rounds}")
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    chosen, weapon = _select_weapon(_inventory_items(save), prefab, entry)
    holds = ROUNDS_HELD.get(prefab)
    if holds is None:
        raise EditError(f"this tool has no number for the rounds {prefab} holds, so it does not change them")
    fit = max(holds - weapon.spent, 0)
    if rounds > fit and weapon.spent:
        room = "none fit" if fit == 0 else f"at most {fit} fit"
        raise EditError(f"{prefab} holds {_count_text(holds, 'round')}, and inventory entry {chosen} still has "
                        f"{_count_text(weapon.spent, 'spent casing')} in it, so {room}. Reload it in the game "
                        "to drop the casings")
    if rounds > fit:
        raise EditError(f"{prefab} holds {_count_text(holds, 'round')}, so {rounds} do not fit")
    old = len(weapon.clip)
    if rounds == old:
        raise EditError(f"inventory entry {chosen} ({prefab}) already holds {_count_text(rounds, 'round')}. "
                        "Nothing to change")
    added = max(rounds - old, 0)
    expected_rounds = [value.literal for value in weapon.clip[:rounds]] + [_NEW_ROUND] * added
    weapon.content[_CLIP] = weapon.clip[:rounds] + [Number(_NEW_ROUND) for _ in range(added)]
    edited = codec.encode_save(save)
    expected = (_clip_path(chosen), f"{old} items", f"{rounds} items")
    entries, unchanged = _prove_single_change(original, edited, expected)
    _prove_clip(edited, chosen, expected_rounds)
    return RoundsEdit(edited, prefab, chosen, old, rounds, holds, entries, unchanged)


_FULL_CONDITION = "1.0"

# A torch and a flare keep their burn state and burn time in a record of their own.
_LIGHT_RECORDS = ("m_TorchItemSerialized", "m_FlareItemSerialized")


def _has_burned(gear: Dict[str, JsonValue]) -> bool:
    """Whether the item is a torch or a flare whose record holds more than a fresh one's.

    A fresh one has no state, or the state Fresh, and no burn time. Anything else counts as
    burned, and so does a record that cannot be read.
    """
    for field in _LIGHT_RECORDS:
        record = gear.get(field)
        if record is None:
            continue
        content = record.value if isinstance(record, NestedJson) else None
        if not isinstance(content, dict):
            return True
        state = content.get("m_StateProxy")
        minutes = content.get("m_ElapsedBurnMinutesProxy")
        if state not in (None, "Fresh") or (minutes is not None and _finite(minutes) != 0):
            return True
    return False


@dataclass(frozen=True)
class RepairEdit:
    data: bytes               # the edited save
    repaired: Tuple[Tuple[int, str, str], ...]    # (entry, item, the condition it had as stored), in save order
    full: int                 # items that were at 100% already
    ruined: int               # ruined items. They are left alone
    burned: int               # torches and flares that have burned. They are left alone
    unreadable: int           # items whose condition this tool does not change. They are left alone
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def repair_inventory(original: bytes) -> RepairEdit:
    """Return the save with every inventory item below 100% set to 100%. Reads and writes no file.

    The rule for each item is the rule of set_item_condition. A ruined item, an item without
    a name, and an item whose condition is stored in a form this tool does not change are
    left alone and counted. So is a torch or a flare that has burned: the save keeps its
    burn time next to its condition. Raises EditError when the save fails the exact rebuild
    check, when there is nothing to repair, and when the result differs from the original
    in anything but the conditions of the repaired items.
    """
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    repaired: List[Tuple[int, str, str]] = []
    full = ruined = burned = unreadable = 0
    for entry, item in enumerate(_inventory_items(save)):
        prefab = _prefab_of(item)
        try:
            gear, old = _editable_condition(item, entry, prefab or "an item without a name")
        except _RuinedItem:
            ruined += 1
            continue
        except EditError:
            unreadable += 1
            continue
        if prefab is None:
            unreadable += 1
        elif _as_float32(float(old)) == 1:
            full += 1
        elif _has_burned(gear):
            burned += 1
        else:
            gear["m_NormalizedCondition"] = Number(_FULL_CONDITION)
            repaired.append((entry, prefab, old))
    if not repaired:
        raise EditError(f"there is nothing to repair in the inventory: {full} at 100%, {ruined} ruined, "
                        f"{burned} burned torches or flares, {unreadable} with a condition this tool does "
                        "not change. Nothing to change")
    edited = codec.encode_save(save)
    expected = [(_condition_path(entry), old, _FULL_CONDITION) for entry, _, old in repaired]
    entries, unchanged = _prove_changes(original, edited, expected)
    return RepairEdit(edited, tuple(repaired), full, ruined, burned, unreadable, entries, unchanged)


_SIGNED_WHOLE = re.compile(r"-?[0-9]+")

# The game keeps the water you carry in two inventory entries, and every real save has both.
_WATER_RECORD = "m_WaterSupplySerialized"


@dataclass(frozen=True)
class RemoveEdit:
    data: bytes               # the edited save
    prefab: str
    entry: int                # the entry number the item had
    count: Optional[int]      # units in the stack that went, or None when it was not a stack
    condition: Optional[float]    # 0 to 100, or None when the save held no usable value
    remaining: int            # inventory entries that are left
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def _items_path() -> str:
    return f"{codec.ENTRY_TABLE} > global > m_Inventory_Serialized > m_SerializedItems"


def _mentions(value: JsonValue, number: int, path: str = "") -> Iterator[str]:
    """Every place in a decoded value that holds this whole number, as a number or as text."""
    if isinstance(value, NestedJson):
        yield from _mentions(value.value, number, path)
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield from _mentions(inner, number, f"{path} > {key}" if path else key)
    elif isinstance(value, list):
        for position, inner in enumerate(value):
            yield from _mentions(inner, number, f"{path}[{position}]")
    elif isinstance(value, (Number, str)):
        literal = value.literal if isinstance(value, Number) else value
        if _SIGNED_WHOLE.fullmatch(literal) and int(literal) == number:
            yield path


def _why_it_stays(save: DecodedSave, entry: int, prefab: str) -> Optional[str]:
    """The reason this tool does not remove an inventory entry, or None when it may go.

    An item stays when the game needs it, when it is worn, and when the save holds its
    instance number anywhere else, because that other place would then point at nothing.
    """
    items = _inventory_items(save)
    gear = _gear_of(items[entry])
    identity = gear.get("m_InstanceIDProxy") if gear is not None else None
    if gear is None or not isinstance(identity, Number) or not _SIGNED_WHOLE.fullmatch(identity.literal):
        return "its instance number is not stored the way this tool expects"
    number = int(identity.literal)
    if gear.get(_WATER_RECORD) is not None:
        return "the game keeps the water you carry in this entry. Use set-liters to empty it"
    clothing = gear.get("m_ClothingItemSerialized")
    worn = clothing.value.get("m_WearingProxy") if isinstance(clothing, NestedJson) \
        and isinstance(clothing.value, dict) else None
    if worn is True:
        return "you are wearing it in this save. Take it off in the game first"
    if worn is not None and worn is not False:
        return "whether you are wearing it is not stored the way this tool expects"
    if abs(number) < 1000:
        return f"its instance number, {number}, is too short to tell apart from other numbers in the save"
    own = _gear_path(entry, "m_InstanceIDProxy")
    elsewhere = next((place for place in _mentions(save.slot, number) if place != own), None)
    if elsewhere is None:
        return None
    if elsewhere.endswith("> m_ItemInHandsInstanceID"):
        return "it is in your hands in this save. Put it away in the game first"
    if elsewhere.endswith("> m_LastUnequippedItemInstanceID"):
        return ("it is the last item you held, and the save remembers it by its number. Hold another item in "
                "the game and put that away first")
    return f"the save holds its instance number in another place too: {elsewhere}"


def _prove_removal(original: bytes, edited: bytes, entry: int) -> None:
    """Decode both saves afresh and confirm that the edited inventory is the original one without one entry.

    The comparison of two saves reports a list that changed its length as one difference and
    does not look inside it. So every item that is left is compared here with the item it
    must be.
    """
    before = _inventory_items(codec.decode_save(original))
    after = _inventory_items(codec.decode_save(edited))
    kept = before[:entry] + before[entry + 1:]
    wrong = [position for position, (left, right) in enumerate(zip(kept, after))
             if next(differences(left, right), None) is not None]
    if len(after) != len(kept) or wrong:
        detail = f"{len(after)} items where {len(kept)} were expected" if len(after) != len(kept) \
            else f"the item now at entry {wrong[0]} is not the one that belongs there"
        raise EditError("safety check failed: the inventory in the edited save is not the original one without "
                        f"entry {entry}: {detail}")


def remove_item(original: bytes, prefab: str, entry: Optional[int] = None) -> RemoveEdit:
    """Return the save without one inventory entry. Reads and writes no file.

    A stack goes as a whole. Raises EditError under the same rules as set_stack_count, and
    when the entry is one this tool leaves in place: the water you carry, an item you are
    wearing, and an item whose instance number the save holds in another place as well.
    """
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    items = _inventory_items(save)
    chosen, item = _select_item(items, prefab, entry)
    reason = _why_it_stays(save, chosen, prefab)
    if reason is not None:
        raise EditError(f"inventory entry {chosen} ({prefab}) is not removed: {reason}")
    stack = _stack_of(item)
    condition = _condition_of(item)
    before = len(items)
    del items[chosen]
    edited = codec.encode_save(save)
    expected = (_items_path(), f"{before} items", f"{before - 1} items")
    entries, unchanged = _prove_single_change(original, edited, expected)
    _prove_removal(original, edited, chosen)
    return RemoveEdit(edited, prefab, chosen, None if stack is None else stack[1], condition, before - 1,
                      entries, unchanged)


_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class CopyEdit:
    data: bytes               # the edited save
    prefab: str
    source: int               # the entry that was copied
    entry: int                # the entry number of the copy: the last one in the inventory
    count: Optional[int]      # units in the stack, or None when the item is not a stack
    old_number: str           # the instance number of the source, as stored
    new_number: str           # the instance number of the copy
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def _second_identity(value: JsonValue, path: str = "") -> Optional[str]:
    """Where an item record holds something, besides its own instance number, that names one item only.

    That is the instance number of an item inside it, a GUID, or a mission object. None when
    it holds nothing of the kind.
    """
    if isinstance(value, NestedJson):
        return _second_identity(value.value, path)
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path} > {key}" if path else key
            inner_item = key == "m_InstanceIDProxy" and path != ""
            if inner_item or (key == "m_MissionObjectSerialized" and inner is not None):
                return here
            found = _second_identity(inner, here)
            if found is not None:
                return found
    elif isinstance(value, list):
        for position, inner in enumerate(value):
            found = _second_identity(inner, f"{path}[{position}]")
            if found is not None:
                return found
    elif isinstance(value, str) and _GUID.fullmatch(value):
        return path
    return None


def _why_no_copy(item: JsonValue) -> Optional[str]:
    """The reason this tool does not copy an inventory entry, or None when it may be copied.

    A copy is a second item that is the same in all but its instance number. So an item is
    not copied when the game expects just one of it, when the copy would be worn as well, and
    when its record names this one item in another way too.
    """
    gear = _gear_of(item)
    identity = gear.get("m_InstanceIDProxy") if gear is not None else None
    if gear is None or not isinstance(identity, Number) or not _SIGNED_WHOLE.fullmatch(identity.literal):
        return "its instance number is not stored the way this tool expects"
    if gear.get(_WATER_RECORD) is not None:
        return "the game keeps the water you carry in this entry. Use set-liters to add water"
    clothing = gear.get("m_ClothingItemSerialized")
    worn = clothing.value.get("m_WearingProxy") if isinstance(clothing, NestedJson) \
        and isinstance(clothing.value, dict) else None
    if worn is True:
        return ("you are wearing it in this save, and the copy would be worn as well. Take it off in the "
                "game first")
    if worn is not None and worn is not False:
        return "whether you are wearing it is not stored the way this tool expects"
    second = _second_identity(gear)
    if second is not None:
        return ("its record names this one item in another way too, so a copy would not be a second item: "
                f"{second}")
    return None


def _whole_numbers(value: JsonValue) -> Iterator[int]:
    """Every whole number a decoded value holds, as a number or as text."""
    if isinstance(value, NestedJson):
        yield from _whole_numbers(value.value)
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _whole_numbers(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _whole_numbers(inner)
    elif isinstance(value, (Number, str)):
        literal = value.literal if isinstance(value, Number) else value
        if _SIGNED_WHOLE.fullmatch(literal):
            yield int(literal)


def _unused_number(save: DecodedSave) -> int:
    """An instance number for a new inventory item.

    It is even and negative, as every number the game hands out while it runs is in the real
    saves. It is below every instance number in the inventory, and the save holds it nowhere.
    """
    carried = [0]
    for gear in map(_gear_of, _inventory_items(save)):
        number = gear.get("m_InstanceIDProxy") if gear is not None else None
        if isinstance(number, Number) and _SIGNED_WHOLE.fullmatch(number.literal):
            carried.append(int(number.literal))
    lowest = min(carried)
    taken = set(_whole_numbers(save.slot))
    candidate = lowest - 2 - lowest % 2
    while candidate in taken:
        candidate -= 2
    return candidate


def _prove_copy(original: bytes, edited: bytes, source: int, new_number: str) -> None:
    """Decode both saves afresh and confirm that the edited inventory is the original one plus one copy.

    The comparison of two saves reports a list that changed its length as one difference and
    does not look inside it. So this checks that every original item is still in its place,
    that the new last item differs from its source in the instance number only, and that the
    save holds the new number in that one place.
    """
    before = _inventory_items(codec.decode_save(original))
    decoded = codec.decode_save(edited)
    after = _inventory_items(decoded)
    problem = None
    if len(after) != len(before) + 1:
        problem = f"{len(after)} items where {len(before) + 1} were expected"
    else:
        moved = [position for position, (left, right) in enumerate(zip(before, after))
                 if next(differences(left, right), None) is not None]
        old = _gear_of(before[source])["m_InstanceIDProxy"].literal     # type: ignore[index,union-attr]
        if moved:
            problem = f"the item at entry {moved[0]} is not the original one"
        elif list(differences(before[source], after[-1])) != [("m_SerializedGear > m_InstanceIDProxy", old,
                                                              new_number)]:
            problem = f"the new item is not entry {source} with a new instance number and nothing else changed"
        elif list(_mentions(decoded.slot, int(new_number))) != [_gear_path(len(before), "m_InstanceIDProxy")]:
            problem = f"the save does not hold the new instance number {new_number} in exactly one place"
    if problem is not None:
        raise EditError("safety check failed: the inventory in the edited save is not the original one plus a "
                        f"copy of entry {source}: {problem}")


def copy_item(original: bytes, prefab: str, entry: Optional[int] = None) -> CopyEdit:
    """Return the save with one more inventory entry: a copy of an entry the inventory holds.

    Reads and writes no file. The copy goes to the end of the inventory and gets an instance
    number of its own. Raises EditError under the same rules as set_stack_count, and when the
    entry is one this tool does not copy: the water you carry, an item you are wearing, and an
    item whose record names it in another way besides its instance number.
    """
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    items = _inventory_items(save)
    chosen, item = _select_item(items, prefab, entry)
    reason = _why_no_copy(item)
    if reason is not None:
        raise EditError(f"inventory entry {chosen} ({prefab}) is not copied: {reason}")
    stack = _stack_of(item)
    old = _gear_of(item)["m_InstanceIDProxy"].literal                   # type: ignore[index,union-attr]
    new = str(_unused_number(save))
    made = copy.deepcopy(item)
    _gear_of(made)["m_InstanceIDProxy"] = Number(new)                   # type: ignore[index]
    before = len(items)
    items.append(made)
    edited = codec.encode_save(save)
    expected = (_items_path(), f"{before} items", f"{before + 1} items")
    entries, unchanged = _prove_single_change(original, edited, expected)
    _prove_copy(original, edited, chosen, new)
    return CopyEdit(edited, prefab, chosen, before, None if stack is None else stack[1], old, new,
                    entries, unchanged)


# ---------------------------------------------------------------------------
# Comparing two saves whose inventories do not hold the same entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SaveComparison:
    """How a second save differs from a first one."""
    removed: Tuple[Tuple[int, str], ...]    # inventory entries only the first save holds: (entry there, item)
    added: Tuple[Tuple[int, str], ...]      # inventory entries only the second save holds: (entry there, item)
    changes: Tuple[Difference, ...]         # every other value that differs, as differences() reports it


def _instance_numbers(items: List[JsonValue]) -> Optional[List[int]]:
    """The instance number of every inventory entry, in order.

    None when an entry has no whole number, or when two entries have the same one.
    """
    numbers: List[int] = []
    for item in items:
        gear = _gear_of(item)
        number = gear.get("m_InstanceIDProxy") if gear is not None else None
        if not isinstance(number, Number) or not _SIGNED_WHOLE.fullmatch(number.literal):
            return None
        numbers.append(int(number.literal))
    return numbers if len(set(numbers)) == len(numbers) else None


def compare_saves(before: DecodedSave, after: DecodedSave) -> SaveComparison:
    """How the second save differs from the first one, with inventory entries that came or went named.

    differences() compares a list position by position. That is wrong for an inventory with
    an entry added or removed, because every entry behind it has moved. So the entries of the
    two inventories are matched by instance number here. An entry that only one save holds is
    listed as removed or added, and an entry that both hold is compared under its entry number
    in the first save.

    When the entries cannot be matched (an instance number is missing or occurs twice, or the
    entries both saves hold are in another order), nothing is listed as removed or added, and
    changes is what differences() reports for the two saves. For two inventories with the
    same entries in the same order the result is the same either way.
    """
    def position_by_position() -> SaveComparison:
        return SaveComparison((), (), tuple(differences(before.slot, after.slot)))

    def only_in(items: List[JsonValue], kept: Set[int]) -> Tuple[Tuple[int, str], ...]:
        return tuple((entry, _prefab_of(item) or "an item without a name")
                     for entry, item in enumerate(items) if entry not in kept)

    try:
        old_items, new_items = _inventory_items(before), _inventory_items(after)
    except EditError:
        return position_by_position()
    old_numbers, new_numbers = _instance_numbers(old_items), _instance_numbers(new_items)
    if old_numbers is None or new_numbers is None:
        return position_by_position()
    place = {number: entry for entry, number in enumerate(old_numbers)}
    pairs = [(place[number], entry) for entry, number in enumerate(new_numbers) if number in place]
    if any(first[0] > second[0] for first, second in zip(pairs, pairs[1:])):
        return position_by_position()
    changes = tuple(differences(before.slot, after.slot, matched=(_items_path(), pairs)))
    return SaveComparison(only_in(old_items, {left for left, _ in pairs}),
                          only_in(new_items, {right for _, right in pairs}), changes)


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


def _run_weapons(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        save = codec.decode_save(data)
        listed = list_weapons(save)
        total = len(_inventory_items(save))
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max([len(row.prefab) for row in listed] + [4])
    print(f"{'entry':>5}  {'item':<{width}}  rounds  spent casings  holds")
    for row in listed:
        holds = "" if row.holds is None else str(row.holds)
        print(f"{row.entry:>5}  {row.prefab:<{width}}  {row.rounds:>6}  {row.spent:>13}  {holds:>5}".rstrip())
    print(f"{_count_text(len(listed), 'item')} that can hold rounds among "
          f"{_count_text(total, 'inventory item')} in {path.name}.")
    return 0


def _percent(stored: str) -> str:
    return f"{float(stored) * 100:.1f}%"


def _run_edit(path: Path, out: Path, command: str, prefab: str, value: Union[float, Decimal],
              entry: Optional[int]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    edit: Union[CountEdit, ConditionEdit, LitersEdit, CaloriesEdit, RoundsEdit]
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
        elif command == "set-rounds":
            edit = set_weapon_rounds(data, prefab, int(value), entry)
            change = f"rounds {edit.old_rounds} -> {edit.new_rounds} ({edit.prefab} holds {edit.holds})"
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


def _run_repair(path: Path, out: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = repair_inventory(data)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    count = len(edit.repaired)
    total = count + edit.full + edit.ruined + edit.burned + edit.unreadable
    values = "this one value" if count == 1 else f"these {count} values"
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Change:   condition set to 100% for {count} of the {_count_text(total, 'item')} in the inventory")
    print(f"Left:     {edit.full} at 100% already, {edit.ruined} ruined, {edit.burned} burned torches or "
          f"flares, {edit.unreadable} with a condition this tool does not change")
    print(f"Checked:  the edited save differs from the original in exactly {values}")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "repair-all" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by repair-all has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _run_remove(path: Path, out: Path, prefab: str, entry: Optional[int]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = remove_item(data, prefab, entry)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    stack = "" if edit.count is None else f"a stack of {edit.count}, "
    condition = "no condition" if edit.condition is None else f"condition {edit.condition:.1f}%"
    left = "1 entry is" if edit.remaining == 1 else f"{edit.remaining} entries are"
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Item:     {edit.prefab}, inventory entry {edit.entry}")
    print(f"Change:   removed ({stack}{condition}). {left} left in the inventory")
    print(f"Note:     every entry number above {edit.entry} is now one lower. Run items again before the "
          "next edit")
    print("Checked:  the edited inventory is the original one without this entry, and nothing else differs")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "remove-item" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by remove-item has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _run_copy(path: Path, out: Path, prefab: str, entry: Optional[int]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = copy_item(data, prefab, entry)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    stack = "" if edit.count is None else f" (a stack of {edit.count})"
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Item:     {edit.prefab}, inventory entry {edit.source}")
    print(f"Change:   copied{stack} to a new entry at the end of the inventory, entry {edit.entry}")
    print(f"Number:   the copy has the instance number {edit.new_number}. The original keeps {edit.old_number}")
    print("Checked:  the edited inventory is the original one plus this copy, and nothing else differs")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "copy-item" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by copy-item has been loaded in the game yet. Try it on a save "
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
  python tld_save_edit.py set-rounds sandbox15 --item GEAR_Revolver --rounds 6 --out sandbox15.edited
  python tld_save_edit.py repair-all sandbox15 --out sandbox15.edited
  python tld_save_edit.py remove-item sandbox15 --item GEAR_Stone --out sandbox15.edited
  python tld_save_edit.py copy-item sandbox15 --item GEAR_MRE --out sandbox15.edited

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
        """,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (("items", "list everything in the player's inventory"),
                       ("stacks", "list the stacks in the player's inventory"),
                       ("liquids", "list the inventory items that hold liquid"),
                       ("foods", "list the inventory items with calories"),
                       ("weapons", "list the inventory items that can hold rounds")):
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
    set_rounds = commands.add_parser("set-rounds", help="change the rounds loaded in one inventory weapon "
                                                        "(not tested in the game yet)")
    set_rounds.add_argument("save", type=Path, metavar="SAVE",
                            help="the save file to read. It is not changed")
    set_rounds.add_argument("--item", required=True, metavar="GEAR_NAME",
                            help="the weapon, as weapons prints it, for example GEAR_Revolver")
    set_rounds.add_argument("--rounds", required=True, type=int, metavar="N",
                            help="the new number of loaded rounds, from 0 to what the weapon holds")
    set_rounds.add_argument("--entry", type=int, metavar="N",
                            help="the entry number from weapons. Needed when the inventory holds "
                                 "more than one of the weapon")
    set_rounds.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                            help="the file to create. It must not exist yet")
    repair_all = commands.add_parser("repair-all", help="set every inventory item below 100%% to 100%% "
                                                        "(not tested in the game yet)")
    repair_all.add_argument("save", type=Path, metavar="SAVE",
                            help="the save file to read. It is not changed")
    repair_all.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                            help="the file to create. It must not exist yet")
    remove = commands.add_parser("remove-item", help="take one item or one whole stack out of the inventory "
                                                     "(not tested in the game yet)")
    remove.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    remove.add_argument("--item", required=True, metavar="GEAR_NAME",
                        help="the item, as items prints it, for example GEAR_Stone")
    remove.add_argument("--entry", type=int, metavar="N",
                        help="the entry number from items. Needed when the inventory holds more than one "
                             "of the item")
    remove.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                        help="the file to create. It must not exist yet")
    duplicate = commands.add_parser("copy-item", help="add another one of an item the inventory holds "
                                                      "(not tested in the game yet)")
    duplicate.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    duplicate.add_argument("--item", required=True, metavar="GEAR_NAME",
                           help="the item to copy, as items prints it, for example GEAR_MRE")
    duplicate.add_argument("--entry", type=int, metavar="N",
                           help="the entry number from items. Needed when the inventory holds more than "
                                "one of the item")
    duplicate.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                           help="the file to create. It must not exist yet")
    args = parser.parse_args(argv)
    if args.command in ("items", "stacks"):
        return _run_list(args.save, stacks_only=args.command == "stacks")
    if args.command == "liquids":
        return _run_liquids(args.save)
    if args.command == "foods":
        return _run_foods(args.save)
    if args.command == "weapons":
        return _run_weapons(args.save)
    if args.command == "repair-all":
        return _run_repair(args.save, args.out)
    if args.command == "remove-item":
        return _run_remove(args.save, args.out, args.item, args.entry)
    if args.command == "copy-item":
        return _run_copy(args.save, args.out, args.item, args.entry)
    value: Union[float, Decimal]
    if args.command == "set-count":
        value = args.count
    elif args.command == "set-liters":
        value = args.liters
    elif args.command == "set-calories":
        value = args.calories
    elif args.command == "set-rounds":
        value = args.rounds
    else:
        value = args.condition
    return _run_edit(args.save, args.out, args.command, args.item, value, args.entry)


if __name__ == "__main__":
    sys.exit(main())
