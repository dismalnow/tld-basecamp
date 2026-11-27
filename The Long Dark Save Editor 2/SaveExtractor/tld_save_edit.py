#!/usr/bin/env python3
"""
tld_save_edit.py: change a value in a The Long Dark save and write the result to a new file.

Usage:
    python tld_save_edit.py items SAVE
    python tld_save_edit.py stacks SAVE
    python tld_save_edit.py liquids SAVE
    python tld_save_edit.py foods SAVE
    python tld_save_edit.py weapons SAVE
    python tld_save_edit.py player SAVE
    python tld_save_edit.py skills SAVE
    python tld_save_edit.py afflictions SAVE
    python tld_save_edit.py position SAVE
    python tld_save_edit.py set-count SAVE --item GEAR_NAME --count N --out NEW_FILE [--entry N]
    python tld_save_edit.py set-condition SAVE --item GEAR_NAME --condition PERCENT --out NEW_FILE [--entry N]
    python tld_save_edit.py set-liters SAVE --item GEAR_NAME --liters LITERS --out NEW_FILE [--entry N]
    python tld_save_edit.py set-calories SAVE --item GEAR_NAME --calories CALORIES --out NEW_FILE [--entry N]
    python tld_save_edit.py set-rounds SAVE --item GEAR_NAME --rounds N --out NEW_FILE [--entry N]
    python tld_save_edit.py repair-all SAVE --out NEW_FILE
    python tld_save_edit.py remove-item SAVE --item GEAR_NAME --out NEW_FILE [--entry N]
    python tld_save_edit.py copy-item SAVE --item GEAR_NAME --out NEW_FILE [--entry N]
    python tld_save_edit.py set-player SAVE [--condition PERCENT] [--calories CALORIES]
                                            [--hydration PERCENT] [--rest PERCENT] [--warmth PERCENT]
                                            --out NEW_FILE
    python tld_save_edit.py set-skill SAVE --skill NAME --points N --out NEW_FILE
    python tld_save_edit.py cure SAVE --out NEW_FILE
    python tld_save_edit.py set-position SAVE [--x X] [--y Y] [--z Z] --out NEW_FILE

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

player lists the player's own values: condition, calories, and the three bars for thirst,
fatigue and cold. set-player changes one or more of them. It takes every number the way
the game shows it, so 100 is a full bar: --hydration 100 is not thirsty at all, --rest 100
is fully rested, and --warmth 100 is warm. The game leaves a number out of the save when it
is 0, and set-player puts such a number in where the game writes it.

skills lists the points of the nine skills, and set-skill changes the points of one. The
save holds points and no level.

afflictions lists five kinds of affliction: hypothermia, sprain pain, sprained ankles,
sprained wrists and burns. cure ends every case of those five that the save holds. A save
can hold other afflictions. This tool does not read them and leaves them as they are.

position prints the scene the save is in and where the character stands in it. set-position
moves the character inside that scene: give one or more of --x, --y and --z, where y is the
height. The tool does not know the ground, so take the numbers from a spot that is known to
be good. Write a number that has an exponent after an equals sign: --y=-1.19E-07.

set-liters, set-calories, set-rounds, repair-all, remove-item, copy-item, set-player,
set-skill, cure and set-position have not been tested in the game yet. Their own output
says so.

The save you name is only read. The result goes to NEW_FILE, which must not exist yet and
must not be named like a live save (sandbox1, challenge3, ...). Putting it in place of the
original is a separate step that you do yourself, after a backup.

Three checks stand between an edit and a written file:
    1. The original must pass the exact rebuild check (tld_save_codec.py verify).
    2. The edited save, decoded again, must differ from the original in exactly what was
       asked for: one value, for repair-all one condition for each repaired item, for
       remove-item one inventory entry less, for copy-item one more, for set-player and
       set-position one value for each option, and for cure the values that end each
       case. Every other entry must be byte for byte unchanged.
       When an edit puts in a number the game had left out, the record that got it is
       checked field by field as well.
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
MAX_SKILL_POINTS = 9999  # the most in 14 real saves is 264. The limit only catches a slip of the hand
MAX_COORDINATE = 100000.0    # the farthest in 14 real saves is 1980. The limit only catches a slip of the hand

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
                                "copy-item", "set-player", "set-skill", "cure", "set-position"})

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
    the same float back. Meant for 0 and for values of a size from 0.0001 to 100000, with
    either sign, which need no exponent."""
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
                matched: Optional[Tuple[str, Sequence[Tuple[int, int]]]] = None, *,
                each_field: bool = False) -> Iterator[Difference]:
    """Every place where two decoded values differ, as (path, before, after).

    A list is compared position by position, and a list whose length changed is one
    difference. matched names one list by its path and says which of its positions belong
    together, as pairs of a position in before and a position in after. Only those pairs
    are compared in that list, each under its position in before.

    Two objects that do not hold the same fields in the same order are one difference, which
    names the fields of both. That is what an edit is proven against. With each_field, which
    is for showing a person what differs, they are taken apart: a field that only one of them
    holds is a difference of its own, with "(left out)" on the other side, the fields that
    both hold are compared one by one, and another order of those is one more difference.
    """
    if isinstance(before, NestedJson) and isinstance(after, NestedJson):
        yield from differences(before.value, after.value, path, matched, each_field=each_field)
    elif isinstance(before, dict) and isinstance(after, dict):
        if list(before) != list(after):
            if not each_field:
                yield (path, "fields " + ", ".join(before), "fields " + ", ".join(after))
                return
            previous = [key for key in before if key in after]
            current = [key for key in after if key in before]
            if previous != current:
                yield (path, "fields in the order " + ", ".join(previous),
                       "fields in the order " + ", ".join(current))
            for key in before:
                if key not in after:
                    yield (f"{path} > {key}" if path else key, _show(before[key]), "(left out)")
        for key in after:
            field_path = f"{path} > {key}" if path else key
            if key in before:
                yield from differences(before[key], after[key], field_path, matched, each_field=each_field)
            else:
                yield (field_path, "(left out)", _show(after[key]))
    elif isinstance(before, list) and isinstance(after, list):
        if matched is not None and path == matched[0]:
            for left, right in matched[1]:
                yield from differences(before[left], after[right], f"{path}[{left}]", matched,
                                       each_field=each_field)
            return
        if len(before) != len(after):
            yield (path, f"{len(before)} items", f"{len(after)} items")
            return
        for position, (left, right) in enumerate(zip(before, after)):
            yield from differences(left, right, f"{path}[{position}]", matched, each_field=each_field)
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
        return SaveComparison((), (), tuple(differences(before.slot, after.slot, each_field=True)))

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
    changes = tuple(differences(before.slot, after.slot, matched=(_items_path(), pairs), each_field=True))
    return SaveComparison(only_in(old_items, {left for left, _ in pairs}),
                          only_in(new_items, {right for _, right in pairs}), changes)


# ---------------------------------------------------------------------------
# The player's own values: condition, calories, and the bars for thirst, fatigue and cold
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PlayerField:
    record: str    # the field of the global entry that holds the record
    field: str     # the number inside that record
    bar: bool      # the save holds the inverse of the bar: 0 for a full one, 100 for an empty one
    low: float     # the lowest and the highest number this tool takes, the way the game shows it
    high: float


# Each value is named for its good end, so that 100 is the best number for every one of them.
# The save holds thirst, fatigue and cold the other way round. In one real save all three had
# been set to 100 with another editor, which is dehydrated, exhausted and freezing.
PLAYER_VALUES: Dict[str, _PlayerField] = {
    "condition": _PlayerField("m_Condition_Serialized", "m_CurrentHPProxy", False, 1.0, 100.0),
    "calories": _PlayerField("m_Hunger_Serialized", "m_CurrentReserveCaloriesProxy", False, 0.0, 2500.0),
    "hydration": _PlayerField("m_Thirst_Serialized", "m_CurrentThirstProxy", True, 0.0, 100.0),
    "rest": _PlayerField("m_Fatigue_Serialized", "m_CurrentFatigueProxy", True, 0.0, 100.0),
    "warmth": _PlayerField("m_Freezing_Serialized", "m_CurrentFreezingProxy", True, 0.0, 100.0),
}


@dataclass(frozen=True)
class PlayerValue:
    """One of the player's own values.

    stored and shown are both there for a value the save holds. With stored missing, the game
    left the number out, which it does for 0. With shown missing, the value cannot be read.
    """
    name: str                   # condition, calories, hydration, rest or warmth
    field: str                  # the number in the save that holds it, for example m_CurrentThirstProxy
    stored: Optional[str]       # that number as the save holds it
    shown: Optional[Decimal]    # the value the way the game shows it: percent, or calories


def _player_record(save: DecodedSave, name: str) -> Optional[Dict[str, JsonValue]]:
    """The record that holds one of the player's values. None when it is not stored as expected."""
    document = save.entries.get("global")
    record = document.get(PLAYER_VALUES[name].record) if isinstance(document, dict) else None
    content = record.value if isinstance(record, NestedJson) else None
    return content if isinstance(content, dict) else None


def _shown(name: str, stored: Decimal) -> Decimal:
    """A stored number the way the game shows it."""
    return 100 - stored if PLAYER_VALUES[name].bar else stored


def _player_text(name: str, shown: Decimal) -> str:
    """One of the player's values the way the game shows it, for example 78.2% of the bar."""
    if name == "calories":
        return f"{shown:.1f}"
    return f"{shown:.1f}%" + (" of the bar" if PLAYER_VALUES[name].bar else "")


def read_player(save: DecodedSave) -> List[PlayerValue]:
    """The player's condition, calories, hydration, rest and warmth.

    The game leaves a number out of the save when it is 0. So a number that is missing from a
    record that can be read counts as 0: no calories, or a full bar.
    """
    values: List[PlayerValue] = []
    for name, where in PLAYER_VALUES.items():
        record = _player_record(save, name)
        if record is None:
            values.append(PlayerValue(name, where.field, None, None))
        elif where.field not in record:
            values.append(PlayerValue(name, where.field, None, _shown(name, Decimal(0))))
        else:
            held = record[where.field]
            number = _finite(held)
            literal = held.literal if isinstance(held, Number) else None
            values.append(PlayerValue(name, where.field, literal,
                                      None if number is None else _shown(name, number)))
    return values


@dataclass(frozen=True)
class PlayerEdit:
    data: bytes               # the edited save
    # (name, the number stored before, the number stored now). The number before is None for
    # a number the game had left out of the save, which the edit put in.
    changes: Tuple[Tuple[str, Optional[str], str], ...]
    entries: int              # entries in the save
    unchanged_entries: int    # entries that are byte for byte the original


def _player_record_path(name: str) -> str:
    return f"{codec.ENTRY_TABLE} > global > {PLAYER_VALUES[name].record}"


def _player_path(name: str) -> str:
    return f"{_player_record_path(name)} > {PLAYER_VALUES[name].field}"


def _prove_put_in(before: Optional[Dict[str, JsonValue]], after: Optional[Dict[str, JsonValue]],
                  record: str, field: str, new: str) -> None:
    """Confirm that a record got one number more, in front, and no other change.

    before and after are that record in the original save and in the edited one, each decoded
    afresh. The comparison of two saves reports a record with another set of fields as one
    difference and does not look inside it. So every field the record had is compared here.
    """
    problem = None
    if before is None or after is None:
        problem = "the record cannot be read"
    elif list(after) != [field] + list(before):
        problem = f"its fields are {', '.join(after)}"
    elif not isinstance(after[field], Number) or after[field].literal != new:
        problem = f"{field} is {_show(after[field])}"
    else:
        changed = [key for key in before if next(differences(before[key], after[key]), None) is not None]
        if changed:
            problem = f"{changed[0]} changed as well"
    if problem is not None:
        raise EditError(f"safety check failed: {record} in the edited save is not the original one with "
                        f"{field} {new} put in front: {problem}")


def _prove_number_put_in(original: bytes, edited: bytes, name: str, new: str) -> None:
    """Decode both saves afresh and run that check for one of the player's values."""
    where = PLAYER_VALUES[name]
    _prove_put_in(_player_record(codec.decode_save(original), name),
                  _player_record(codec.decode_save(edited), name), where.record, where.field, new)


def stored_player_value(name: str, value: float) -> str:
    """The number the save holds for one of the player's values, given the way the game shows it."""
    where = PLAYER_VALUES[name]
    if not where.low <= value <= where.high:
        raise EditError(f"{name} must be a number from {where.low:g} to {where.high:g}, not {value:g}")
    given = Decimal(repr(value))
    stored = 100 - given if where.bar else given
    if 0 < stored < Decimal("0.0001"):
        nearest = where.high if where.bar else 0.0
        raise EditError(f"{name} {value!r} is too close to {nearest:g} for this tool to write. Use {nearest:g}")
    return float32_literal(float(stored))


def set_player_values(original: bytes, values: Dict[str, float]) -> PlayerEdit:
    """Return the save with one or more of the player's own values changed. Reads and writes no file.

    values maps condition, calories, hydration, rest and warmth to the new number, the way the
    game shows it: percent, or calories. Raises EditError for a name or a number outside of
    that, for a value the save does not hold as a plain number, and for a value that is what
    the save holds already.

    The game leaves a number out of the save when it is 0. Such a number is put in as the first
    field of its record, which is where the game writes it. The condition is the exception: a
    save without one is that of a dead character, and this tool does not bring it back.
    """
    unknown = [name for name in values if name not in PLAYER_VALUES]
    if unknown or not values:
        raise EditError("name one or more of " + ", ".join(PLAYER_VALUES)
                        + (f", not {', '.join(unknown)}" if unknown else ""))
    wanted = {name: stored_player_value(name, value) for name, value in values.items()}
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    document = save.entries.get("global")
    order = list(document) if isinstance(document, dict) else []

    def place(name: str) -> int:
        """Where the record of a value stands in the save. The proof wants the changes in that order."""
        record = PLAYER_VALUES[name].record
        return order.index(record) if record in order else len(order)

    changes: List[Tuple[str, Optional[str], str]] = []
    expected: List[Difference] = []
    for name in sorted(wanted, key=place):
        where = PLAYER_VALUES[name]
        record = _player_record(save, name)
        if record is None:
            raise EditError(f"{name} is not stored the way this tool expects ({where.record}), so it is not "
                            "changed")
        new = wanted[name]
        if where.field not in record:
            if name == "condition":
                raise EditError("this save holds no condition, which means 0: the character is dead. This "
                                "tool does not bring it back")
            if Decimal(new) == 0:
                raise EditError(f"{name} is already {_player_text(name, _shown(name, Decimal(0)))}: the game "
                                f"leaves {where.field} out of the save when it is 0. Nothing to change")
            fields = list(record.items())
            record.clear()
            record[where.field] = Number(new)
            record.update(fields)
            changes.append((name, None, new))
            expected.append((_player_record_path(name), "fields " + ", ".join(key for key, _ in fields),
                             "fields " + ", ".join(record)))
            continue
        old = record[where.field]
        number = _finite(old)
        if number is None or not isinstance(old, Number):
            raise EditError(f"{name} is not stored as a number ({where.field}), so it is not changed")
        if number == Decimal(new):
            raise EditError(f"{name} is already {_player_text(name, _shown(name, number))}. Nothing to change")
        record[where.field] = Number(new)
        changes.append((name, old.literal, new))
        expected.append((_player_path(name), old.literal, new))
    edited = codec.encode_save(save)
    entries, unchanged = _prove_changes(original, edited, expected)
    for name, old, new in changes:
        if old is None:
            _prove_number_put_in(original, edited, name, new)
    return PlayerEdit(edited, tuple(changes), entries, unchanged)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_SKILLS_RECORD = "m_SkillsManagerSerialized"
_SKILL_POINTS = "m_Points"

# The nine skills by the names this tool takes, and the record of each one in the save.
SKILLS: Dict[str, str] = {
    "fire-starting": "m_Skill_FirestartingSerialized",
    "carcass-harvesting": "m_Skill_CarcassHarvestingSerialized",
    "cooking": "m_Skill_CookingSerialized",
    "ice-fishing": "m_Skill_IceFishingSerialized",
    "rifle": "m_Skill_RifleSerialized",
    "archery": "m_Skill_ArcherySerialized",
    "mending": "m_Skill_ClothingRepairSerialized",
    "revolver": "m_Skill_RevolverSerialized",
    "gunsmithing": "m_Skill_GunsmithingSerialized",
}


@dataclass(frozen=True)
class Skill:
    """The points of one skill.

    stored and points are both there for a number the save holds. With stored missing and 0
    points, the game left the number out, which it does for 0. With points missing, the number
    cannot be read.
    """
    name: str                # the name this tool takes, for example cooking
    record: str              # the record of the skill in the save
    stored: Optional[str]    # the points as the save holds them
    points: Optional[int]


def _skill_record(save: DecodedSave, name: str) -> Optional[Dict[str, JsonValue]]:
    """The record of one skill. None when it is not stored as expected."""
    document = save.entries.get("global")
    manager = document.get(_SKILLS_RECORD) if isinstance(document, dict) else None
    skills = manager.value if isinstance(manager, NestedJson) else None
    record = skills.get(SKILLS[name]) if isinstance(skills, dict) else None
    content = record.value if isinstance(record, NestedJson) else None
    return content if isinstance(content, dict) else None


def list_skills(save: DecodedSave) -> List[Skill]:
    """The points of the nine skills. Points that the game left out of the save count as 0."""
    listed: List[Skill] = []
    for name, where in SKILLS.items():
        record = _skill_record(save, name)
        if record is None:
            listed.append(Skill(name, where, None, None))
        elif _SKILL_POINTS not in record:
            listed.append(Skill(name, where, None, 0))
        else:
            held = record[_SKILL_POINTS]
            listed.append(Skill(name, where, held.literal if isinstance(held, Number) else None, _whole(held)))
    return listed


@dataclass(frozen=True)
class SkillEdit:
    data: bytes                  # the edited save
    skill: str
    old_points: Optional[str]    # as stored. None for points the game had left out, which the edit put in
    new_points: str
    entries: int                 # entries in the save
    unchanged_entries: int       # entries that are byte for byte the original


def _skill_path(name: str) -> str:
    return f"{codec.ENTRY_TABLE} > global > {_SKILLS_RECORD} > {SKILLS[name]}"


def set_skill_points(original: bytes, skill: str, points: int) -> SkillEdit:
    """Return the save with the points of one skill changed. Reads and writes no file.

    Raises EditError for a name that is no skill, for points outside 0 to MAX_SKILL_POINTS, for
    a skill whose points the save does not hold as a plain whole number, and for the points
    the save holds already. Points that the game left out of the save, which it does for 0,
    are put in as the first field of the skill's record, where the game writes them.
    """
    if skill not in SKILLS:
        close = difflib.get_close_matches(skill, list(SKILLS), n=1)
        raise EditError(f"there is no skill named {skill}. " + (f"Did you mean {close[0]}? " if close else "")
                        + "The skills are " + ", ".join(SKILLS))
    if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= MAX_SKILL_POINTS:
        raise EditError(f"the points must be a whole number from 0 to {MAX_SKILL_POINTS}, not {points}")
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    record = _skill_record(save, skill)
    if record is None:
        raise EditError(f"the skill {skill} is not stored the way this tool expects ({SKILLS[skill]}), so it "
                        "is not changed")
    new = str(points)
    if _SKILL_POINTS not in record:
        if points == 0:
            raise EditError(f"{skill} has 0 points already: the game leaves {_SKILL_POINTS} out of the save "
                            "when it is 0. Nothing to change")
        fields = list(record.items())
        record.clear()
        record[_SKILL_POINTS] = Number(new)
        record.update(fields)
        expected = (_skill_path(skill), "fields " + ", ".join(key for key, _ in fields),
                    "fields " + ", ".join(record))
        old: Optional[str] = None
    else:
        held = record[_SKILL_POINTS]
        if _whole(held) is None:
            raise EditError(f"the points of {skill} are not stored as a plain whole number, so they are not "
                            "changed")
        if _whole(held) == points:
            raise EditError(f"{skill} has {points} points already. Nothing to change")
        record[_SKILL_POINTS] = Number(new)
        old = _show(held)
        expected = (f"{_skill_path(skill)} > {_SKILL_POINTS}", old, new)
    edited = codec.encode_save(save)
    entries, unchanged = _prove_single_change(original, edited, expected)
    if old is None:
        _prove_put_in(_skill_record(codec.decode_save(original), skill),
                      _skill_record(codec.decode_save(edited), skill), SKILLS[skill], _SKILL_POINTS, new)
    return SkillEdit(edited, skill, old, new, entries, unchanged)


# ---------------------------------------------------------------------------
# Afflictions
# ---------------------------------------------------------------------------

_ACTIVE = "m_Active"
_SPRAIN_LISTS = ("m_CausesLocIDs", "m_Locations", "m_ElapsedHoursList", "m_DurationHoursList",
                 "m_ElapsedRestList")


@dataclass(frozen=True)
class _AfflictionKind:
    record: str                # the record of the affliction in the global entry
    lists: Tuple[str, ...]     # the lists that hold one item for each case. Empty for a kind with a switch


# The kinds of affliction this tool reads and cures, by the names it prints. The save holds a
# kind in one of two ways. Hypothermia and burns are there or not, and m_Active says which.
# The others can be there several times, with one item for each case in every list of the record.
AFFLICTIONS: Dict[str, _AfflictionKind] = {
    "hypothermia": _AfflictionKind("m_HypothermiaSerialized", ()),
    "sprain-pain": _AfflictionKind("m_SprainPainSerialized", ("m_Locations", "m_Causes", "m_EndTimes")),
    "sprained-ankle": _AfflictionKind("m_SprainedAnkleSerialized", _SPRAIN_LISTS),
    "sprained-wrist": _AfflictionKind("m_SprainedWristSerialized", _SPRAIN_LISTS),
    "burns": _AfflictionKind("m_BurnsSerialized", ()),
}


@dataclass(frozen=True)
class Affliction:
    name: str                # the name this tool prints, for example sprained-wrist
    record: str              # the record of the affliction in the save
    cases: Optional[int]     # how many the save holds: 0 or 1 for a kind with a switch. None: it cannot be read


def _affliction(save: DecodedSave, name: str) -> Optional[Tuple[Dict[str, JsonValue], int]]:
    """The record of one kind of affliction and the cases it holds. None when it is not stored as expected."""
    document = save.entries.get("global")
    record = document.get(AFFLICTIONS[name].record) if isinstance(document, dict) else None
    content = record.value if isinstance(record, NestedJson) else None
    if not isinstance(content, dict):
        return None
    lists = AFFLICTIONS[name].lists
    if not lists:
        # The game leaves the switch out of the save while it is off.
        active = content.get(_ACTIVE, False)
        return (content, int(active)) if isinstance(active, bool) else None
    lengths: Set[int] = set()
    for field in lists:
        held = content.get(field)
        if not isinstance(held, list):
            return None
        lengths.add(len(held))
    return (content, lengths.pop()) if len(lengths) == 1 else None


def list_afflictions(save: DecodedSave) -> List[Affliction]:
    """What the save holds of the kinds of affliction this tool reads."""
    listed: List[Affliction] = []
    for name, kind in AFFLICTIONS.items():
        read = _affliction(save, name)
        listed.append(Affliction(name, kind.record, None if read is None else read[1]))
    return listed


@dataclass(frozen=True)
class CureEdit:
    data: bytes                            # the edited save
    cured: Tuple[Tuple[str, int], ...]     # (affliction, cases it had), in the order of the save
    changes: int                           # values that differ from the original
    entries: int                           # entries in the save
    unchanged_entries: int                 # entries that are byte for byte the original


def cure_afflictions(original: bytes) -> CureEdit:
    """Return the save with every case of the afflictions this tool reads ended. Reads and writes no file.

    Hypothermia and burns are switched off. For the other kinds every list that holds one item
    for each case is emptied. Everything else in those records stays, as it does when the game
    ends an affliction itself. Raises EditError when the save fails the exact rebuild check,
    when one of the kinds is not stored the way this tool expects, when there is nothing to
    cure, and when the result differs from the original in anything but those values.
    """
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    found: Dict[str, Tuple[Dict[str, JsonValue], int]] = {}
    unreadable: List[str] = []
    for name in AFFLICTIONS:
        read = _affliction(save, name)
        if read is None:
            unreadable.append(name)
        else:
            found[name] = read
    if unreadable:
        raise EditError(", ".join(f"{name} ({AFFLICTIONS[name].record})" for name in unreadable)
                        + (" is" if len(unreadable) == 1 else " are")
                        + " not stored the way this tool expects, so nothing is cured")
    order = list(save.entries["global"])
    cured: List[Tuple[str, int]] = []
    expected: List[Difference] = []
    for name in sorted(found, key=lambda kind: order.index(AFFLICTIONS[kind].record)):
        (record, cases), kind = found[name], AFFLICTIONS[name]
        if not cases:
            continue
        where = f"{codec.ENTRY_TABLE} > global > {kind.record}"
        if not kind.lists:
            record[_ACTIVE] = False
            expected.append((f"{where} > {_ACTIVE}", "true", "false"))
        for field in [held for held in record if held in kind.lists]:
            record[field] = []
            expected.append((f"{where} > {field}", f"{cases} items", "0 items"))
        cured.append((name, cases))
    if not cured:
        names = list(AFFLICTIONS)
        raise EditError("there is nothing to cure: this save holds no " + ", ".join(names[:-1])
                        + " or " + names[-1])
    edited = codec.encode_save(save)
    entries, unchanged = _prove_changes(original, edited, expected)
    return CureEdit(edited, tuple(cured), len(expected), entries, unchanged)


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

_PLAYER_MANAGER = "m_PlayerManagerSerialized"
_POSITION = "m_SaveGamePosition"
_ROPE_RECORD = "m_PlayerClimbRopeSerialized"
_ROPE = "m_RopeGuid"
AXES = ("x", "y", "z")    # the order of the three numbers in the save. y is the height

# States in which this tool does not move the character: (the switch in the player's record, what it means).
_HELD_IN_PLACE = (("m_PlayerInVehicle", "in a vehicle"), ("m_PlayerInSnowShelter", "in a snow shelter"))


@dataclass(frozen=True)
class Position:
    scene: Optional[str]                      # the scene the save is in. None: the save does not say
    stored: Optional[Tuple[str, str, str]]    # x, y and z as the save holds them. None: they cannot be read
    held: Optional[str]                       # why this tool does not move the character: "in a vehicle"


def _scene(save: DecodedSave) -> Optional[str]:
    boot = save.entries.get("boot")
    name = boot.get("m_SceneName") if isinstance(boot, dict) else None
    return name if isinstance(name, str) and name else None


def _player_manager(save: DecodedSave) -> Optional[Dict[str, JsonValue]]:
    document = save.entries.get("global")
    record = document.get(_PLAYER_MANAGER) if isinstance(document, dict) else None
    content = record.value if isinstance(record, NestedJson) else None
    return content if isinstance(content, dict) else None


def _position_numbers(save: DecodedSave) -> Optional[List[JsonValue]]:
    """The list of the three numbers of the position. None when it is not stored as expected."""
    manager = _player_manager(save)
    held = manager.get(_POSITION) if manager is not None else None
    if not isinstance(held, list) or len(held) != len(AXES):
        return None
    return held if all(_finite(number) is not None for number in held) else None


def _held_in_place(save: DecodedSave) -> Optional[str]:
    """Why this tool does not move the character. None when nothing stands in the way."""
    manager = _player_manager(save)
    for switch, meaning in _HELD_IN_PLACE:
        # The game leaves a switch out of the save while it is off.
        state = manager.get(switch, False) if manager is not None else False
        if state is not False:
            return meaning if state is True else f"in a state this tool cannot read ({switch})"
    document = save.entries.get("global")
    record = document.get(_ROPE_RECORD) if isinstance(document, dict) else None
    content = record.value if isinstance(record, NestedJson) else None
    rope = content.get(_ROPE) if isinstance(content, dict) else None
    return "on a rope" if isinstance(rope, str) and rope else None


def read_position(save: DecodedSave) -> Position:
    """The scene the save is in, and where the character stands in it."""
    numbers = _position_numbers(save)
    stored = None if numbers is None else (_show(numbers[0]), _show(numbers[1]), _show(numbers[2]))
    return Position(_scene(save), stored, _held_in_place(save))


@dataclass(frozen=True)
class PositionEdit:
    data: bytes                                  # the edited save
    scene: Optional[str]                         # the scene the save is in. It is not changed
    changes: Tuple[Tuple[str, str, str], ...]    # (x, y or z, the number as stored, the new number)
    kept: Tuple[Tuple[str, str], ...]            # (x, y or z, the number as stored) where the new one is equal
    entries: int                                 # entries in the save
    unchanged_entries: int                       # entries that are byte for byte the original


def _check_coordinate(axis: str, value: float) -> None:
    if not -MAX_COORDINATE <= value <= MAX_COORDINATE:
        raise EditError(f"{axis} must be a number from -{MAX_COORDINATE:g} to {MAX_COORDINATE:g}, "
                        f"not {value:g}")


def stored_coordinate(axis: str, value: float) -> str:
    """One number of a position the way this tool writes it into a save."""
    _check_coordinate(axis, value)
    if 0 < abs(value) < 0.0001:
        raise EditError(f"{axis} {value!r} is too close to 0 for this tool to write. Use 0")
    return float32_literal(abs(value) if value == 0 else value)


def set_position(original: bytes, values: Dict[str, float]) -> PositionEdit:
    """Return the save with the character moved inside the scene it is in. Reads and writes no file.

    values gives the new number for one or more of x, y and z. A number the save holds
    already stays as the game wrote it, and the edit goes ahead when one or more of the others
    changes. Raises EditError for a name that is no axis, for a number outside the limits, for
    a position that is not stored the way this tool expects, for a character in a vehicle, in
    a snow shelter or on a rope, when nothing would change, and when the result differs from
    the original in anything but the numbers asked for.
    """
    unknown = [name for name in values if name not in AXES]
    if unknown or not values:
        raise EditError("name one or more of " + ", ".join(AXES) + (f", not {unknown[0]}" if unknown else ""))
    for axis, value in values.items():
        _check_coordinate(axis, value)
    check = codec.verify_save(original)
    if not check.identical:
        raise EditError(f"this save does not pass the exact rebuild check, so it is not edited: {check.detail}")
    save = codec.decode_save(original)
    numbers = _position_numbers(save)
    if numbers is None:
        raise EditError(f"the position is not stored the way this tool expects ({_PLAYER_MANAGER} > "
                        f"{_POSITION}), so it is not changed")
    held = _held_in_place(save)
    if held is not None:
        raise EditError(f"the character is {held} in this save, so this tool does not move it. Leave that "
                        "state in the game, let the game save, and try again")
    changes: List[Tuple[str, str, str]] = []
    kept: List[Tuple[str, str]] = []
    expected: List[Difference] = []
    for index, axis in enumerate(AXES):
        if axis not in values:
            continue
        old = _show(numbers[index])
        if _as_float32(values[axis]) == _as_float32(float(old)):
            kept.append((axis, old))
            continue
        new = stored_coordinate(axis, values[axis])
        numbers[index] = Number(new)
        changes.append((axis, old, new))
        expected.append((f"{codec.ENTRY_TABLE} > global > {_PLAYER_MANAGER} > {_POSITION}[{index}]", old, new))
    if not changes:
        raise EditError("the character is at that position already. Nothing to change")
    edited = codec.encode_save(save)
    entries, unchanged = _prove_changes(original, edited, expected)
    return PositionEdit(edited, _scene(save), tuple(changes), tuple(kept), entries, unchanged)


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


def _run_player(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        values = read_player(codec.decode_save(data))
    except codec.SaveFormatError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    rows: List[Tuple[str, str, str]] = []
    for value in values:
        if value.shown is None:
            rows.append((value.name, "cannot be read",
                         f"{value.field} is not stored the way this tool expects"))
        elif value.stored is None:
            rows.append((value.name, _player_text(value.name, value.shown),
                         f"{value.field} is left out, which means 0"))
        else:
            rows.append((value.name, _player_text(value.name, value.shown), f"{value.field} {value.stored}"))
    width = max([len(shown) for _, shown, _ in rows] + [11])
    print(f"{'value':<9}  {'in the game':<{width}}  in the save")
    for name, shown, stored in rows:
        print(f"{name:<9}  {shown:<{width}}  {stored}")
    print(f"The player's {len(rows)} values in {path.name}.")
    print("For hydration, rest and warmth the save holds the inverse of the bar: 0 is a full bar, and 100 an "
          "empty one.")
    return 0


def _run_player_edit(path: Path, out: Path, values: Dict[str, float]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = set_player_values(data, values)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    count = len(edit.changes)
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    for name, old, new in edit.changes:
        before, after = (_player_text(name, _shown(name, Decimal(stored))) for stored in (old or "0", new))
        field = PLAYER_VALUES[name].field
        stored = f"stored as {field} {old} -> {new}" if old is not None \
            else f"the game had left {field} out of the save, which means 0. It is put in as {new}"
        print(f"Change:   {name} {before} -> {after} ({stored})")
    print("Checked:  the edited save differs from the original in exactly "
          + ("this one value" if count == 1 else f"these {count} values"))
    put_in = sum(old is None for _, old, _ in edit.changes)
    if put_in:
        print("Checked:  " + ("the number that was put in is" if put_in == 1 else
                              f"the {put_in} numbers that were put in are")
              + " the first in its record, where the game writes it, and nothing else in that record "
                "differs")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "set-player" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by set-player has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _run_skills(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        listed = list_skills(codec.decode_save(data))
    except codec.SaveFormatError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max(len(name) for name in SKILLS)
    print(f"{'skill':<{width}}  points  in the save")
    for skill in listed:
        if skill.points is None:
            points, stored = "", f"cannot be read: {skill.record} is not stored the way this tool expects"
        elif skill.stored is None:
            points, stored = "0", f"{_SKILL_POINTS} is left out, which means 0"
        else:
            points, stored = str(skill.points), f"{_SKILL_POINTS} {skill.stored}"
        print(f"{skill.name:<{width}}  {points:>6}  {stored}")
    print(f"The {len(listed)} skills in {path.name}.")
    print("The save holds points and no level. The game works the level out from the points.")
    return 0


def _run_skill_edit(path: Path, out: Path, skill: str, points: int) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = set_skill_points(data, skill, points)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Skill:    {edit.skill} ({SKILLS[edit.skill]})")
    if edit.old_points is None:
        print(f"Change:   points 0 -> {edit.new_points} (the game had left {_SKILL_POINTS} out of the save, "
              f"which means 0. It is put in as {edit.new_points})")
    else:
        print(f"Change:   points {edit.old_points} -> {edit.new_points}")
    print("Checked:  the edited save differs from the original in exactly this one value")
    if edit.old_points is None:
        print("Checked:  the number that was put in is the first in its record, where the game writes it, "
              "and nothing else in that record differs")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "set-skill" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by set-skill has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _run_afflictions(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        listed = list_afflictions(codec.decode_save(data))
    except codec.SaveFormatError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    width = max(len(name) for name in AFFLICTIONS)
    print(f"{'affliction':<{width}}  cases  in the save")
    for kind in listed:
        if kind.cases is None:
            print(f"{kind.name:<{width}}         cannot be read: {kind.record} is not stored the way this tool "
                  "expects")
        else:
            print(f"{kind.name:<{width}}  {kind.cases:>5}  {kind.record}")
    total = sum(kind.cases or 0 for kind in listed)
    print(f"{_count_text(total, 'case')} of these {len(listed)} kinds of affliction in {path.name}.")
    print("A save can hold other afflictions, such as frostbite or food poisoning. This tool does not read "
          "them.")
    return 0


def _run_cure(path: Path, out: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = cure_afflictions(data)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    values = "this one value" if edit.changes == 1 else f"these {edit.changes} values"
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    for name, cases in edit.cured:
        kind = AFFLICTIONS[name]
        how = f"{len(kind.lists)} lists emptied" if kind.lists else f"{_ACTIVE} true -> false"
        print(f"Cured:    {name}, {_count_text(cases, 'case')} ({kind.record}: {how})")
    print(f"Checked:  the edited save differs from the original in exactly {values}")
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "cure" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by cure has been loaded in the game yet. Try it on a save you can "
              "afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
    return 0


def _scene_text(scene: Optional[str]) -> str:
    return "the save does not name it" if scene is None else scene


def _run_position(path: Path) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        position = read_position(codec.decode_save(data))
    except codec.SaveFormatError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(f"Scene:     {_scene_text(position.scene)}")
    if position.stored is None:
        print(f"Position:  cannot be read: {_PLAYER_MANAGER} > {_POSITION} is not stored the way this tool "
              "expects")
    else:
        print("Position:  " + ", ".join(f"{axis} {number}" for axis, number in zip(AXES, position.stored)))
    if position.held is not None:
        print(f"Held:      the character is {position.held}, so set-position does not move it")
    print(f"Where the character stands in {path.name}. y is the height. The numbers count inside this scene "
          "only.")
    return 0


def _run_position_edit(path: Path, out: Path, values: Dict[str, float]) -> int:
    data = _read_save(path)
    if data is None:
        return 2
    try:
        check_new_file_path(out)
        edit = set_position(data, values)
        write_new_file(out, edit.data)
    except (EditError, codec.SaveFormatError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    count = len(edit.changes)
    print(f"Save:     {path.name}, {len(data):,} bytes, passes the exact rebuild check")
    print(f"Scene:    {_scene_text(edit.scene)}. It is not changed, and the numbers count inside it only")
    for axis, old, new in edit.changes:
        print(f"Change:   {axis} {old} -> {new}")
    for axis, old in edit.kept:
        print(f"Same:     {axis} is {old} already")
    print("Checked:  the edited save differs from the original in exactly "
          + ("this one value" if count == 1 else f"these {count} values"))
    print(f"Checked:  {edit.unchanged_entries} of {edit.entries} entries are byte for byte unchanged "
          "(all except global)")
    if "set-position" in NOT_TESTED_IN_GAME:
        print("Untested: no save changed by set-position has been loaded in the game yet. Try it on a save "
              "you can afford to lose")
    print(f"Written:  {out}, {len(edit.data):,} bytes")
    print(f"SHA-256:  {hashlib.sha256(edit.data).hexdigest()}")
    print(f"{path.name} itself was not changed.")
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
  python tld_save_edit.py player sandbox15
  python tld_save_edit.py set-player sandbox15 --condition 100 --hydration 100 --out sandbox15.edited
  python tld_save_edit.py skills sandbox15
  python tld_save_edit.py set-skill sandbox15 --skill cooking --points 150 --out sandbox15.edited
  python tld_save_edit.py afflictions sandbox15
  python tld_save_edit.py cure sandbox15 --out sandbox15.edited
  python tld_save_edit.py position sandbox15
  python tld_save_edit.py set-position sandbox15 --x -0.8 --y 0.02 --z -1.4 --out sandbox15.edited

Exit codes: 0 done, 1 refused (the reason is printed), 2 the save was not found or an
option was wrong.
        """,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (("items", "list everything in the player's inventory"),
                       ("stacks", "list the stacks in the player's inventory"),
                       ("liquids", "list the inventory items that hold liquid"),
                       ("foods", "list the inventory items with calories"),
                       ("weapons", "list the inventory items that can hold rounds"),
                       ("player", "list the player's condition, calories, hydration, rest and warmth"),
                       ("skills", "list the points of the nine skills"),
                       ("afflictions", "list hypothermia, sprain pain, sprained ankles and wrists, and burns"),
                       ("position", "print the scene the save is in and where the character stands")):
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
    set_player = commands.add_parser("set-player", help="change the player's condition, calories, hydration, "
                                                        "rest or warmth (not tested in the game yet)")
    set_player.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    for name, meaning, best in (("condition", "the player's condition in percent", ""),
                                ("calories", "the calories the player has in store", ""),
                                ("hydration", "the thirst bar in percent of a full bar", "not thirsty at all"),
                                ("rest", "the fatigue bar in percent of a full bar", "fully rested"),
                                ("warmth", "the cold bar in percent of a full bar", "warm")):
        limits = PLAYER_VALUES[name]
        unit = "CALORIES" if name == "calories" else "PERCENT"
        set_player.add_argument(f"--{name}", type=float, metavar=unit,
                                help=f"{meaning}, from {limits.low:g} to {limits.high:g}"
                                     + (f". {limits.high:g} is {best}" if best else ""))
    set_player.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                            help="the file to create. It must not exist yet")
    set_skill = commands.add_parser("set-skill", help="change the points of one skill "
                                                      "(not tested in the game yet)")
    set_skill.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    set_skill.add_argument("--skill", required=True, metavar="NAME",
                           help="the skill, as skills prints it: " + ", ".join(SKILLS))
    set_skill.add_argument("--points", required=True, type=int, metavar="N",
                           help=f"the new points, from 0 to {MAX_SKILL_POINTS}")
    set_skill.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                           help="the file to create. It must not exist yet")
    cure = commands.add_parser("cure", help="end hypothermia, sprain pain, sprained ankles and wrists, and "
                                            "burns (not tested in the game yet)")
    cure.add_argument("save", type=Path, metavar="SAVE", help="the save file to read. It is not changed")
    cure.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                      help="the file to create. It must not exist yet")
    set_position_ = commands.add_parser("set-position", help="move the character inside the scene it is in "
                                                             "(not tested in the game yet)")
    set_position_.add_argument("save", type=Path, metavar="SAVE",
                               help="the save file to read. It is not changed")
    for axis, meaning in zip(AXES, ("the new x", "the new y, which is the height", "the new z")):
        set_position_.add_argument(f"--{axis}", type=float, metavar=axis.upper(),
                                   help=f"{meaning}, from -{MAX_COORDINATE:g} to {MAX_COORDINATE:g}")
    set_position_.add_argument("--out", required=True, type=Path, metavar="NEW_FILE",
                               help="the file to create. It must not exist yet")
    args = parser.parse_args(argv)
    if args.command == "player":
        return _run_player(args.save)
    if args.command == "position":
        return _run_position(args.save)
    if args.command == "set-position":
        moved = {axis: getattr(args, axis) for axis in AXES if getattr(args, axis) is not None}
        if not moved:
            parser.error("set-position needs one or more of " + ", ".join(f"--{axis}" for axis in AXES))
        return _run_position_edit(args.save, args.out, moved)
    if args.command == "afflictions":
        return _run_afflictions(args.save)
    if args.command == "cure":
        return _run_cure(args.save, args.out)
    if args.command == "skills":
        return _run_skills(args.save)
    if args.command == "set-skill":
        return _run_skill_edit(args.save, args.out, args.skill, args.points)
    if args.command == "set-player":
        chosen = {name: getattr(args, name) for name in PLAYER_VALUES if getattr(args, name) is not None}
        if not chosen:
            parser.error("set-player needs one or more of " + ", ".join(f"--{name}" for name in PLAYER_VALUES))
        return _run_player_edit(args.save, args.out, chosen)
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
