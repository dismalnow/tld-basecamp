r"""Tests for tld_save_edit.py.

Run from this folder:
    python -m unittest -v

The tests build their own small saves, so they need no game files. One more test edits
real saves in memory only (no file is written). It is skipped unless TLD_SAVE_DIR names
a folder that holds them:
    $env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
    python -m unittest -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

import tld_save_codec as codec
import tld_save_edit as edit
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number
from tld_save_edit import (Affliction, EditError, Food, Item, Liquid, PlayerValue, Position, Skill, Stack,
                           Weapon)

# (item, count or None when the item is not a stack, condition)
ItemSpec = Tuple[str, Optional[str], str]

DEFAULT_ITEMS: Tuple[ItemSpec, ...] = (
    ("GEAR_WoodMatches", "23", "1.0"),
    ("GEAR_Hatchet", None, "0.75"),
    ("GEAR_BottleAntibiotics", "6", "0.5"),
    ("GEAR_BottleAntibiotics", "12", "0.25"),
    ("GEAR_Stick", "122", "1.0"),
)


def item(prefab: str, count: Optional[str], condition: str) -> Dict[str, JsonValue]:
    gear: Dict[str, JsonValue] = {
        "m_NormalizedCondition": Number(condition),
        "m_WeightKG": Number("250000000"),
        "m_InspectSerialized": None,
    }
    if count is not None:
        gear["m_StackableItemSerialized"] = NestedJson({"m_UnitsProxy": Number(count)})
    return {"m_PrefabName": prefab, "m_SerializedGear": NestedJson(gear)}


def with_count(items: Sequence[ItemSpec], entry: int, count: str) -> List[ItemSpec]:
    changed = list(items)
    changed[entry] = (changed[entry][0], count, changed[entry][2])
    return changed


def build_save(items: Sequence[ItemSpec] = DEFAULT_ITEMS, scene: str = "LakeRegion",
               global_document: Optional[Dict[str, JsonValue]] = None) -> bytes:
    if global_document is None:
        inventory = {"m_SerializedItems": [item(*spec) for spec in items], "m_QuickSelectInstanceIDs": []}
        global_document = {"m_Inventory_Serialized": NestedJson(inventory),
                           "m_SecondsSinceVoiceOver": Number("Infinity")}
    scene_items: List[JsonValue] = [
        {"m_PrefabName": "GEAR_Wood", "m_Position": [Number("1.5"), Number("-2.25E-05")]} for _ in range(30)]
    slot: Dict[str, JsonValue] = {
        "m_DisplayName": "Test save",
        "m_Dict": {"boot": {"m_SceneName": scene}, "global": global_document, scene: {"m_Items": scene_items}},
        "m_Version": Number("100"),
    }
    return codec.encode_save(DecodedSave(slot))


def entry_table(save_bytes: bytes) -> Dict[str, str]:
    return json.loads(codec.lzf_decompress(save_bytes))["m_Dict"]


def count_path(entry: int) -> str:
    return (f"m_Dict > global > m_Inventory_Serialized > m_SerializedItems[{entry}] > m_SerializedGear > "
            "m_StackableItemSerialized > m_UnitsProxy")


def with_condition(items: Sequence[ItemSpec], entry: int, condition: str) -> List[ItemSpec]:
    changed = list(items)
    changed[entry] = (changed[entry][0], changed[entry][1], condition)
    return changed


def condition_path(entry: int) -> str:
    return (f"m_Dict > global > m_Inventory_Serialized > m_SerializedItems[{entry}] > m_SerializedGear > "
            "m_NormalizedCondition")


def doctored_save(change) -> bytes:
    """The default save after change(list of inventory items) has altered it."""
    save = codec.decode_save(build_save())
    change(save.entries["global"]["m_Inventory_Serialized"].value["m_SerializedItems"])
    return codec.encode_save(save)


class ItemListTests(unittest.TestCase):
    def test_lists_every_item_in_save_order(self) -> None:
        items = edit.list_items(codec.decode_save(build_save()))
        self.assertEqual(items, [
            Item(0, "GEAR_WoodMatches", 23, 100.0),
            Item(1, "GEAR_Hatchet", None, 75.0),
            Item(2, "GEAR_BottleAntibiotics", 6, 50.0),
            Item(3, "GEAR_BottleAntibiotics", 12, 25.0),
            Item(4, "GEAR_Stick", 122, 100.0),
        ])


class ConditionLiteralTests(unittest.TestCase):
    def test_percent_becomes_a_fraction_in_the_game_s_form(self) -> None:
        cases = {100: "1.0", 75: "0.75", 62.5: "0.625", 33.3: "0.333", 1: "0.01", 85.6: "0.856",
                 99.99: "0.9999", 12.345678: "0.123456784"}
        for percent, literal in cases.items():
            with self.subTest(percent):
                self.assertEqual(edit.condition_literal(percent), literal)

    def test_floats_are_written_with_seven_digits_or_nine(self) -> None:
        # Literals copied from real saves. Each must come back exactly as the game wrote it.
        for literal in ("1.0", "0.856", "0.9206942", "0.9859133", "0.953999341", "0.200000077",
                        "0.698802352", "0.03829546", "0.650164545", "0.767226", "0.5"):
            with self.subTest(literal):
                self.assertEqual(edit.float32_literal(float(literal)), literal)


class SetConditionTests(unittest.TestCase):
    def test_changes_exactly_one_value(self) -> None:
        original = build_save()
        result = edit.set_item_condition(original, "GEAR_Hatchet", 100)
        self.assertEqual((result.prefab, result.entry, result.old_condition, result.new_condition),
                         ("GEAR_Hatchet", 1, "0.75", "1.0"))
        # Built independently, with the one condition changed.
        self.assertEqual(result.data, build_save(with_condition(DEFAULT_ITEMS, 1, "1.0")))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(condition_path(1), "0.75", "1.0")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_a_stack_keeps_its_count(self) -> None:
        result = edit.set_item_condition(build_save(), "GEAR_WoodMatches", 42.5)
        self.assertEqual(edit.list_items(codec.decode_save(result.data))[0],
                         Item(0, "GEAR_WoodMatches", 23, 42.5))

    def test_an_entry_number_picks_one_of_several(self) -> None:
        result = edit.set_item_condition(build_save(), "GEAR_BottleAntibiotics", 90, entry=3)
        self.assertEqual((result.entry, result.old_condition, result.new_condition), (3, "0.25", "0.9"))
        self.assertEqual(result.data, build_save(with_condition(DEFAULT_ITEMS, 3, "0.9")))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = build_save()
        copy = bytes(original)
        edit.set_item_condition(original, "GEAR_Hatchet", 100)
        self.assertEqual(original, copy)

    def test_requests_that_do_not_name_exactly_one_item_are_refused(self) -> None:
        several = ("holds 2 entries of GEAR_BottleAntibiotics: entry 2 (condition 50.0%), "
                   "entry 3 (condition 25.0%). Add --entry to say which one")
        cases = {
            "several of the item and no entry number": (("GEAR_BottleAntibiotics", 90, None), several),
            "a name that is close": (("GEAR_Hatchett", 90, None), "Did you mean GEAR_Hatchet?"),
            "a name that is nothing like": (("GEAR_Zzzzzzzz", 90, None), "Run items to see the item names"),
            "an entry number past the end": (("GEAR_Stick", 90, 99), "Entries run from 0 to 4"),
            "a negative entry number": (("GEAR_Stick", 90, -1), "there is no inventory entry -1"),
            "an entry number for another item": (("GEAR_Stick", 90, 1), "inventory entry 1 is GEAR_Hatchet, "
                                                                        "not GEAR_Stick. Run items again"),
            "the condition it already has": (("GEAR_Hatchet", 75, None), "already has a condition of 75%"),
            "a condition of zero": (("GEAR_Hatchet", 0, None), "from 1 to 100"),
            "a condition below the range": (("GEAR_Hatchet", 0.5, None), "from 1 to 100"),
            "a negative condition": (("GEAR_Hatchet", -5, None), "from 1 to 100"),
            "a condition above 100": (("GEAR_Hatchet", 100.5, None), "from 1 to 100"),
            "a condition that is not a number": (("GEAR_Hatchet", float("nan"), None), "from 1 to 100"),
        }
        original = build_save()
        for name, ((prefab, percent, entry), message) in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_item_condition(original, prefab, percent, entry)
                self.assertIn(message, str(caught.exception))

    def test_ruined_items_are_refused(self) -> None:
        def worn_out(items) -> None:
            items[1]["m_SerializedGear"].value["m_WornOut"] = True

        cases = {"condition 0": build_save(with_condition(DEFAULT_ITEMS, 1, "0.0")),
                 "marked worn out": doctored_save(worn_out)}
        for name, save in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_item_condition(save, "GEAR_Hatchet", 100)
                self.assertIn("inventory entry 1 (GEAR_Hatchet) is ruined", str(caught.exception))

    def test_a_condition_in_an_unknown_form_is_refused(self) -> None:
        def no_condition(items) -> None:
            del items[1]["m_SerializedGear"].value["m_NormalizedCondition"]

        def text_condition(items) -> None:
            items[1]["m_SerializedGear"].value["m_NormalizedCondition"] = "0.75"

        def plain_gear(items) -> None:
            items[1]["m_SerializedGear"] = None

        cases = {"above 1": build_save(with_condition(DEFAULT_ITEMS, 1, "75.0")),
                 "negative": build_save(with_condition(DEFAULT_ITEMS, 1, "-0.5")),
                 "exponent form": build_save(with_condition(DEFAULT_ITEMS, 1, "7.5E-01")),
                 "not a number": build_save(with_condition(DEFAULT_ITEMS, 1, "NaN")),
                 "missing": doctored_save(no_condition),
                 "text": doctored_save(text_condition),
                 "no gear record": doctored_save(plain_gear)}
        for name, save in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_item_condition(save, "GEAR_Hatchet", 100)
                self.assertIn("is not stored the way this tool expects", str(caught.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as caught:
            edit.set_item_condition(b"\xff\xff\xff", "GEAR_Hatchet", 100)
        self.assertIn("does not pass the exact rebuild check", str(caught.exception))

    def test_an_edit_that_writes_the_wrong_value_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save with 0.5 where 1.0 was asked for.
        original = build_save()
        wrong = build_save(with_condition(DEFAULT_ITEMS, 1, "0.5"))
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return encoded if encoded in (original, wrong) else wrong

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_item_condition(original, "GEAR_Hatchet", 100)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("0.75 -> 0.5", str(caught.exception))

    def test_an_edit_that_also_changes_the_count_is_caught(self) -> None:
        original = build_save()
        both = build_save(with_count(with_condition(DEFAULT_ITEMS, 0, "0.5"), 0, "24"))
        with self.assertRaises(EditError) as caught:
            edit._prove_single_change(original, both, (condition_path(0), "1.0", "0.5"))
        self.assertIn("m_UnitsProxy: 23 -> 24", str(caught.exception))


def liquid_item(prefab: str, record_field: Optional[str] = None,
                record: JsonValue = None) -> Dict[str, JsonValue]:
    """An inventory item with one record for the liquid it holds, or with none."""
    made = item(prefab, None, "1.0")
    if record_field is not None:
        made["m_SerializedGear"].value[record_field] = record        # type: ignore[union-attr,index]
    return made


def liquids_save(lamp_amount: str = "1E+09") -> bytes:
    """A save whose inventory holds liquid in every form real saves show, and items that hold none."""
    fuel = "m_LiquidItemSerialized"
    items: List[JsonValue] = [
        # 0: the water you carry
        liquid_item("GEAR_WaterSupplyPotable", "m_WaterSupplySerialized",
                    NestedJson({"m_VolumeProxy": Number("1904800000")})),
        # 1: a record without an amount
        liquid_item("GEAR_WaterSupplyNotPotable", "m_WaterSupplySerialized", NestedJson({})),
        # 2 and 3: two bottles of the same kind
        liquid_item("GEAR_LampFuel", fuel, NestedJson({"m_LiquidLitersProxy": Number("171669576"),
                                                       "m_LiquidQuality": "NonPotable"})),
        liquid_item("GEAR_LampFuel", fuel, NestedJson({"m_LiquidLitersProxy": Number("500000000"),
                                                       "m_LiquidQuality": "NonPotable"})),
        # 4: a lamp, with its fuel in the form the C# editor writes
        liquid_item("GEAR_KeroseneLampB", "m_KeroseneLampItemSerialized",
                    NestedJson({"m_HoursPlayed": Number("1583.43872"),
                                "m_CurrentFuelLitersProxy": Number(lamp_amount)})),
        # 5: an amount the game has not set
        liquid_item("GEAR_BottleHydrogenPeroxide", fuel,
                    NestedJson({"m_LiquidLitersProxy": Number("-214748364800000"),
                                "m_LiquidQuality": "NonPotable"})),
        # 6: no liquid at all. 7: an empty slot for a record. 8 and 9: amounts that are no number
        liquid_item("GEAR_Hatchet"),
        liquid_item("GEAR_JerrycanRusty", fuel, None),
        liquid_item("GEAR_Thermos", fuel, NestedJson({"m_LiquidLitersProxy": Number("NaN")})),
        liquid_item("GEAR_RecycledCan", fuel, NestedJson({"m_LiquidLitersProxy": "a lot"})),
    ]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})


def liquid_path(entry: int, fields: str) -> str:
    return (f"m_Dict > global > m_Inventory_Serialized > m_SerializedItems[{entry}] > m_SerializedGear > "
            f"{fields}")


def amount_in(save_bytes: bytes, entry: int, record_field: str, amount_field: str) -> JsonValue:
    items = codec.decode_save(save_bytes).entries["global"]["m_Inventory_Serialized"].value["m_SerializedItems"]
    return items[entry]["m_SerializedGear"].value[record_field].value[amount_field]


def liquid_literal(items: Sequence[JsonValue], liquid: Liquid) -> str:
    found = edit._liquid_of(items[liquid.entry])
    assert found is not None
    return found.literal


def check_ambiguous_liquid(case: unittest.TestCase, original: bytes, liquid: Liquid, literal: str) -> None:
    with case.assertRaises(EditError) as refused:
        edit.set_item_liters(original, liquid.prefab, Decimal("0.25"), entry=liquid.entry)
    case.assertEqual(str(refused.exception),
                     f"inventory entry {liquid.entry} ({liquid.prefab}) holds the amount {literal}, which is "
                     "not written the way the game writes it: as a whole number of billionths of a liter. "
                     "This tool cannot tell whether it means liters or billionths, so it is not changed. "
                     "Another editor may have written it")


class ExactLiquidTests(unittest.TestCase):
    def test_listed_amounts_with_ambiguous_units_are_refused(self) -> None:
        for literal in ("0.5", "3.0", "0.0", "4E+08", "4.112937E+08", "-2.14748365E+14"):
            with self.subTest(literal=literal):
                original = liquids_save(lamp_amount=literal)
                lamp = next(item for item in edit.list_liquids(codec.decode_save(original)) if item.entry == 4)
                check_ambiguous_liquid(self, original, lamp, literal)

    def test_new_amount_must_fit_exactly_in_billionths(self) -> None:
        original = liquids_save()
        target = "GEAR_BottleHydrogenPeroxide"
        for given, wanted in (("1.000000001", "1000000001"), ("2.500000000000", "2500000000")):
            result = edit.set_item_liters(original, target, Decimal(given))
            self.assertEqual((result.old_amount, result.new_amount), ("-214748364800000", wanted))
        for given in ("1.0000000000000000000000000001", "1e-1000100"):
            with self.subTest(given=given), self.assertRaisesRegex(EditError, "nine decimal places"):
                edit.set_item_liters(original, target, Decimal(given))


class LiquidListTests(unittest.TestCase):
    def test_lists_the_items_that_hold_liquid_in_save_order(self) -> None:
        self.assertEqual(edit.list_liquids(codec.decode_save(liquids_save())), [
            Liquid(0, "GEAR_WaterSupplyPotable", Decimal("1.9048")),
            Liquid(2, "GEAR_LampFuel", Decimal("0.171669576")),
            Liquid(3, "GEAR_LampFuel", Decimal("0.5")),
            Liquid(4, "GEAR_KeroseneLampB", Decimal("1")),
            Liquid(5, "GEAR_BottleHydrogenPeroxide", None),
        ])

    def test_a_save_without_liquid_gives_an_empty_list(self) -> None:
        self.assertEqual(edit.list_liquids(codec.decode_save(build_save())), [])

    def test_the_reader_and_the_editor_look_for_liquid_in_the_same_places(self) -> None:
        import tld_save_reader as reader
        self.assertEqual(edit._LIQUID_FIELDS, reader._LIQUID_FIELDS)


class StoredLitersTests(unittest.TestCase):
    def test_liters_become_whole_billionths(self) -> None:
        for liters, stored in (("0", "0"), ("1", "1000000000"), ("2.5", "2500000000"),
                               ("0.171669576", "171669576"), ("999", "999000000000"), ("1E+2", "100000000000")):
            with self.subTest(liters=liters):
                self.assertEqual(edit.stored_liters(Decimal(liters)), stored)

    def test_amounts_the_game_cannot_hold_are_refused(self) -> None:
        for liters in ("-0.1", "999.000000001", "1000", "0.0000000001", "NaN", "Infinity", "-Infinity"):
            with self.subTest(liters=liters):
                with self.assertRaises(EditError):
                    edit.stored_liters(Decimal(liters))


class SetLitersTests(unittest.TestCase):
    def test_changes_exactly_one_value(self) -> None:
        original = liquids_save()
        result = edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("2.5"))
        self.assertEqual((result.entry, result.old_amount, result.new_amount), (0, "1904800000", "2500000000"))
        self.assertEqual((result.old_liters, result.new_liters), (Decimal("1.9048"), Decimal("2.5")))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(liquid_path(0, "m_WaterSupplySerialized > m_VolumeProxy"),
                                  "1904800000", "2500000000")])
        self.assertEqual(amount_in(result.data, 0, "m_WaterSupplySerialized", "m_VolumeProxy"),
                         Number("2500000000"))
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_each_kind_of_liquid_record_can_be_set(self) -> None:
        for prefab, entry, fields, old in (
                ("GEAR_LampFuel", 3, "m_LiquidItemSerialized > m_LiquidLitersProxy", "500000000"),
                ("GEAR_KeroseneLampB", None, "m_KeroseneLampItemSerialized > m_CurrentFuelLitersProxy",
                 "1000000000")):
            with self.subTest(prefab=prefab):
                original = liquids_save(lamp_amount="1000000000")
                result = edit.set_item_liters(original, prefab, Decimal("0.25"), entry=entry)
                found = list(edit.differences(codec.decode_save(original).slot,
                                              codec.decode_save(result.data).slot))
                self.assertEqual(found, [(liquid_path(result.entry, fields), old, "250000000")])

    def test_an_amount_the_game_had_not_set_can_be_set(self) -> None:
        result = edit.set_item_liters(liquids_save(), "GEAR_BottleHydrogenPeroxide", Decimal("0.5"))
        self.assertIsNone(result.old_liters)
        self.assertEqual((result.old_amount, result.new_amount), ("-214748364800000", "500000000"))

    def test_zero_liters_is_allowed_and_is_not_the_same_as_not_set(self) -> None:
        result = edit.set_item_liters(liquids_save(), "GEAR_WaterSupplyPotable", Decimal("0"))
        self.assertEqual(result.new_amount, "0")
        self.assertEqual(edit.list_liquids(codec.decode_save(result.data))[0],
                         Liquid(0, "GEAR_WaterSupplyPotable", Decimal("0")))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = liquids_save()
        kept = bytes(original)
        edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("3"))
        self.assertEqual(original, kept)

    def test_requests_that_do_not_name_exactly_one_item_with_liquid_are_refused(self) -> None:
        cases = (
            ("GEAR_LampFuel", None, "holds 2 entries of GEAR_LampFuel with liquid: entry 2 (0.171669576), "
                                    "entry 3 (0.5). Add --entry"),
            ("GEAR_LampFuel", 0, "inventory entry 0 is GEAR_WaterSupplyPotable, not GEAR_LampFuel"),
            ("GEAR_LampFuel", 40, "there is no inventory entry 40. Entries run from 0 to 9"),
            ("GEAR_Lampfuel", None, "there is no GEAR_Lampfuel in the inventory. Did you mean GEAR_LampFuel?"),
            ("GEAR_Rifle", None, "there is no GEAR_Rifle in the inventory. Run liquids to see the item names."),
            ("GEAR_Hatchet", None, "GEAR_Hatchet is in the inventory, but the save holds no amount of liquid"),
            ("GEAR_Hatchet", 6, "inventory entry 6 is GEAR_Hatchet, and the save holds no amount of liquid"),
            ("GEAR_WaterSupplyNotPotable", None, "the save holds no amount of liquid for it"),
            ("GEAR_JerrycanRusty", None, "the save holds no amount of liquid for it"),
            ("GEAR_Thermos", None, "the save holds no amount of liquid for it"),
            ("GEAR_RecycledCan", 9, "the save holds no amount of liquid for it"),
        )
        for prefab, entry, message in cases:
            with self.subTest(prefab=prefab, entry=entry):
                with self.assertRaises(EditError) as refused:
                    edit.set_item_liters(liquids_save(), prefab, Decimal("1"), entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_the_amount_it_already_holds_is_refused(self) -> None:
        for prefab, liters in (("GEAR_WaterSupplyPotable", "1.9048"), ("GEAR_KeroseneLampB", "1")):
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.set_item_liters(liquids_save(lamp_amount="1000000000"), prefab, Decimal(liters))
                self.assertIn(f"already holds {liters} liters. Nothing to change", str(refused.exception))

    def test_amounts_out_of_range_are_refused_before_the_save_is_read(self) -> None:
        for liters in ("-1", "1000", "0.0000000001"):
            with self.subTest(liters=liters):
                with self.assertRaises(EditError):
                    edit.set_item_liters(b"not a save", "GEAR_WaterSupplyPotable", Decimal(liters))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_item_liters(b"not a save", "GEAR_WaterSupplyPotable", Decimal("1"))
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def test_an_edit_that_writes_the_wrong_value_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save with 3 liters where 2.5 were asked for.
        original = liquids_save()
        wrong = edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("3")).data
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return encoded if encoded in (original, wrong) else wrong

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("2.5"))
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("1904800000 -> 3000000000", str(caught.exception))

    def test_an_edit_that_also_changes_another_item_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save where the lamp fuel changed as well.
        original = liquids_save()
        first = edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("2.5")).data
        wrong = edit.set_item_liters(first, "GEAR_LampFuel", Decimal("1"), entry=2).data
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return wrong if encoded == first else encoded

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_item_liters(original, "GEAR_WaterSupplyPotable", Decimal("2.5"))
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))


def food_item(prefab: str, record: JsonValue = None, count: Optional[str] = None) -> Dict[str, JsonValue]:
    """An inventory item with a food record, or with an empty slot for one."""
    made = item(prefab, count, "1.0")
    made["m_SerializedGear"].value["m_FoodItemSerialized"] = record        # type: ignore[union-attr,index]
    return made


def food_record(left: str, full: str) -> NestedJson:
    return NestedJson({"m_CaloriesRemainingProxy": Number(left), "m_CaloriesTotal": Number(full),
                       "m_HoursPlayed": Number("213.6582"), "m_Opened": False})


def foods_save(left: Optional[Dict[int, str]] = None, full: Optional[Dict[int, str]] = None) -> bytes:
    """A save whose inventory holds food in the forms real saves show, and items without calories.

    left and full replace what is left, or the figure for an untouched one, in the entries they name.
    """
    items: List[JsonValue] = [
        # 0: more left than the figure for an untouched one, as two cooked fish have in a real save
        food_item("GEAR_CookedLakeWhiteFish", food_record("689.2364", "461.6905")),
        # 1 and 2: two of the same kind, the first partly eaten
        food_item("GEAR_MRE", food_record("846.509155", "1750.0")),
        food_item("GEAR_MRE", food_record("1750.0", "1750.0")),
        # 3: a stack
        food_item("GEAR_CookedBarPemmican", food_record("450.0", "450.0"), count="3"),
        # 4: calories in a form the game does not write
        food_item("GEAR_EnergyBar", food_record("2.5E+2", "500.0")),
        # 5: a record without the figure for an untouched one
        food_item("GEAR_GranolaBar", NestedJson({"m_CaloriesRemainingProxy": Number("29.59488")})),
        # 6: no food record. 7: an empty slot for one. 8: a record without calories
        item("GEAR_Hatchet", None, "1.0"),
        food_item("GEAR_Stone"),
        food_item("GEAR_Soda", NestedJson({"m_CaloriesTotal": Number("250.0")})),
        # 9 and 10: calories that are no number
        food_item("GEAR_CandyBar", NestedJson({"m_CaloriesRemainingProxy": Number("NaN"),
                                               "m_CaloriesTotal": Number("250.0")})),
        food_item("GEAR_PeanutButter", NestedJson({"m_CaloriesRemainingProxy": "900.0",
                                                   "m_CaloriesTotal": Number("900.0")})),
        # 11: a stack of one
        food_item("GEAR_CattailStalk", food_record("150.0", "150.0"), count="1"),
    ]
    for field, changes in (("m_CaloriesRemainingProxy", left), ("m_CaloriesTotal", full)):
        for entry, literal in (changes or {}).items():
            items[entry]["m_SerializedGear"].value["m_FoodItemSerialized"].value[field] = Number(literal)
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})


def calories_path(entry: int) -> str:
    return liquid_path(entry, "m_FoodItemSerialized > m_CaloriesRemainingProxy")


def calories_in(save_bytes: bytes, entry: int) -> JsonValue:
    return amount_in(save_bytes, entry, "m_FoodItemSerialized", "m_CaloriesRemainingProxy")


class FoodListTests(unittest.TestCase):
    def test_lists_the_items_with_calories_in_save_order(self) -> None:
        self.assertEqual(edit.list_foods(codec.decode_save(foods_save())), [
            Food(0, "GEAR_CookedLakeWhiteFish", None, Decimal("689.2364"), Decimal("461.6905")),
            Food(1, "GEAR_MRE", None, Decimal("846.509155"), Decimal("1750.0")),
            Food(2, "GEAR_MRE", None, Decimal("1750.0"), Decimal("1750.0")),
            Food(3, "GEAR_CookedBarPemmican", 3, Decimal("450.0"), Decimal("450.0")),
            Food(4, "GEAR_EnergyBar", None, Decimal("250"), Decimal("500.0")),
            Food(5, "GEAR_GranolaBar", None, Decimal("29.59488"), None),
            Food(11, "GEAR_CattailStalk", 1, Decimal("150.0"), Decimal("150.0")),
        ])

    def test_a_save_without_food_gives_an_empty_list(self) -> None:
        self.assertEqual(edit.list_foods(codec.decode_save(build_save())), [])

    def test_an_entry_without_a_name_is_left_out(self) -> None:
        save = codec.decode_save(foods_save())
        del edit._inventory_items(save)[0]["m_PrefabName"]
        self.assertEqual([food.entry for food in edit.list_foods(save)], [1, 2, 3, 4, 5, 11])


class SetCaloriesTests(unittest.TestCase):
    def test_changes_exactly_one_value(self) -> None:
        original = foods_save()
        result = edit.set_item_calories(original, "GEAR_CookedLakeWhiteFish", 500)
        self.assertEqual((result.prefab, result.entry, result.count, result.old_calories, result.new_calories),
                         ("GEAR_CookedLakeWhiteFish", 0, None, "689.2364", "500.0"))
        # Built independently, with the one amount changed.
        self.assertEqual(result.data, foods_save({0: "500.0"}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(calories_path(0), "689.2364", "500.0")])
        self.assertEqual(calories_in(result.data, 0), Number("500.0"))
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_the_amount_is_written_the_way_the_game_writes_a_float(self) -> None:
        # The last one has more digits than a 32-bit float holds: the nearest float is 1234.56787109375.
        cases = ((1750, "1750.0"), (62.5, "62.5"), (689.2364, "689.2364"), (846.509155, "846.509155"),
                 (120.000008, "120.000008"), (13.4822607, "13.4822607"), (1234.56789, "1234.56787"))
        for calories, literal in cases:
            with self.subTest(calories=calories):
                result = edit.set_item_calories(foods_save(), "GEAR_CattailStalk", calories)
                self.assertEqual(result.new_calories, literal)
                self.assertEqual(calories_in(result.data, 11), Number(literal))

    def test_floats_in_the_range_of_calories_are_written_with_seven_digits_or_nine(self) -> None:
        # Literals copied from real saves. Each must come back exactly as the game wrote it.
        for literal in ("689.2364", "688.2153", "846.509155", "310.302826", "1249.26721", "122.235107",
                        "13.4822607", "69.35612", "571.1395", "29.59488", "120.000008", "283.679169",
                        "5.0", "1750.0"):
            with self.subTest(literal):
                self.assertEqual(edit.float32_literal(float(literal)), literal)

    def test_a_stack_keeps_its_count(self) -> None:
        result = edit.set_item_calories(foods_save(), "GEAR_CookedBarPemmican", 300)
        self.assertEqual(result.count, 3)
        self.assertEqual(result.data, foods_save({3: "300.0"}))
        self.assertEqual(edit.list_foods(codec.decode_save(result.data))[3],
                         Food(3, "GEAR_CookedBarPemmican", 3, Decimal("300.0"), Decimal("450.0")))

    def test_an_entry_number_picks_one_of_several(self) -> None:
        result = edit.set_item_calories(foods_save(), "GEAR_MRE", 1750, entry=1)
        self.assertEqual((result.entry, result.old_calories, result.new_calories), (1, "846.509155", "1750.0"))
        self.assertEqual(result.data, foods_save({1: "1750.0"}))

    def test_more_than_the_figure_for_an_untouched_one_is_allowed(self) -> None:
        result = edit.set_item_calories(foods_save(), "GEAR_MRE", 2500, entry=2)
        self.assertEqual((result.old_calories, result.new_calories), ("1750.0", "2500.0"))
        self.assertEqual(result.data, foods_save({2: "2500.0"}))

    def test_an_amount_in_a_form_the_game_does_not_write_can_be_replaced(self) -> None:
        result = edit.set_item_calories(foods_save(), "GEAR_EnergyBar", 300)
        self.assertEqual((result.old_calories, result.new_calories), ("2.5E+2", "300.0"))

    def test_an_item_without_the_figure_for_an_untouched_one_can_be_set(self) -> None:
        result = edit.set_item_calories(foods_save(), "GEAR_GranolaBar", 300)
        self.assertEqual((result.entry, result.old_calories, result.new_calories), (5, "29.59488", "300.0"))

    def test_the_ends_of_the_range_are_allowed(self) -> None:
        for calories, literal in ((1, "1.0"), (9999, "9999.0")):
            with self.subTest(calories=calories):
                result = edit.set_item_calories(foods_save(), "GEAR_CookedLakeWhiteFish", calories)
                self.assertEqual(result.new_calories, literal)

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = foods_save()
        kept = bytes(original)
        edit.set_item_calories(original, "GEAR_CookedLakeWhiteFish", 500)
        self.assertEqual(original, kept)

    def test_requests_that_do_not_name_exactly_one_item_with_calories_are_refused(self) -> None:
        cases = (
            ("GEAR_MRE", None, "holds 2 entries of GEAR_MRE with calories: entry 1 (846.509155), "
                               "entry 2 (1750.0). Add --entry"),
            ("GEAR_MRE", 0, "inventory entry 0 is GEAR_CookedLakeWhiteFish, not GEAR_MRE. Run foods again"),
            ("GEAR_MRE", 40, "there is no inventory entry 40. Entries run from 0 to 11"),
            ("GEAR_Mre", None, "there is no GEAR_Mre in the inventory. Did you mean GEAR_MRE?"),
            ("GEAR_Rifle", None, "there is no GEAR_Rifle in the inventory. Run foods to see the item names."),
            ("GEAR_Hatchet", None, "GEAR_Hatchet is in the inventory, but the save holds no calories for it"),
            ("GEAR_Hatchet", 6, "inventory entry 6 is GEAR_Hatchet, and the save holds no calories for it"),
            ("GEAR_Stone", None, "the save holds no calories for it, so there are none to set"),
            ("GEAR_Soda", None, "the save holds no calories for it"),
            ("GEAR_CandyBar", None, "the save holds no calories for it"),
            ("GEAR_PeanutButter", 10, "the save holds no calories for it"),
        )
        for prefab, entry, message in cases:
            with self.subTest(prefab=prefab, entry=entry):
                with self.assertRaises(EditError) as refused:
                    edit.set_item_calories(foods_save(), prefab, 100, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_a_close_name_is_only_suggested_when_that_item_has_calories(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_item_calories(foods_save(), "GEAR_Hatchett", 100)
        self.assertIn("there is no GEAR_Hatchett in the inventory. Run foods to see the item names.",
                      str(refused.exception))

    def test_a_long_list_of_the_same_item_is_cut_short(self) -> None:
        items: List[JsonValue] = [food_item("GEAR_MRE", food_record(f"{100 + entry}.0", "1750.0"))
                                  for entry in range(10)]
        inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
        save = build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})
        with self.assertRaises(EditError) as refused:
            edit.set_item_calories(save, "GEAR_MRE", 500)
        self.assertIn("holds 10 entries of GEAR_MRE with calories: entry 0 (100.0), entry 1 (101.0), ",
                      str(refused.exception))
        self.assertIn("entry 7 (107.0) and 2 more. Add --entry to say which one", str(refused.exception))
        self.assertEqual(edit.set_item_calories(save, "GEAR_MRE", 500, entry=9).old_calories, "109.0")

    def test_the_amount_it_already_holds_is_refused(self) -> None:
        for prefab, calories, shown in (("GEAR_CookedLakeWhiteFish", 689.2364, "689.2364"),
                                        ("GEAR_EnergyBar", 250, "250.0")):
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.set_item_calories(foods_save(), prefab, calories)
                self.assertIn(f"already holds {shown} calories. Nothing to change", str(refused.exception))

    def test_amounts_out_of_range_are_refused_before_the_save_is_read(self) -> None:
        for calories in (0, 0.5, 0.999, -1, 9999.5, 10000, float("nan"), float("inf"), float("-inf")):
            with self.subTest(calories=calories):
                with self.assertRaises(EditError) as refused:
                    edit.set_item_calories(b"not a save", "GEAR_MRE", calories)
                self.assertIn("the calories must be a number from 1 to 9999, not ", str(refused.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_item_calories(b"not a save", "GEAR_MRE", 100)
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def test_an_edit_that_writes_the_wrong_value_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save with 300 calories where 500 were asked for.
        original = foods_save()
        wrong = foods_save({0: "300.0"})
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return encoded if encoded in (original, wrong) else wrong

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_item_calories(original, "GEAR_CookedLakeWhiteFish", 500)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("689.2364 -> 300.0", str(caught.exception))

    def test_an_edit_that_also_changes_the_figure_for_an_untouched_one_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save where m_CaloriesTotal changed as well.
        original = foods_save()
        meant = foods_save({0: "500.0"})
        wrong = foods_save({0: "500.0"}, full={0: "500.0"})
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return wrong if encoded == meant else encoded

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_item_calories(original, "GEAR_CookedLakeWhiteFish", 500)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("m_CaloriesTotal: 461.6905 -> 500.0", str(caught.exception))


def weapon_item(prefab: str, record: JsonValue = None) -> Dict[str, JsonValue]:
    """An inventory item with a weapon record, or with an empty slot for one."""
    made = item(prefab, None, "1.0")
    made["m_SerializedGear"].value["m_WeaponItemSerialized"] = record        # type: ignore[union-attr,index]
    return made


def clip(*rounds: str, spent: Optional[str] = None) -> NestedJson:
    """A weapon record as the game writes it: the rounds, then the spent casings when there are any."""
    record: Dict[str, JsonValue] = {"m_Clip": [Number(value) for value in rounds]}
    if spent is not None:
        record["m_SpentCasingsInClip"] = Number(spent)
    return NestedJson(record)


def weapons_save(clips: Optional[Dict[int, Sequence[str]]] = None) -> bytes:
    """A save whose inventory holds weapons in the forms real saves show, and items that hold no rounds.

    clips replaces the rounds of the entries it names.
    """
    items: List[JsonValue] = [
        # 0: a revolver with five rounds and one spent casing. 1: a second one with two rounds
        weapon_item("GEAR_Revolver", clip("100", "100", "100", "100", "100", spent="1")),
        weapon_item("GEAR_Revolver", clip("100", "100")),
        # 2: an empty rifle. 3: a full rifle of a named kind. 4: an empty distress pistol
        weapon_item("GEAR_Rifle", clip()),
        weapon_item("GEAR_Rifle_Vaughns", clip(*["100"] * 10)),
        weapon_item("GEAR_FlareGun", clip()),
        # 5: an item with a weapon record that the tool has no number for
        weapon_item("GEAR_Camera", clip()),
        # 6: a record in the form the C# editor writes, with rounds that are not all 100
        weapon_item("GEAR_RevolverGreen", NestedJson({
            "m_RoundsInClipProxy": Number("0"), "m_IsJammed": False,
            "m_Clip": [Number("100"), Number("87"), Number("55"), Number("9")],
            "m_SpentCasingsInClip": Number("0")})),
        # 7: a revolver that was fired until it was empty
        weapon_item("GEAR_RevolverFancy", clip(spent="6")),
        # 8: no weapon record. 9: an empty slot for one. 10: a record without a list of rounds
        item("GEAR_Hatchet", None, "1.0"),
        weapon_item("GEAR_Stone"),
        weapon_item("GEAR_Bow", NestedJson({"m_IsJammed": False})),
        # 11 to 13: rounds or spent casings that are no whole numbers
        weapon_item("GEAR_Rifle_Barbs", NestedJson({"m_Clip": [Number("100"), "full"]})),
        weapon_item("GEAR_Rifle_Curators", NestedJson({"m_Clip": [Number("100.5")]})),
        weapon_item("GEAR_RevolverStubNosed", NestedJson({"m_Clip": [], "m_SpentCasingsInClip": Number("-1")})),
    ]
    for entry, rounds in (clips or {}).items():
        record = items[entry]["m_SerializedGear"].value["m_WeaponItemSerialized"].value
        record["m_Clip"] = [Number(value) for value in rounds]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})


def clip_path(entry: int) -> str:
    return liquid_path(entry, "m_WeaponItemSerialized > m_Clip")


def rounds_in(save_bytes: bytes, entry: int) -> List[str]:
    return [value.literal for value in amount_in(save_bytes, entry, "m_WeaponItemSerialized", "m_Clip")]


class WeaponListTests(unittest.TestCase):
    def test_lists_the_items_that_can_hold_rounds_in_save_order(self) -> None:
        self.assertEqual(edit.list_weapons(codec.decode_save(weapons_save())), [
            Weapon(0, "GEAR_Revolver", 5, 1, 6),
            Weapon(1, "GEAR_Revolver", 2, 0, 6),
            Weapon(2, "GEAR_Rifle", 0, 0, 10),
            Weapon(3, "GEAR_Rifle_Vaughns", 10, 0, 10),
            Weapon(4, "GEAR_FlareGun", 0, 0, 1),
            Weapon(5, "GEAR_Camera", 0, 0, None),
            Weapon(6, "GEAR_RevolverGreen", 4, 0, 6),
            Weapon(7, "GEAR_RevolverFancy", 0, 6, 6),
        ])

    def test_a_save_without_weapons_gives_an_empty_list(self) -> None:
        self.assertEqual(edit.list_weapons(codec.decode_save(build_save())), [])

    def test_an_entry_without_a_name_is_left_out(self) -> None:
        save = codec.decode_save(weapons_save())
        del edit._inventory_items(save)[0]["m_PrefabName"]
        self.assertEqual([weapon.entry for weapon in edit.list_weapons(save)], [1, 2, 3, 4, 5, 6, 7])

    def test_what_each_weapon_holds(self) -> None:
        rifles = {name for name in edit.ROUNDS_HELD if name.startswith("GEAR_Rifle")}
        revolvers = {name for name in edit.ROUNDS_HELD if name.startswith("GEAR_Revolver")}
        self.assertEqual(rifles, {"GEAR_Rifle", "GEAR_Rifle_Barbs", "GEAR_Rifle_Curators", "GEAR_Rifle_Trader",
                                  "GEAR_Rifle_Vaughns"})
        self.assertEqual(revolvers, {"GEAR_Revolver", "GEAR_RevolverFancy", "GEAR_RevolverGreen",
                                     "GEAR_RevolverStubNosed"})
        self.assertEqual({edit.ROUNDS_HELD[name] for name in rifles}, {10})
        self.assertEqual({edit.ROUNDS_HELD[name] for name in revolvers}, {6})
        self.assertEqual(edit.ROUNDS_HELD["GEAR_FlareGun"], 1)
        self.assertEqual(len(edit.ROUNDS_HELD), len(rifles) + len(revolvers) + 1)

    def test_every_weapon_the_tool_has_a_number_for_is_a_firearm_in_the_item_table(self) -> None:
        import tld_taxonomy as taxonomy
        for prefab, holds in edit.ROUNDS_HELD.items():
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.TAXONOMY_LOOKUP[prefab][:2], ("Weapons", "Firearms"))
                self.assertGreater(holds, 0)


class SetRoundsTests(unittest.TestCase):
    def test_changes_exactly_the_list_of_rounds(self) -> None:
        original = weapons_save()
        result = edit.set_weapon_rounds(original, "GEAR_Revolver", 6, entry=1)
        self.assertEqual((result.prefab, result.entry, result.old_rounds, result.new_rounds, result.holds),
                         ("GEAR_Revolver", 1, 2, 6, 6))
        # Built independently, with the one list changed.
        self.assertEqual(result.data, weapons_save({1: ["100"] * 6}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(clip_path(1), "2 items", "6 items")])
        self.assertEqual(rounds_in(result.data, 1), ["100"] * 6)
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_each_kind_of_weapon_can_be_filled(self) -> None:
        for prefab, holds in (("GEAR_Rifle", 10), ("GEAR_FlareGun", 1)):
            with self.subTest(prefab=prefab):
                result = edit.set_weapon_rounds(weapons_save(), prefab, holds)
                self.assertEqual((result.old_rounds, result.new_rounds, result.holds), (0, holds, holds))
                self.assertEqual(rounds_in(result.data, result.entry), ["100"] * holds)

    def test_rounds_already_loaded_are_kept_when_more_are_added(self) -> None:
        result = edit.set_weapon_rounds(weapons_save(), "GEAR_RevolverGreen", 6)
        self.assertEqual(rounds_in(result.data, 6), ["100", "87", "55", "9", "100", "100"])
        self.assertEqual(result.data, weapons_save({6: ["100", "87", "55", "9", "100", "100"]}))

    def test_rounds_come_off_the_end_of_the_list(self) -> None:
        original = weapons_save()
        result = edit.set_weapon_rounds(original, "GEAR_RevolverGreen", 2)
        self.assertEqual(rounds_in(result.data, 6), ["100", "87"])
        self.assertEqual(result.data, weapons_save({6: ["100", "87"]}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(clip_path(6), "4 items", "2 items")])

    def test_a_weapon_can_be_emptied(self) -> None:
        result = edit.set_weapon_rounds(weapons_save(), "GEAR_Rifle_Vaughns", 0)
        self.assertEqual((result.old_rounds, result.new_rounds), (10, 0))
        self.assertEqual(result.data, weapons_save({3: []}))

    def test_spent_casings_are_left_alone(self) -> None:
        result = edit.set_weapon_rounds(weapons_save(), "GEAR_Revolver", 4, entry=0)
        self.assertEqual((result.old_rounds, result.new_rounds, result.holds), (5, 4, 6))
        self.assertEqual(result.data, weapons_save({0: ["100"] * 4}))
        self.assertEqual(amount_in(result.data, 0, "m_WeaponItemSerialized", "m_SpentCasingsInClip"),
                         Number("1"))
        self.assertEqual(edit.list_weapons(codec.decode_save(result.data))[0],
                         Weapon(0, "GEAR_Revolver", 4, 1, 6))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = weapons_save()
        kept = bytes(original)
        edit.set_weapon_rounds(original, "GEAR_Rifle", 10)
        self.assertEqual(original, kept)

    def test_rounds_must_fit_next_to_the_spent_casings(self) -> None:
        cases = (
            ("GEAR_Revolver", 0, 6, "GEAR_Revolver holds 6 rounds, and inventory entry 0 still has 1 spent "
                                    "casing in it, so at most 5 fit. Reload it in the game to drop the "
                                    "casings"),
            ("GEAR_RevolverFancy", None, 1, "GEAR_RevolverFancy holds 6 rounds, and inventory entry 7 still "
                                            "has 6 spent casings in it, so none fit. Reload it in the game"),
        )
        for prefab, entry, rounds, message in cases:
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.set_weapon_rounds(weapons_save(), prefab, rounds, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_as_many_rounds_as_fit_next_to_the_spent_casings_are_allowed(self) -> None:
        emptied = edit.set_weapon_rounds(weapons_save(), "GEAR_Revolver", 0, entry=0).data
        result = edit.set_weapon_rounds(emptied, "GEAR_Revolver", 5, entry=0)
        self.assertEqual((result.old_rounds, result.new_rounds), (0, 5))
        self.assertEqual(result.data, weapons_save())

    def test_a_weapon_with_more_spent_casings_than_it_holds_can_still_be_emptied(self) -> None:
        inventory: Dict[str, JsonValue] = {
            "m_SerializedItems": [weapon_item("GEAR_Revolver", clip("100", spent="9"))],
            "m_QuickSelectInstanceIDs": []}
        save = build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})
        with self.assertRaises(EditError) as refused:
            edit.set_weapon_rounds(save, "GEAR_Revolver", 2)
        self.assertIn("still has 9 spent casings in it, so none fit", str(refused.exception))
        self.assertEqual(rounds_in(edit.set_weapon_rounds(save, "GEAR_Revolver", 0).data, 0), [])

    def test_more_than_the_weapon_holds_is_refused(self) -> None:
        cases = (("GEAR_Revolver", 1, 7, "GEAR_Revolver holds 6 rounds, so 7 do not fit"),
                 ("GEAR_Rifle", None, 11, "GEAR_Rifle holds 10 rounds, so 11 do not fit"),
                 ("GEAR_FlareGun", None, 2, "GEAR_FlareGun holds 1 round, so 2 do not fit"))
        for prefab, entry, rounds, message in cases:
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.set_weapon_rounds(weapons_save(), prefab, rounds, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_an_item_the_tool_has_no_number_for_is_refused(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_weapon_rounds(weapons_save(), "GEAR_Camera", 1)
        self.assertIn("this tool has no number for the rounds GEAR_Camera holds, so it does not change them",
                      str(refused.exception))

    def test_requests_that_do_not_name_exactly_one_weapon_are_refused(self) -> None:
        nothing = "the save holds no rounds for it, so there are none to set"
        cases = (
            ("GEAR_Revolver", None, "holds 2 entries of GEAR_Revolver that can hold rounds: entry 0 "
                                    "(5 rounds), entry 1 (2 rounds). Add --entry"),
            ("GEAR_Revolver", 2, "inventory entry 2 is GEAR_Rifle, not GEAR_Revolver. Run weapons again"),
            ("GEAR_Revolver", 40, "there is no inventory entry 40. Entries run from 0 to 13"),
            ("GEAR_Camira", None, "there is no GEAR_Camira in the inventory. Did you mean GEAR_Camera?"),
            ("GEAR_Zzzzzzzz", None, "there is no GEAR_Zzzzzzzz in the inventory. Run weapons to see the item "
                                    "names."),
            ("GEAR_Hatchet", None, "GEAR_Hatchet is in the inventory, but " + nothing),
            ("GEAR_Hatchet", 8, "inventory entry 8 is GEAR_Hatchet, and " + nothing),
            ("GEAR_Stone", None, nothing),
            ("GEAR_Bow", None, nothing),
            ("GEAR_Rifle_Barbs", None, nothing),
            ("GEAR_Rifle_Curators", 12, nothing),
            ("GEAR_RevolverStubNosed", None, nothing),
        )
        for prefab, entry, message in cases:
            with self.subTest(prefab=prefab, entry=entry):
                with self.assertRaises(EditError) as refused:
                    edit.set_weapon_rounds(weapons_save(), prefab, 1, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_a_close_name_is_only_suggested_when_that_item_can_hold_rounds(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_weapon_rounds(weapons_save(), "GEAR_Hatchett", 1)
        self.assertIn("there is no GEAR_Hatchett in the inventory. Run weapons to see the item names.",
                      str(refused.exception))

    def test_the_number_it_already_holds_is_refused(self) -> None:
        loaded = edit.set_weapon_rounds(weapons_save(), "GEAR_FlareGun", 1).data
        cases = ((weapons_save(), "GEAR_Revolver", 1, 2, "already holds 2 rounds. Nothing to change"),
                 (weapons_save(), "GEAR_Rifle", None, 0, "already holds 0 rounds. Nothing to change"),
                 (loaded, "GEAR_FlareGun", None, 1, "already holds 1 round. Nothing to change"))
        for save, prefab, entry, rounds, message in cases:
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.set_weapon_rounds(save, prefab, rounds, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_a_negative_number_is_refused_before_the_save_is_read(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_weapon_rounds(b"not a save", "GEAR_Rifle", -1)
        self.assertIn("the number of rounds must be a whole number from 0 up, not -1", str(refused.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_weapon_rounds(b"not a save", "GEAR_Rifle", 1)
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def encode_as(self, meant: bytes, wrong: bytes):
        """Fault injection: an encoder that hands back wrong where the edit meant to write meant."""
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return wrong if encoded == meant else encoded

        return mock.patch.object(codec, "encode_save", side_effect=encode_wrongly)

    def test_an_edit_that_writes_the_wrong_number_of_rounds_is_caught(self) -> None:
        with self.encode_as(weapons_save({1: ["100"] * 6}), weapons_save({1: ["100"] * 5})):
            with self.assertRaises(EditError) as caught:
                edit.set_weapon_rounds(weapons_save(), "GEAR_Revolver", 6, entry=1)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("2 items -> 5 items", str(caught.exception))

    def test_an_edit_that_writes_a_wrong_round_is_caught(self) -> None:
        # The right number of rounds, so the two saves differ in that one list only. The last round is wrong.
        wrong = weapons_save({1: ["100", "100", "100", "100", "100", "99"]})
        with self.encode_as(weapons_save({1: ["100"] * 6}), wrong):
            with self.assertRaises(EditError) as caught:
                edit.set_weapon_rounds(weapons_save(), "GEAR_Revolver", 6, entry=1)
        self.assertIn("the rounds in the edited save are not the ones asked for", str(caught.exception))
        self.assertIn("'99'", str(caught.exception))

    def test_an_edit_that_changes_a_round_already_loaded_is_caught(self) -> None:
        meant = weapons_save({6: ["100", "87", "55", "9", "100", "100"]})
        wrong = weapons_save({6: ["100", "87", "55", "100", "100", "100"]})
        with self.encode_as(meant, wrong):
            with self.assertRaises(EditError) as caught:
                edit.set_weapon_rounds(weapons_save(), "GEAR_RevolverGreen", 6)
        self.assertIn("the rounds in the edited save are not the ones asked for", str(caught.exception))

    def test_an_edit_that_also_changes_another_weapon_is_caught(self) -> None:
        wrong = weapons_save({1: ["100"] * 6, 2: ["100"]})
        with self.encode_as(weapons_save({1: ["100"] * 6}), wrong):
            with self.assertRaises(EditError) as caught:
                edit.set_weapon_rounds(weapons_save(), "GEAR_Revolver", 6, entry=1)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))


class StackListTests(unittest.TestCase):
    def test_lists_the_stacks_in_save_order(self) -> None:
        stacks = edit.list_stacks(codec.decode_save(build_save()))
        self.assertEqual(stacks, [
            Stack(0, "GEAR_WoodMatches", 23, 100.0),
            Stack(2, "GEAR_BottleAntibiotics", 6, 50.0),
            Stack(3, "GEAR_BottleAntibiotics", 12, 25.0),
            Stack(4, "GEAR_Stick", 122, 100.0),
        ])

    def test_an_empty_stack_record_is_not_a_stack(self) -> None:
        # Saves written by the old C# editor carry "m_StackableItemSerialized": null on every other item.
        save = codec.decode_save(build_save())
        matches = save.entries["global"]["m_Inventory_Serialized"].value["m_SerializedItems"][0]
        matches["m_SerializedGear"].value["m_StackableItemSerialized"] = None
        self.assertEqual([stack.entry for stack in edit.list_stacks(save)], [2, 3, 4])

    def test_a_count_that_is_not_a_plain_whole_number_is_not_a_stack(self) -> None:
        for literal in ("1.5", "1E+01", "-3"):
            with self.subTest(literal):
                save = codec.decode_save(build_save([("GEAR_Odd", literal, "1.0")]))
                self.assertEqual(edit.list_stacks(save), [])


class DifferenceTests(unittest.TestCase):
    def diff(self, before: str, after: str) -> List[Tuple[str, str, str]]:
        return list(edit.differences(codec.parse_json(before), codec.parse_json(after)))

    def test_identical_documents_have_no_differences(self) -> None:
        text = '{"a":[1,{"b":"x"}],"c":"{\\"d\\":2.5E-05}"}'
        self.assertEqual(self.diff(text, text), [])

    def test_each_kind_of_difference_is_reported_with_its_place(self) -> None:
        cases = {
            "number": ('{"a":{"b":1}}', '{"a":{"b":2}}', [("a > b", "1", "2")]),
            "same value written another way": ('{"a":1.0}', '{"a":1.00}', [("a", "1.0", "1.00")]),
            "text": ('{"a":"x"}', '{"a":"y"}', [("a", '"x"', '"y"')]),
            "null to value": ('{"a":null}', '{"a":true}', [("a", "null", "true")]),
            "number to text": ('{"a":1}', '{"a":"1"}', [("a", "1", '"1"')]),
            "list item": ('{"a":[1,2,3]}', '{"a":[1,9,3]}', [("a[1]", "2", "9")]),
            "list length": ('{"a":[1,2]}', '{"a":[1,2,3]}', [("a", "2 items", "3 items")]),
            "field added": ('{"a":1}', '{"a":1,"b":2}', [("", "fields a", "fields a, b")]),
            "field order": ('{"a":1,"b":2}', '{"b":2,"a":1}', [("", "fields a, b", "fields b, a")]),
            "inside a nested document": ('{"a":"{\\"b\\":[1,2]}"}', '{"a":"{\\"b\\":[1,5]}"}',
                                         [("a > b[1]", "2", "5")]),
            "nested document to plain text": ('{"a":"{\\"b\\":1}"}', '{"a":"plain"}',
                                              [("a", "a nested document", '"plain"')]),
            "two at once": ('{"a":1,"b":[1,2]}', '{"a":3,"b":[1,4]}', [("a", "1", "3"), ("b[1]", "2", "4")]),
        }
        for name, (before, after, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.diff(before, after), expected)

    def test_matched_positions_of_one_list_are_compared_in_place_of_all_of_them(self) -> None:
        before = codec.parse_json('{"a":[10,20,30,40],"b":[1,2],"c":"{\\"a\\":[5,6]}"}')
        after = codec.parse_json('{"a":[10,30,41],"b":[1,3],"c":"{\\"a\\":[5,7]}"}')
        length, second, nested = ("a", "4 items", "3 items"), ("b[1]", "2", "3"), ("c > a[1]", "6", "7")
        cases = {
            "no list is matched": (None, [length, second, nested]),
            # The length of the list is not looked at, and each pair goes by its position in the first list.
            "the first list": (("a", [(0, 0), (2, 1), (3, 2)]), [("a[3]", "40", "41"), second, nested]),
            "pairs that cross": (("b", [(0, 1), (1, 0)]),
                                 [length, ("b[0]", "1", "3"), ("b[1]", "2", "1"), nested]),
            "no pairs at all": (("a", []), [second, nested]),
            # The list of the same name inside the nested document has a path of its own.
            "a list inside a nested document": (("c > a", [(1, 0)]), [length, second, ("c > a[1]", "6", "5")]),
            "a path that is not there": (("d", [(0, 0)]), [length, second, nested]),
        }
        for name, (matched, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(list(edit.differences(before, after, matched=matched)), expected)

    def test_the_matched_list_may_be_below_another_list(self) -> None:
        before = codec.parse_json('{"x":[{"a":[1,2]},{"a":[3,4,5]}]}')
        after = codec.parse_json('{"x":[{"a":[1,2]},{"a":[5,3]}]}')
        self.assertEqual(list(edit.differences(before, after)), [("x[1] > a", "3 items", "2 items")])
        self.assertEqual(list(edit.differences(before, after, matched=("x[1] > a", [(0, 1), (2, 0)]))), [])
        self.assertEqual(list(edit.differences(before, after, matched=("x[1] > a", [(1, 1)]))),
                         [("x[1] > a[1]", "4", "3")])

    def test_a_list_inside_a_matched_pair_is_compared_position_by_position(self) -> None:
        before = codec.parse_json('{"a":[{"a":[1,2]},{"a":[3]}]}')
        after = codec.parse_json('{"a":[{"a":[3,4]},{"a":[1,2,5]}]}')
        self.assertEqual(list(edit.differences(before, after, matched=("a", [(0, 1), (1, 0)]))),
                         [("a[0] > a", "2 items", "3 items"), ("a[1] > a", "1 items", "2 items")])


class SetCountTests(unittest.TestCase):
    def test_changes_exactly_one_value(self) -> None:
        original = build_save()
        result = edit.set_stack_count(original, "GEAR_WoodMatches", 40)
        self.assertEqual((result.prefab, result.entry, result.old_count, result.new_count),
                         ("GEAR_WoodMatches", 0, 23, 40))
        # Built independently, with the one count changed.
        self.assertEqual(result.data, build_save(with_count(DEFAULT_ITEMS, 0, "40")))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(count_path(0), "23", "40")])

    def test_untouched_entries_keep_their_bytes(self) -> None:
        original = build_save()
        result = edit.set_stack_count(original, "GEAR_WoodMatches", 40)
        before, after = entry_table(original), entry_table(result.data)
        self.assertEqual(list(before), list(after))
        self.assertEqual(before["boot"], after["boot"])
        self.assertEqual(before["LakeRegion"], after["LakeRegion"])
        self.assertNotEqual(before["global"], after["global"])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))

    def test_the_edited_save_rebuilds_exactly(self) -> None:
        result = edit.set_stack_count(build_save(), "GEAR_Stick", 9999)
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_an_entry_number_picks_one_of_several_stacks(self) -> None:
        result = edit.set_stack_count(build_save(), "GEAR_BottleAntibiotics", 30, entry=3)
        self.assertEqual((result.entry, result.old_count, result.new_count), (3, 12, 30))
        counts = {stack.entry: stack.count for stack in edit.list_stacks(codec.decode_save(result.data))}
        self.assertEqual(counts, {0: 23, 2: 6, 3: 30, 4: 122})

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = build_save()
        copy = bytes(original)
        edit.set_stack_count(original, "GEAR_WoodMatches", 40)
        self.assertEqual(original, copy)

    def test_requests_that_do_not_name_exactly_one_stack_are_refused(self) -> None:
        several = "holds 2 stacks of GEAR_BottleAntibiotics: entry 2 (count 6), entry 3 (count 12)"
        close = "there is no GEAR_WoodMatch in the inventory. Did you mean GEAR_WoodMatches?"
        cases = {
            "several stacks and no entry number": (("GEAR_BottleAntibiotics", 30, None), several),
            "a name that is close": (("GEAR_WoodMatch", 30, None), close),
            "a name that is nothing like": (("GEAR_Zzzzzzzz", 30, None), "Run stacks to see the item names"),
            "an item that is not a stack": (("GEAR_Hatchet", 30, None), "it is not a stack"),
            "an entry number past the end": (("GEAR_Stick", 30, 99), "there is no inventory entry 99. "
                                                                     "Entries run from 0 to 4"),
            "a negative entry number": (("GEAR_Stick", 30, -1), "there is no inventory entry -1"),
            "an entry number for another item": (("GEAR_Stick", 30, 1), "inventory entry 1 is GEAR_Hatchet, "
                                                                        "not GEAR_Stick"),
            "an entry number for an item that is not a stack": (("GEAR_Hatchet", 30, 1), "is not a stack"),
            "the count it already has": (("GEAR_WoodMatches", 23, None), "already has a count of 23"),
            "a count of zero": (("GEAR_WoodMatches", 0, None), "from 1 to 9999"),
            "a negative count": (("GEAR_WoodMatches", -5, None), "from 1 to 9999"),
            "a count that is too large": (("GEAR_WoodMatches", 10000, None), "from 1 to 9999"),
        }
        original = build_save()
        for name, ((prefab, count, entry), message) in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_stack_count(original, prefab, count, entry)
                self.assertIn(message, str(caught.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        def stored(data: bytes) -> bytes:
            out = bytearray()
            for start in range(0, len(data), 32):
                chunk = data[start:start + 32]
                out.append(len(chunk) - 1)
                out += chunk
            return bytes(out)

        other_compression = stored(codec.lzf_decompress(build_save()))
        self.assertFalse(codec.verify_save(other_compression).identical)
        for name, data in (("compressed another way", other_compression), ("not a save", b"\xff\xff\xff")):
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_stack_count(data, "GEAR_WoodMatches", 40)
                self.assertIn("does not pass the exact rebuild check", str(caught.exception))

    def test_an_inventory_in_an_unknown_form_is_refused(self) -> None:
        plain_inventory = {"m_SerializedItems": [item("GEAR_Stick", "3", "1.0")]}
        cases = {
            "no inventory": {"m_Hours": Number("1")},
            "inventory is not a nested document": {"m_Inventory_Serialized": plain_inventory},
            "inventory has no item list": {"m_Inventory_Serialized": NestedJson({"m_Other": []})},
            "item list is not a list": {"m_Inventory_Serialized": NestedJson({"m_SerializedItems": "x"})},
        }
        for name, global_document in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.set_stack_count(build_save(global_document=global_document), "GEAR_Stick", 5)
                self.assertIn("not stored the way this tool expects", str(caught.exception))

    def test_an_edit_that_writes_the_wrong_value_is_caught(self) -> None:
        # Fault injection: the encoder hands back a save with 77 where 40 was asked for.
        original = build_save()
        wrong = build_save(with_count(DEFAULT_ITEMS, 0, "77"))
        real_encode = codec.encode_save

        def encode_wrongly(save: DecodedSave) -> bytes:
            encoded = real_encode(save)
            return encoded if encoded in (original, wrong) else wrong

        with mock.patch.object(codec, "encode_save", side_effect=encode_wrongly):
            with self.assertRaises(EditError) as caught:
                edit.set_stack_count(original, "GEAR_WoodMatches", 40)
        self.assertIn("did not change exactly the one value asked for", str(caught.exception))
        self.assertIn("23 -> 77", str(caught.exception))


class SingleChangeProofTests(unittest.TestCase):
    """The check that stands between an edit and a written file, tried against doctored results."""

    def setUp(self) -> None:
        self.original = build_save()
        self.expected = (count_path(0), "23", "40")

    def assert_refused(self, doctored: bytes, message: str) -> None:
        with self.assertRaises(EditError) as caught:
            edit._prove_single_change(self.original, doctored, self.expected)
        self.assertIn(message, str(caught.exception))

    def test_the_intended_change_alone_passes(self) -> None:
        good = build_save(with_count(DEFAULT_ITEMS, 0, "40"))
        self.assertEqual(edit._prove_single_change(self.original, good, self.expected), (3, 2))

    def test_no_change_at_all_is_refused(self) -> None:
        self.assert_refused(self.original, "no difference at all")

    def test_a_second_changed_value_is_refused(self) -> None:
        two = build_save(with_count(with_count(DEFAULT_ITEMS, 0, "40"), 4, "121"))
        self.assert_refused(two, "did not change exactly the one value asked for")

    def test_the_right_place_with_the_wrong_value_is_refused(self) -> None:
        self.assert_refused(build_save(with_count(DEFAULT_ITEMS, 0, "41")), "23 -> 41")

    def test_a_change_in_another_item_is_refused(self) -> None:
        self.assert_refused(build_save(with_count(DEFAULT_ITEMS, 4, "40")), "m_SerializedItems[4]")

    def test_a_changed_condition_is_refused(self) -> None:
        items = list(with_count(DEFAULT_ITEMS, 0, "40"))
        items[0] = (items[0][0], items[0][1], "0.5")
        self.assert_refused(build_save(items), "m_NormalizedCondition")

    def test_a_removed_item_is_refused(self) -> None:
        self.assert_refused(build_save(with_count(DEFAULT_ITEMS, 0, "40")[:-1]), "5 items -> 4 items")

    def test_a_result_that_does_not_rebuild_exactly_is_refused(self) -> None:
        self.assert_refused(b"\xff\xff\xff", "does not rebuild exactly")

    def test_other_entries_must_keep_their_bytes_even_if_the_rebuild_check_were_fooled(self) -> None:
        # The right edit, but with the scene entry compressed another way: same content, other bytes.
        good = build_save(with_count(DEFAULT_ITEMS, 0, "40"))
        slot = json.loads(codec.lzf_decompress(good))
        scene = codec.lzf_decompress(base64.b64decode(slot["m_Dict"]["LakeRegion"]))
        stored = b"".join(bytes([len(scene[start:start + 32]) - 1]) + scene[start:start + 32]
                          for start in range(0, len(scene), 32))
        slot["m_Dict"]["LakeRegion"] = base64.b64encode(stored).decode("ascii")
        doctored = codec.lzf_compress(json.dumps(slot, separators=(",", ":")).encode("utf-8"))

        self.assert_refused(doctored, "does not rebuild exactly")
        fooled = codec.VerifyResult(True, 3, 4, "")
        with mock.patch.object(codec, "verify_save", return_value=fooled):
            self.assert_refused(doctored, "entries other than global changed: global, LakeRegion")


class SeveralChangesProofTests(unittest.TestCase):
    """The same check, for an edit that changes several values."""

    def setUp(self) -> None:
        self.original = build_save()
        self.expected = [(condition_path(1), "0.75", "1.0"), (condition_path(2), "0.5", "1.0")]

    def refusal(self, doctored: bytes) -> str:
        with self.assertRaises(EditError) as caught:
            edit._prove_changes(self.original, doctored, self.expected)
        return str(caught.exception)

    def with_conditions(self, changes: Dict[int, str]) -> bytes:
        items: Sequence[ItemSpec] = DEFAULT_ITEMS
        for entry, condition in changes.items():
            items = with_condition(items, entry, condition)
        return build_save(items)

    def test_the_intended_changes_alone_pass(self) -> None:
        good = self.with_conditions({1: "1.0", 2: "1.0"})
        self.assertEqual(edit._prove_changes(self.original, good, self.expected), (3, 2))

    def test_a_change_that_was_not_made_is_named(self) -> None:
        message = self.refusal(self.with_conditions({1: "1.0"}))
        self.assertIn("did not change exactly the 2 values asked for. Found 1 difference: "
                      "1 asked for and not made (", message)
        self.assertIn("m_SerializedItems[2] > m_SerializedGear > m_NormalizedCondition: 0.5 -> 1.0)", message)
        self.assertNotIn("not asked for (", message)

    def test_a_change_that_was_not_asked_for_is_named(self) -> None:
        message = self.refusal(self.with_conditions({1: "1.0", 2: "1.0", 3: "1.0"}))
        self.assertIn("Found 3 differences: 1 not asked for (", message)
        self.assertIn("m_SerializedItems[3] > m_SerializedGear > m_NormalizedCondition: 0.25 -> 1.0)", message)
        self.assertNotIn("asked for and not made", message)

    def test_a_wrong_new_value_counts_as_both(self) -> None:
        message = self.refusal(self.with_conditions({1: "1.0", 2: "0.9"}))
        self.assertIn("Found 2 differences: 1 not asked for (", message)
        self.assertIn("0.5 -> 0.9), 1 asked for and not made (", message)

    def test_no_change_at_all_is_refused(self) -> None:
        self.assertIn("Found 0 differences: 2 asked for and not made", self.refusal(self.original))

    def test_the_values_are_expected_in_save_order(self) -> None:
        good = self.with_conditions({1: "1.0", 2: "1.0"})
        with self.assertRaises(EditError) as caught:
            edit._prove_changes(self.original, good, list(reversed(self.expected)))
        self.assertIn("Found 2 differences: the same values in another order", str(caught.exception))

    def test_a_result_that_does_not_rebuild_exactly_is_refused(self) -> None:
        self.assertIn("does not rebuild exactly", self.refusal(b"\xff\xff\xff"))


def repair_save(repaired: Sequence[int] = ()) -> bytes:
    """A save with items below 100%, items at 100%, and items that a repair leaves alone.

    A repair changes entries 1, 2, 3, 16, 18, 19 and 22. Entries 17, 20, 21 and 23 are torches
    and flares that have burned.

    The entries named in repaired are built at 100%.
    """
    specs: List[ItemSpec] = list(DEFAULT_ITEMS) + [
        ("GEAR_FlareA", None, "0.0"),             # 5: ruined
        ("GEAR_Knife", None, "0.4"),              # 6: marked worn out below
        ("GEAR_Prybar", None, "75.0"),            # 7 to 10: conditions in forms the tool does not change
        ("GEAR_Hammer", None, "-0.5"),
        ("GEAR_Hacksaw", None, "7.5E-01"),
        ("GEAR_Hook", None, "NaN"),
        ("GEAR_Line", None, "0.75"),              # 11: its condition becomes text below
        ("GEAR_Tinder", None, "0.75"),            # 12: loses its gear record below
        ("GEAR_Arrow", None, "1"),                # 13 and 14: at 100% in other forms
        ("GEAR_Bow", None, "0.99999999"),
        ("GEAR_Stone", None, "0.5"),              # 15: loses its name below
        ("GEAR_Rope", "2", "0.9"),                # 16: below 100%, and a stack
        ("GEAR_Torch", None, "0.404903769"),      # 17: a torch that has burned, as a real save holds one
        ("GEAR_Torch", None, "0.8"),              # 18: a fresh torch below 100%
        ("GEAR_FlareA", None, "0.7"),             # 19: a fresh flare, in the form the C# editor writes
        ("GEAR_BlueFlare", None, "0.6"),          # 20: a flare that is burning
        ("GEAR_Torch", None, "0.5"),              # 21: a burn time that is no number
        ("GEAR_Torch", None, "0.6"),              # 22: the state Fresh, spelled out
        ("GEAR_Torch", None, "0.3"),              # 23: a torch record that is no record
        ("GEAR_Torch", None, "1.0"),              # 24: a torch that has burned and is at 100% all the same
    ]
    for entry in repaired:
        specs[entry] = (specs[entry][0], specs[entry][1], "1.0")
    items: List[JsonValue] = [item(*spec) for spec in specs]
    burned = NestedJson({"m_HoursPlayed": Number("301.523438"), "m_StateProxy": "Extinguished",
                         "m_ElapsedBurnMinutesProxy": Number("53.5586624")})
    lights: Dict[int, Tuple[str, JsonValue]] = {
        17: ("m_TorchItemSerialized", burned),
        18: ("m_TorchItemSerialized", NestedJson({"m_HoursPlayed": Number("35.7810822")})),
        19: ("m_FlareItemSerialized", NestedJson({"m_HoursPlayed": Number("158.37413"), "m_StateProxy": None,
                                                  "m_ElapsedBurnMinutesProxy": Number("0.0")})),
        20: ("m_FlareItemSerialized", NestedJson({"m_StateProxy": "Burning",
                                                  "m_ElapsedBurnMinutesProxy": Number("0.0")})),
        21: ("m_TorchItemSerialized", NestedJson({"m_ElapsedBurnMinutesProxy": "12"})),
        22: ("m_TorchItemSerialized", NestedJson({"m_StateProxy": "Fresh"})),
        23: ("m_TorchItemSerialized", "burned"),
        24: ("m_TorchItemSerialized", NestedJson({"m_StateProxy": "Extinguished",
                                                  "m_ElapsedBurnMinutesProxy": Number("30.0")})),
    }
    for entry, (field, record) in lights.items():
        items[entry]["m_SerializedGear"].value[field] = record                # type: ignore[index,union-attr]
    items[6]["m_SerializedGear"].value["m_WornOut"] = True                  # type: ignore[index,union-attr]
    items[11]["m_SerializedGear"].value["m_NormalizedCondition"] = "0.75"   # type: ignore[index,union-attr]
    items[12]["m_SerializedGear"] = None                                    # type: ignore[index]
    del items[15]["m_PrefabName"]                                           # type: ignore[union-attr]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)})


# The entries of repair_save() that a repair changes.
REPAIRED = (1, 2, 3, 16, 18, 19, 22)


def encoding_as(meant: bytes, wrong: bytes):
    """Fault injection: an encoder that hands back wrong where the edit meant to write meant."""
    real_encode = codec.encode_save

    def encode_wrongly(save: DecodedSave) -> bytes:
        encoded = real_encode(save)
        return wrong if encoded == meant else encoded

    return mock.patch.object(codec, "encode_save", side_effect=encode_wrongly)


class RepairTests(unittest.TestCase):
    def test_every_item_below_full_is_set_to_full(self) -> None:
        original = repair_save()
        result = edit.repair_inventory(original)
        self.assertEqual(result.repaired, ((1, "GEAR_Hatchet", "0.75"), (2, "GEAR_BottleAntibiotics", "0.5"),
                                           (3, "GEAR_BottleAntibiotics", "0.25"), (16, "GEAR_Rope", "0.9"),
                                           (18, "GEAR_Torch", "0.8"), (19, "GEAR_FlareA", "0.7"),
                                           (22, "GEAR_Torch", "0.6")))
        self.assertEqual((result.full, result.ruined, result.burned, result.unreadable), (5, 2, 4, 7))
        # Built independently, with those seven conditions at 100%.
        self.assertEqual(result.data, repair_save(repaired=REPAIRED))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(condition_path(1), "0.75", "1.0"), (condition_path(2), "0.5", "1.0"),
                                 (condition_path(3), "0.25", "1.0"), (condition_path(16), "0.9", "1.0"),
                                 (condition_path(18), "0.8", "1.0"), (condition_path(19), "0.7", "1.0"),
                                 (condition_path(22), "0.6", "1.0")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_it_repairs_what_set_condition_accepts_apart_from_burned_torches_and_flares(self) -> None:
        original = repair_save()
        repaired = {entry for entry, _, _ in edit.repair_inventory(original).repaired}
        for entry, one in enumerate(edit._inventory_items(codec.decode_save(original))):
            prefab = edit._prefab_of(one)
            with self.subTest(entry=entry):
                if prefab is None:
                    self.assertNotIn(entry, repaired)
                    continue
                try:
                    edit.set_item_condition(original, prefab, 100, entry=entry)
                    accepted = True
                except EditError:
                    accepted = False
                self.assertEqual(entry in repaired, accepted and entry not in (17, 20, 21, 23))

    def test_a_stack_keeps_its_count(self) -> None:
        result = edit.repair_inventory(repair_save())
        self.assertEqual(edit.list_items(codec.decode_save(result.data))[15],
                         Item(16, "GEAR_Rope", 2, 100.0))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = repair_save()
        kept = bytes(original)
        edit.repair_inventory(original)
        self.assertEqual(original, kept)

    def test_a_second_repair_finds_nothing_to_change(self) -> None:
        once = edit.repair_inventory(repair_save()).data
        with self.assertRaises(EditError) as refused:
            edit.repair_inventory(once)
        self.assertIn("there is nothing to repair in the inventory: 12 at 100%, 2 ruined, 4 burned torches or "
                      "flares, 7 with a condition this tool does not change. Nothing to change",
                      str(refused.exception))

    def test_an_inventory_at_full_is_refused(self) -> None:
        full = build_save([(prefab, count, "1.0") for prefab, count, _ in DEFAULT_ITEMS])
        with self.assertRaises(EditError) as refused:
            edit.repair_inventory(full)
        self.assertIn("5 at 100%, 0 ruined, 0 burned torches or flares, 0 with a condition this tool does not "
                      "change", str(refused.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.repair_inventory(b"not a save")
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def test_a_repair_that_misses_an_item_is_caught(self) -> None:
        with encoding_as(repair_save(repaired=REPAIRED), repair_save(repaired=REPAIRED[:-1])):
            with self.assertRaises(EditError) as caught:
                edit.repair_inventory(repair_save())
        self.assertIn("did not change exactly the 7 values asked for. Found 6 differences: "
                      "1 asked for and not made", str(caught.exception))
        self.assertIn("m_SerializedItems[22]", str(caught.exception))

    def test_a_repair_that_touches_a_ruined_item_is_caught(self) -> None:
        with encoding_as(repair_save(repaired=REPAIRED), repair_save(repaired=REPAIRED + (5,))):
            with self.assertRaises(EditError) as caught:
                edit.repair_inventory(repair_save())
        self.assertIn("Found 8 differences: 1 not asked for (", str(caught.exception))
        self.assertIn("m_SerializedItems[5] > m_SerializedGear > m_NormalizedCondition: 0.0 -> 1.0",
                      str(caught.exception))

    def test_a_repair_that_touches_a_burned_torch_is_caught(self) -> None:
        with encoding_as(repair_save(repaired=REPAIRED), repair_save(repaired=REPAIRED + (17,))):
            with self.assertRaises(EditError) as caught:
                edit.repair_inventory(repair_save())
        self.assertIn("Found 8 differences: 1 not asked for (", str(caught.exception))
        self.assertIn("m_SerializedItems[17] > m_SerializedGear > m_NormalizedCondition: 0.404903769 -> 1.0",
                      str(caught.exception))

    def test_a_repair_that_also_changes_a_count_is_caught(self) -> None:
        meant = repair_save(repaired=REPAIRED)
        save = codec.decode_save(meant)
        stack = edit._inventory_items(save)[16]["m_SerializedGear"].value["m_StackableItemSerialized"]
        stack.value["m_UnitsProxy"] = Number("3")
        wrong = codec.encode_save(save)
        with encoding_as(meant, wrong):
            with self.assertRaises(EditError) as caught:
                edit.repair_inventory(repair_save())
        self.assertIn("1 not asked for (", str(caught.exception))
        self.assertIn("m_UnitsProxy: 2 -> 3", str(caught.exception))


def removal_save(without: Optional[int] = None) -> bytes:
    """A save whose inventory holds items that may go, and items that the tool leaves in place.

    without builds the same save with one inventory entry left out.
    """
    made: List[Dict[str, JsonValue]] = [item(*spec) for spec in DEFAULT_ITEMS] + [
        item("GEAR_Charcoal", None, "1.0"),                # 5: in the player's hands
        item("GEAR_PackMatches", "13", "0.889"),           # 6: the last item the player held
        item("GEAR_WoolSocks", None, "0.7"),               # 7: worn
        item("GEAR_WoolSocks", None, "0.9"),               # 8: not worn
        item("GEAR_WaterSupplyPotable", None, "1.0"),      # 9 and 10: the water the player carries
        item("GEAR_WaterSupplyNotPotable", None, "1.0"),
        item("GEAR_Rope", None, "1.0"),                    # 11: its number is held in a place of unknown use
        item("GEAR_Knife", None, "1.0"),                   # 12: an instance number that is no whole number
        item("GEAR_Stone", None, "1.0"),                   # 13: an instance number that is too short
        item("GEAR_Prybar", None, "1.0"),                  # 14 and 15: two entries with the same number
        item("GEAR_Hammer", None, "1.0"),
        item("GEAR_Tinder", None, "1.0"),                  # 16: no gear record
        item("GEAR_Hook", None, "1.0"),                    # 17: its number is held as text in another place
        item("GEAR_Scarf", None, "0.5"),                   # 18: clothing with a worn mark that is no yes or no
    ]
    numbers = ("-1001000", "-1001001", "-1001002", "1474360", "-1001004", "-347910", "-509100", "-1001007",
               "-1001008", "-1001009", "-1001010", "-1001011", "12.5", "7", "-1001014", "-1001014", None,
               "-1001017", "-1001018")

    def gear(entry: int) -> Dict[str, JsonValue]:
        return made[entry]["m_SerializedGear"].value                           # type: ignore[union-attr]

    for entry, number in enumerate(numbers):
        if number is not None:
            gear(entry)["m_InstanceIDProxy"] = Number(number)
    gear(7)["m_ClothingItemSerialized"] = NestedJson({"m_WearingProxy": True, "m_HasEquippedData": True,
                                                      "m_EquippedLayerIndex": Number("1")})
    gear(8)["m_ClothingItemSerialized"] = NestedJson({"m_WearingProxy": False})
    gear(9)["m_WaterSupplySerialized"] = NestedJson({"m_VolumeProxy": Number("2000000000")})
    gear(10)["m_WaterSupplySerialized"] = NestedJson({})
    gear(18)["m_ClothingItemSerialized"] = NestedJson({"m_WearingProxy": "yes"})
    made[16]["m_SerializedGear"] = None
    if without is not None:
        del made[without]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": list(made),
                                       "m_QuickSelectInstanceIDs": [Number("0") for _ in range(4)]}
    return build_save(global_document={
        "m_Inventory_Serialized": NestedJson(inventory),
        "m_PlayerManagerSerialized": NestedJson({"m_ItemInHandsInstanceID": Number("-347910"),
                                                 "m_LastUnequippedItemInstanceID": Number("-509100")}),
        "m_Unknown": NestedJson({"m_Target": Number("-1001011"), "m_Name": "-1001017",
                                 "m_OtherSign": Number("1001004")}),
    })


ITEMS_PATH = "m_Dict > global > m_Inventory_Serialized > m_SerializedItems"


class RemoveTests(unittest.TestCase):
    def test_removes_exactly_one_entry(self) -> None:
        original = removal_save()
        result = edit.remove_item(original, "GEAR_Hatchet")
        self.assertEqual((result.prefab, result.entry, result.count, result.condition, result.remaining),
                         ("GEAR_Hatchet", 1, None, 75.0, 18))
        # Built independently, without that one entry.
        self.assertEqual(result.data, removal_save(without=1))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(ITEMS_PATH, "19 items", "18 items")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_a_stack_goes_as_a_whole(self) -> None:
        result = edit.remove_item(removal_save(), "GEAR_BottleAntibiotics", entry=3)
        self.assertEqual((result.entry, result.count, result.condition), (3, 12, 25.0))
        self.assertEqual(result.data, removal_save(without=3))

    def test_the_first_entry_can_go(self) -> None:
        result = edit.remove_item(removal_save(), "GEAR_WoodMatches")
        self.assertEqual(result.data, removal_save(without=0))

    def test_the_same_digits_with_the_other_sign_are_another_number(self) -> None:
        # The stick is number -1001004, and another place in the save holds 1001004.
        result = edit.remove_item(removal_save(), "GEAR_Stick")
        self.assertEqual(result.data, removal_save(without=4))

    def test_clothing_that_is_not_worn_can_go(self) -> None:
        result = edit.remove_item(removal_save(), "GEAR_WoolSocks", entry=8)
        self.assertEqual(result.data, removal_save(without=8))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = removal_save()
        kept = bytes(original)
        edit.remove_item(original, "GEAR_Hatchet")
        self.assertEqual(original, kept)

    def test_items_the_tool_leaves_in_place(self) -> None:
        elsewhere = "the save holds its instance number in another place too: m_Dict > global > "
        cases = (
            ("GEAR_Charcoal", None, 5, "it is in your hands in this save. Put it away in the game first"),
            ("GEAR_PackMatches", None, 6, "it is the last item you held, and the save remembers it by its "
                                          "number. Hold another item in the game and put that away first"),
            ("GEAR_WoolSocks", 7, 7, "you are wearing it in this save. Take it off in the game first"),
            ("GEAR_WaterSupplyPotable", None, 9, "the game keeps the water you carry in this entry. Use "
                                                 "set-liters to empty it"),
            ("GEAR_WaterSupplyNotPotable", None, 10, "the game keeps the water you carry in this entry"),
            ("GEAR_Rope", None, 11, elsewhere + "m_Unknown > m_Target"),
            ("GEAR_Hook", None, 17, elsewhere + "m_Unknown > m_Name"),
            ("GEAR_Knife", None, 12, "its instance number is not stored the way this tool expects"),
            ("GEAR_Stone", None, 13, "its instance number, 7, is too short to tell apart from other numbers"),
            ("GEAR_Prybar", None, 14, elsewhere + "m_Inventory_Serialized > m_SerializedItems[15] > "
                                                  "m_SerializedGear > m_InstanceIDProxy"),
            ("GEAR_Hammer", None, 15, elsewhere + "m_Inventory_Serialized > m_SerializedItems[14] > "
                                                  "m_SerializedGear > m_InstanceIDProxy"),
            ("GEAR_Tinder", None, 16, "its instance number is not stored the way this tool expects"),
            ("GEAR_Scarf", None, 18, "whether you are wearing it is not stored the way this tool expects"),
        )
        for prefab, entry, chosen, reason in cases:
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.remove_item(removal_save(), prefab, entry=entry)
                self.assertIn(f"inventory entry {chosen} ({prefab}) is not removed: {reason}",
                              str(refused.exception))

    def test_requests_that_do_not_name_exactly_one_item_are_refused(self) -> None:
        cases = (("GEAR_BottleAntibiotics", None, "holds 2 entries of GEAR_BottleAntibiotics"),
                 ("GEAR_WoolSocks", None, "Add --entry to say which one"),
                 ("GEAR_Hatchett", None, "Did you mean GEAR_Hatchet?"),
                 ("GEAR_Hatchet", 2, "inventory entry 2 is GEAR_BottleAntibiotics, not GEAR_Hatchet"),
                 ("GEAR_Hatchet", 40, "Entries run from 0 to 18"))
        for prefab, entry, message in cases:
            with self.subTest(prefab=prefab, entry=entry):
                with self.assertRaises(EditError) as refused:
                    edit.remove_item(removal_save(), prefab, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.remove_item(b"not a save", "GEAR_Hatchet")
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def test_a_removal_of_another_item_is_caught(self) -> None:
        # The right number of items, so the two saves differ in the length of that one list only.
        with encoding_as(removal_save(without=1), removal_save(without=4)):
            with self.assertRaises(EditError) as caught:
                edit.remove_item(removal_save(), "GEAR_Hatchet")
        self.assertIn("the inventory in the edited save is not the original one without entry 1: the item now "
                      "at entry 1 is not the one that belongs there", str(caught.exception))

    def test_a_removal_that_also_changes_another_item_is_caught(self) -> None:
        meant = removal_save(without=1)
        save = codec.decode_save(meant)
        edit._inventory_items(save)[10]["m_SerializedGear"].value["m_NormalizedCondition"] = Number("0.5")
        wrong = codec.encode_save(save)
        with encoding_as(meant, wrong):
            with self.assertRaises(EditError) as caught:
                edit.remove_item(removal_save(), "GEAR_Hatchet")
        self.assertIn("the item now at entry 10 is not the one that belongs there", str(caught.exception))

    def test_a_removal_that_removes_nothing_is_caught(self) -> None:
        with encoding_as(removal_save(without=1), removal_save()):
            with self.assertRaises(EditError) as caught:
                edit.remove_item(removal_save(), "GEAR_Hatchet")
        self.assertIn("no difference at all", str(caught.exception))

    def test_a_removal_of_two_items_is_caught(self) -> None:
        save = codec.decode_save(removal_save(without=1))
        del edit._inventory_items(save)[0]
        two_gone = codec.encode_save(save)
        with encoding_as(removal_save(without=1), two_gone):
            with self.assertRaises(EditError) as caught:
                edit.remove_item(removal_save(), "GEAR_Hatchet")
        self.assertIn("19 items -> 17 items", str(caught.exception))

    def test_the_check_of_the_inventory_counts_the_items(self) -> None:
        with self.assertRaises(EditError) as caught:
            edit._prove_removal(removal_save(), removal_save(), 1)
        self.assertIn("without entry 1: 19 items where 18 were expected", str(caught.exception))
        edit._prove_removal(removal_save(), removal_save(without=18), 18)
        edit._prove_removal(removal_save(), removal_save(without=0), 0)
        # Two items gone from the end: every item that is left sits where it belongs, and one is missing.
        save = codec.decode_save(removal_save(without=18))
        del edit._inventory_items(save)[17]
        with self.assertRaises(EditError) as caught:
            edit._prove_removal(removal_save(), codec.encode_save(save), 18)
        self.assertIn("without entry 18: 17 items where 18 were expected", str(caught.exception))


GUID = "3caf3e38-6044-4544-97b3-72c2273efa1f"


def copy_save(copy_of: Optional[int] = None, number: str = "-5240390") -> bytes:
    """A save whose inventory holds items that may be copied, and items that the tool does not copy.

    copy_of builds the same save with a copy of that entry at the end, under the given instance number.
    """
    def worn(mark: JsonValue) -> NestedJson:
        return NestedJson({"m_WearingProxy": mark})

    def make(entry: int) -> Dict[str, JsonValue]:
        # Built anew on every call, so a copy shares nothing with its source.
        specs = (
            ("GEAR_MRE", None, "-1001000", {"m_FoodItemSerialized": food_record("846.509155", "1750.0")}),
            ("GEAR_WoodMatches", "23", "-1001002", {}),
            ("GEAR_WoolSocks", None, "-1001004", {"m_ClothingItemSerialized": worn(True)}),
            ("GEAR_WoolSocks", None, "1474360", {"m_ClothingItemSerialized": worn(False)}),
            ("GEAR_WaterSupplyPotable", None, "-1001006",
             {"m_WaterSupplySerialized": NestedJson({"m_VolumeProxy": Number("2000000000")})}),
            ("GEAR_SprayPaintCan", None, "-1001008", {"m_ObjectGuidSerialized": GUID}),
            ("GEAR_PostCard_ML_LakeOverlook", None, "-1001010",
             {"m_MissionObjectSerialized": NestedJson({"m_Id": "GEAR_PostCard_ML_LakeOverlook_X_949.08"})}),
            ("GEAR_Respirator", None, "-1001012", {"m_RespiratorSerialized": NestedJson(
                {"AttachedCanister": {"m_InstanceIDProxy": Number("-584372")}})}),
            ("GEAR_BedRoll", None, "-1001014", {"m_BedSerialized": NestedJson({"m_Guid": GUID.upper()})}),
            ("GEAR_RecycledCan", None, "-1001016", {"m_CookingPotItemSerialized": NestedJson(
                {"m_FireBeingUsedGUID": "", "m_GearItemBeingCookedGUID": ""})}),
            ("GEAR_Knife", None, "12.5", {}),
            ("GEAR_Tinder", None, None, {}),
            ("GEAR_InsulatedFlask_A", None, "-1001020",
             {"m_InsulatedFlaskSerialized": NestedJson({"m_SerializedItems": []})}),
            ("GEAR_Scarf", None, "-1001022", {"m_ClothingItemSerialized": worn("yes")}),
            ("GEAR_Hatchet", None, "-5240386", {}),
            ("GEAR_InsulatedFlask_B", None, "-1001026", {"m_InsulatedFlaskSerialized": NestedJson(
                {"m_SerializedItems": [{"m_PrefabName": "GEAR_CoffeeCup", "m_SerializedGear": NestedJson(
                    {"m_InstanceIDProxy": Number("-1001030")})}]})}),
            ("GEAR_BackerNote1B", None, "-1001028", {"m_MissionObjectSerialized": None,
                                                     "m_ObjectGuidSerialized": None}),
        )
        prefab, count, identity, records = specs[entry]
        made = item(prefab, count, "0.9")
        if identity is None:
            made["m_SerializedGear"] = None
        else:
            gear = made["m_SerializedGear"].value                             # type: ignore[union-attr]
            gear["m_InstanceIDProxy"] = Number(identity)
            gear.update(records)
        return made

    items: List[JsonValue] = [make(entry) for entry in range(17)]
    if copy_of is not None:
        extra = make(copy_of)
        extra["m_SerializedGear"].value["m_InstanceIDProxy"] = Number(number)   # type: ignore[union-attr]
        items.append(extra)
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items,
                                       "m_QuickSelectInstanceIDs": [Number("0") for _ in range(4)]}
    return build_save(global_document={
        "m_Inventory_Serialized": NestedJson(inventory),
        "m_PlayerManagerSerialized": NestedJson({"m_ItemInHandsInstanceID": Number("-1001000")}),
        "m_Unknown": NestedJson({"m_Taken": Number("-5240388")}),
    })


def numbered_save(numbers: Sequence[Optional[str]], elsewhere: Sequence[JsonValue] = ()) -> bytes:
    """A save whose inventory items carry these instance numbers, and that holds other values elsewhere."""
    items: List[JsonValue] = []
    for number in numbers:
        made = item("GEAR_Stone", None, "1.0")
        if number is not None:
            made["m_SerializedGear"].value["m_InstanceIDProxy"] = Number(number)   # type: ignore[union-attr]
        items.append(made)
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items, "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory),
                                       "m_Unknown": NestedJson({"m_Values": list(elsewhere)})})


class NestedRemovalTests(unittest.TestCase):
    def save(self, holder: str, identity: JsonValue = Number("-584372")) -> bytes:
        save = codec.decode_save(copy_save())
        inventory = save.entries["global"]["m_Inventory_Serialized"].value
        items = inventory["m_SerializedItems"]
        items[1], items[7] = items[7], items[1]
        gear = edit._gear_of(items[1])
        assert gear is not None
        attached = gear["m_RespiratorSerialized"].value["AttachedCanister"]
        attached["m_InstanceIDProxy"] = identity
        if holder == "inside":
            gear["m_AttachedCanisterInstanceID"] = identity
        elif holder == "another entry":
            other = edit._gear_of(items[12])
            assert other is not None
            other["m_TargetInstanceID"] = "-584372"
        elif holder == "companion record":
            items[1]["m_Attachments"] = NestedJson([{"m_InstanceIDProxy": Number("-584374")}])
            player = save.entries["global"]["m_PlayerManagerSerialized"].value
            player["m_ItemInHandsInstanceID"] = Number("-584374")
        elif holder:
            save.entries["global"]["m_PlayerManagerSerialized"].value[holder] = identity
        return codec.encode_save(save)

    def test_last_held_canister_stays(self) -> None:
        with self.assertRaisesRegex(EditError, "an item inside it is the last item you held"):
            edit.remove_item(self.save("m_LastUnequippedItemInstanceID"), "GEAR_Respirator")

    def test_canister_in_hands_stays(self) -> None:
        message = "an item inside it is in your hands"
        for holder in ("m_ItemInHandsInstanceID", "companion record"):
            with self.subTest(holder=holder), self.assertRaisesRegex(EditError, message):
                edit.remove_item(self.save(holder), "GEAR_Respirator")

    def test_only_references_inside_the_entry_can_go_with_it(self) -> None:
        result = edit.remove_item(self.save("inside"), "GEAR_Respirator")
        self.assertEqual(result.entry, 1)
        with self.assertRaises(EditError) as caught:
            edit.remove_item(self.save("another entry"), "GEAR_Respirator")
        self.assertIn("the save holds the instance number of an item inside it in another place too: "
                      + ITEMS_PATH + "[12] > m_SerializedGear > m_TargetInstanceID", str(caught.exception))

    def test_short_or_unreadable_nested_identity_is_refused(self) -> None:
        for identity in (Number("999"), Number("-99"), Number("4.5"), "-584372", None):
            with self.subTest(identity=identity), self.assertRaisesRegex(EditError, "an item inside it"):
                edit.remove_item(self.save("", identity), "GEAR_Respirator")


class CopyTests(unittest.TestCase):
    def test_adds_exactly_one_entry(self) -> None:
        original = copy_save()
        result = edit.copy_item(original, "GEAR_MRE")
        self.assertEqual((result.prefab, result.source, result.entry, result.count), ("GEAR_MRE", 0, 17, None))
        self.assertEqual((result.old_number, result.new_number), ("-1001000", "-5240390"))
        # Built independently, with one more entry at the end.
        self.assertEqual(result.data, copy_save(copy_of=0))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(ITEMS_PATH, "17 items", "18 items")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_a_stack_is_copied_with_its_count(self) -> None:
        result = edit.copy_item(copy_save(), "GEAR_WoodMatches")
        self.assertEqual((result.source, result.count), (1, 23))
        self.assertEqual(result.data, copy_save(copy_of=1))

    def test_items_that_may_be_copied(self) -> None:
        for prefab, entry in (("GEAR_WoolSocks", 3), ("GEAR_RecycledCan", 9), ("GEAR_InsulatedFlask_A", 12),
                              ("GEAR_Hatchet", 14), ("GEAR_BackerNote1B", 16)):
            with self.subTest(prefab=prefab):
                result = edit.copy_item(copy_save(), prefab, entry=entry)
                self.assertEqual(result.data, copy_save(copy_of=entry))

    def test_copy_refuses_an_instance_number_below_the_game_range(self) -> None:
        lowest = "-2147483648"
        legal = edit.copy_item(numbered_save(["-2147483646"]), "GEAR_Stone")
        self.assertEqual(legal.new_number, lowest)
        message = "the inventory holds instance numbers so low that no lower one is left for the copy"
        for reason, save in (("at the limit", numbered_save([lowest])),
                             ("last number taken", numbered_save(["-2147483646"], [lowest]))):
            with self.subTest(reason=reason), self.assertRaisesRegex(EditError, message):
                edit.copy_item(save, "GEAR_Stone")

    def test_the_new_instance_number_is_even_negative_below_the_others_and_unused(self) -> None:
        cases = (
            (["-10", "-4"], [], -12),
            (["-9", "-4"], [], -12),                                        # an odd lowest number
            (["1474360"], [], -2),                                          # nothing below zero
            ([None, "12.5"], [], -2),                                       # no number that can be read
            # -12 is taken as a number and -14 as text. The other two are no whole numbers.
            (["-10"], [Number("-12"), "-14", Number("-16.5"), "minus 16"], -16),
            ([], [], -2),
        )
        for numbers, elsewhere, expected in cases:
            with self.subTest(numbers=numbers):
                save = codec.decode_save(numbered_save(numbers, elsewhere))
                self.assertEqual(edit._unused_number(save), expected)

    def test_items_the_tool_does_not_copy(self) -> None:
        other = "its record names this one item in another way too, so a copy would not be a second item: "
        cases = (
            ("GEAR_WoolSocks", 2, "you are wearing it in this save, and the copy would be worn as well. Take "
                                  "it off in the game first"),
            ("GEAR_Scarf", None, "whether you are wearing it is not stored the way this tool expects"),
            ("GEAR_WaterSupplyPotable", None, "the game keeps the water you carry in this entry. Use "
                                              "set-liters to add water"),
            ("GEAR_SprayPaintCan", None, other + "m_ObjectGuidSerialized"),
            ("GEAR_PostCard_ML_LakeOverlook", None, other + "m_MissionObjectSerialized"),
            ("GEAR_Respirator", None, other + "m_RespiratorSerialized > AttachedCanister > m_InstanceIDProxy"),
            ("GEAR_BedRoll", None, other + "m_BedSerialized > m_Guid"),
            ("GEAR_InsulatedFlask_B", None, other + "m_InsulatedFlaskSerialized > m_SerializedItems[0] > "
                                                    "m_SerializedGear > m_InstanceIDProxy"),
            ("GEAR_Knife", None, "its instance number is not stored the way this tool expects"),
            ("GEAR_Tinder", None, "its instance number is not stored the way this tool expects"),
        )
        entries = {"GEAR_Scarf": 13, "GEAR_WaterSupplyPotable": 4, "GEAR_SprayPaintCan": 5,
                   "GEAR_PostCard_ML_LakeOverlook": 6, "GEAR_Respirator": 7, "GEAR_BedRoll": 8,
                   "GEAR_InsulatedFlask_B": 15, "GEAR_Knife": 10, "GEAR_Tinder": 11, "GEAR_WoolSocks": 2}
        for prefab, entry, reason in cases:
            with self.subTest(prefab=prefab):
                with self.assertRaises(EditError) as refused:
                    edit.copy_item(copy_save(), prefab, entry=entry)
                self.assertIn(f"inventory entry {entries[prefab]} ({prefab}) is not copied: {reason}",
                              str(refused.exception))

    def test_requests_that_do_not_name_exactly_one_item_are_refused(self) -> None:
        for prefab, entry, message in (("GEAR_WoolSocks", None, "Add --entry to say which one"),
                                       ("GEAR_Nope", None, "there is no GEAR_Nope in the inventory"),
                                       ("GEAR_MRE", 1, "inventory entry 1 is GEAR_WoodMatches, not GEAR_MRE")):
            with self.subTest(prefab=prefab, entry=entry):
                with self.assertRaises(EditError) as refused:
                    edit.copy_item(copy_save(), prefab, entry=entry)
                self.assertIn(message, str(refused.exception))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = copy_save()
        kept = bytes(original)
        edit.copy_item(original, "GEAR_MRE")
        self.assertEqual(original, kept)

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.copy_item(b"not a save", "GEAR_MRE")
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def refused_with(self, wrong: bytes) -> str:
        """What the tool says when the encoder hands back wrong in place of the save with the copy."""
        with encoding_as(copy_save(copy_of=0), wrong):
            with self.assertRaises(EditError) as caught:
                edit.copy_item(copy_save(), "GEAR_MRE")
        return str(caught.exception)

    def test_a_copy_that_keeps_the_old_number_is_caught(self) -> None:
        self.assertIn("is not the original one plus a copy of entry 0: the new item is not entry 0 with a new "
                      "instance number and nothing else changed", self.refused_with(copy_save(0, "-1001000")))

    def test_a_copy_of_another_item_is_caught(self) -> None:
        self.assertIn("the new item is not entry 0 with a new instance number", self.refused_with(copy_save(1)))

    def test_a_copy_that_differs_from_its_source_is_caught(self) -> None:
        save = codec.decode_save(copy_save(copy_of=0))
        edit._inventory_items(save)[17]["m_SerializedGear"].value["m_NormalizedCondition"] = Number("1.0")
        self.assertIn("the new item is not entry 0 with a new instance number",
                      self.refused_with(codec.encode_save(save)))

    def test_a_copy_that_also_changes_an_original_item_is_caught(self) -> None:
        save = codec.decode_save(copy_save(copy_of=0))
        edit._inventory_items(save)[14]["m_SerializedGear"].value["m_NormalizedCondition"] = Number("1.0")
        self.assertIn("the item at entry 14 is not the original one",
                      self.refused_with(codec.encode_save(save)))

    def test_a_copy_that_is_not_at_the_end_is_caught(self) -> None:
        save = codec.decode_save(copy_save(copy_of=0))
        items = edit._inventory_items(save)
        items.insert(0, items.pop())
        self.assertIn("the item at entry 0 is not the original one", self.refused_with(codec.encode_save(save)))

    def test_two_copies_are_caught(self) -> None:
        save = codec.decode_save(copy_save(copy_of=0))
        items = edit._inventory_items(save)
        items.append(items[1])
        self.assertIn("17 items -> 19 items", self.refused_with(codec.encode_save(save)))

    def test_the_check_of_the_inventory_counts_the_items(self) -> None:
        with self.assertRaises(EditError) as caught:
            edit._prove_copy(copy_save(), copy_save(), 0, "-5240390")
        self.assertIn("plus a copy of entry 0: 17 items where 18 were expected", str(caught.exception))
        edit._prove_copy(copy_save(), copy_save(copy_of=0), 0, "-5240390")

    def test_a_new_number_that_the_save_holds_elsewhere_is_caught(self) -> None:
        # -5240388 is in the save already, in a place outside the inventory.
        with self.assertRaises(EditError) as caught:
            edit._prove_copy(copy_save(), copy_save(0, "-5240388"), 0, "-5240388")
        self.assertIn("the save does not hold the new instance number -5240388 in exactly one place",
                      str(caught.exception))


# (item, instance number, condition). None leaves the name or the number out.
CarriedSpec = Tuple[Optional[str], Optional[str], str]

CARRIED: Tuple[CarriedSpec, ...] = (
    ("GEAR_WoodMatches", "-1001000", "1.0"),
    ("GEAR_Hatchet", "-1001002", "0.75"),
    ("GEAR_Crackers", "1474360", "0.5"),
    ("GEAR_Stick", "-1001006", "0.25"),
    ("GEAR_Stone", "-1001008", "1.0"),
)
ROPE: CarriedSpec = ("GEAR_Rope", "-1001010", "1.0")


def carried_save(carried: Sequence[CarriedSpec] = CARRIED, hours: str = "3.5",
                 inventory: Optional[JsonValue] = None) -> bytes:
    """A save whose inventory holds these entries, each with an instance number of its own.

    inventory puts another value where the list of entries belongs.
    """
    items: List[JsonValue] = []
    for prefab, number, condition in carried:
        made = item(prefab or "GEAR_Nameless", None, condition)
        if prefab is None:
            del made["m_PrefabName"]
        if number is not None:
            made["m_SerializedGear"].value["m_InstanceIDProxy"] = Number(number)   # type: ignore[union-attr]
        items.append(made)
    content: Dict[str, JsonValue] = {"m_SerializedItems": items if inventory is None else inventory,
                                     "m_QuickSelectInstanceIDs": []}
    return build_save(global_document={"m_Inventory_Serialized": NestedJson(content),
                                       "m_HoursPlayed": Number(hours)})


def without(carried: Sequence[CarriedSpec], *entries: int) -> List[CarriedSpec]:
    return [spec for entry, spec in enumerate(carried) if entry not in entries]


def with_condition_of(carried: Sequence[CarriedSpec], entry: int, condition: str) -> List[CarriedSpec]:
    changed = list(carried)
    changed[entry] = (changed[entry][0], changed[entry][1], condition)
    return changed


def with_number_of(carried: Sequence[CarriedSpec], entry: int, number: Optional[str]) -> List[CarriedSpec]:
    changed = list(carried)
    changed[entry] = (changed[entry][0], number, changed[entry][2])
    return changed


HOURS_PATH = "m_Dict > global > m_HoursPlayed"


class PreviewFieldTests(unittest.TestCase):
    def test_new_points_and_changed_hours_are_both_shown(self) -> None:
        before = skills_save({"carcass-harvesting": None}, hours="0.5")
        after = skills_save({"carcass-harvesting": "40"}, hours="0.9")
        path = skill_path("carcass-harvesting")
        self.assertEqual(edit.compare_saves(codec.decode_save(before), codec.decode_save(after)).changes,
                         ((path + " > m_Points", "(left out)", "40"),
                          (path + " > m_NumHoursToConvertToSkillPoints", "0.5", "0.9")))

    def compare_record(self, first: str, second: str) -> Tuple[edit.Difference, ...]:
        before, after = (codec.decode_save(carried_save()) for _ in range(2))
        for save, source in ((before, first), (after, second)):
            items = save.entries["global"]["m_Inventory_Serialized"].value["m_SerializedItems"]
            items[0]["m_SerializedGear"].value["record"] = NestedJson(codec.parse_json(source))
        return edit.compare_saves(before, after).changes

    def test_a_lost_field_shows_its_value(self) -> None:
        path = ITEMS_PATH + "[0] > m_SerializedGear > record > x"
        self.assertEqual(self.compare_record('{"x":false}', '{}'), ((path, "false", "(left out)"),))

    def test_common_fields_in_another_order(self) -> None:
        path = ITEMS_PATH + "[0] > m_SerializedGear > record"
        self.assertEqual(self.compare_record('{"a":1,"b":2}', '{"b":2,"a":1}'),
                         ((path, "fields in the order a, b", "fields in the order b, a"),))

    def test_empty_record_gains_a_field(self) -> None:
        path = ITEMS_PATH + "[0] > m_SerializedGear > record > x"
        self.assertEqual(self.compare_record('{}', '{"x":null}'), ((path, "(left out)", "null"),))


class CompareSavesTests(unittest.TestCase):
    def compare(self, before: bytes, after: bytes) -> edit.SaveComparison:
        return edit.compare_saves(codec.decode_save(before), codec.decode_save(after))

    def position_by_position(self, before: bytes, after: bytes) -> Tuple[Tuple[str, str, str], ...]:
        return tuple(edit.differences(codec.decode_save(before).slot, codec.decode_save(after).slot))

    def test_identical_saves_do_not_differ(self) -> None:
        self.assertEqual(self.compare(carried_save(), carried_save()), edit.SaveComparison((), (), ()))

    def test_inventories_with_the_same_entries_are_compared_position_by_position(self) -> None:
        after = carried_save(with_condition_of(CARRIED, 3, "1.0"), hours="4.25")
        expected = ((condition_path(3), "0.25", "1.0"), (HOURS_PATH, "3.5", "4.25"))
        self.assertEqual(self.compare(carried_save(), after), edit.SaveComparison((), (), expected))
        self.assertEqual(self.position_by_position(carried_save(), after), expected)

    def test_an_entry_that_went_is_named_and_the_others_keep_their_numbers(self) -> None:
        after = carried_save(without(with_condition_of(CARRIED, 3, "1.0"), 1))
        found = self.compare(carried_save(), after)
        # The stick is entry 3 in the first save and entry 2 in the second one.
        self.assertEqual(found, edit.SaveComparison(((1, "GEAR_Hatchet"),), (),
                                                    ((condition_path(3), "0.25", "1.0"),)))
        self.assertEqual(self.position_by_position(carried_save(), after),
                         ((ITEMS_PATH, "5 items", "4 items"),))

    def test_an_entry_that_came_is_named_with_its_number_in_the_second_save(self) -> None:
        for position in (0, 2, 5):
            with self.subTest(position=position):
                more = list(CARRIED)
                more.insert(position, ROPE)
                found = self.compare(carried_save(), carried_save(more))
                self.assertEqual(found, edit.SaveComparison((), ((position, "GEAR_Rope"),), ()))

    def test_copy_reusing_a_removed_items_number_is_a_new_entry(self) -> None:
        before = carried_save([("GEAR_Stick", "-2000", "1.0"), ("GEAR_Hatchet", "-2002", "1.0")])
        without_hatchet = edit.remove_item(before, "GEAR_Hatchet").data
        new_stick = edit.copy_item(without_hatchet, "GEAR_Stick")
        self.assertEqual(new_stick.new_number, "-2002")
        self.assertEqual(self.compare(before, new_stick.data),
                         edit.SaveComparison(((1, "GEAR_Hatchet"),), ((1, "GEAR_Stick"),), ()))

    def test_one_entry_gone_and_one_new_leave_the_others_in_place(self) -> None:
        after = carried_save(without(CARRIED, 1) + [ROPE])
        self.assertEqual(self.compare(carried_save(), after),
                         edit.SaveComparison(((1, "GEAR_Hatchet"),), ((4, "GEAR_Rope"),), ()))
        # The two inventories are of one length. Position by position, every entry behind the one that
        # went is compared with its neighbor: three entries differ in name, number and condition, and
        # the last one in name and number.
        self.assertEqual(len(self.position_by_position(carried_save(), after)), 11)

    def test_several_entries_gone_and_new_next_to_changed_values(self) -> None:
        tinder: CarriedSpec = ("GEAR_Tinder", "-1001012", "1.0")
        after = carried_save([tinder] + without(with_condition_of(CARRIED, 2, "1.0"), 0, 3) + [ROPE],
                             hours="4.25")
        found = self.compare(carried_save(), after)
        self.assertEqual(found.removed, ((0, "GEAR_WoodMatches"), (3, "GEAR_Stick")))
        self.assertEqual(found.added, ((0, "GEAR_Tinder"), (4, "GEAR_Rope")))
        # In save order: the inventory comes before the hours played.
        self.assertEqual(found.changes, ((condition_path(2), "0.5", "1.0"), (HOURS_PATH, "3.5", "4.25")))

    def test_an_entry_without_a_name_is_listed_all_the_same(self) -> None:
        nameless: CarriedSpec = (None, "-1001012", "1.0")
        longer = carried_save(list(CARRIED) + [nameless])
        self.assertEqual(self.compare(longer, carried_save()),
                         edit.SaveComparison(((5, "an item without a name"),), (), ()))
        self.assertEqual(self.compare(carried_save(), longer),
                         edit.SaveComparison((), ((5, "an item without a name"),), ()))

    def test_a_chain_of_edits_made_by_this_tool_is_reported_as_what_was_asked_for(self) -> None:
        original = carried_save()
        step = edit.set_item_condition(original, "GEAR_Stick", 100).data
        step = edit.copy_item(step, "GEAR_Crackers").data
        step = edit.remove_item(step, "GEAR_Hatchet").data
        self.assertEqual(self.compare(original, step),
                         edit.SaveComparison(((1, "GEAR_Hatchet"),), ((4, "GEAR_Crackers"),),
                                             ((condition_path(3), "0.25", "1.0"),)))

    def test_entries_that_cannot_be_matched_are_compared_position_by_position(self) -> None:
        shorter = without(CARRIED, 1)
        twin: CarriedSpec = ("GEAR_Rope", "-1001008", "1.0")
        cases = {
            "an entry of the first save has no instance number": (with_number_of(CARRIED, 4, None), shorter),
            "an entry of the second save has no instance number": (CARRIED, with_number_of(shorter, 3, None)),
            "an instance number is no whole number": (with_number_of(CARRIED, 4, "12.5"), shorter),
            # The rope has the number of the stone before it. Matched by number, the stone would be taken
            # for an entry that went or came, and the rope for the stone.
            "two entries of the first save have the same number": (list(CARRIED) + [twin], CARRIED),
            "two entries of the second save have the same number": (CARRIED, list(CARRIED) + [twin]),
            "the entries both saves hold are in another order": (CARRIED,
                                                                 [shorter[1], shorter[0]] + shorter[2:]),
            "the same entries in another order": (CARRIED, list(reversed(CARRIED))),
        }
        for name, (first, second) in cases.items():
            with self.subTest(name):
                before, after = carried_save(first), carried_save(second)
                expected = self.position_by_position(before, after)
                self.assertTrue(expected)
                self.assertEqual(self.compare(before, after), edit.SaveComparison((), (), expected))

    def test_a_save_without_a_list_of_entries_is_compared_position_by_position(self) -> None:
        odd = carried_save(inventory="none")
        for before, after in ((carried_save(), odd), (odd, carried_save())):
            expected = self.position_by_position(before, after)
            self.assertEqual([path for path, _, _ in expected], [ITEMS_PATH])
            self.assertEqual(self.compare(before, after), edit.SaveComparison((), (), expected))

    def test_the_two_saves_are_left_as_they_are(self) -> None:
        before, after = codec.decode_save(carried_save()), codec.decode_save(carried_save(without(CARRIED, 1)))
        edit.compare_saves(before, after)
        self.assertEqual(codec.encode_save(before), carried_save())
        self.assertEqual(codec.encode_save(after), carried_save(without(CARRIED, 1)))


# The player's own values: (the record in the global entry, the number inside that record).
PLAYER_FIELDS: Dict[str, Tuple[str, str]] = {
    "condition": ("m_Condition_Serialized", "m_CurrentHPProxy"),
    "calories": ("m_Hunger_Serialized", "m_CurrentReserveCaloriesProxy"),
    "hydration": ("m_Thirst_Serialized", "m_CurrentThirstProxy"),
    "rest": ("m_Fatigue_Serialized", "m_CurrentFatigueProxy"),
    "warmth": ("m_Freezing_Serialized", "m_CurrentFreezingProxy"),
}
# What the save holds for each of them. None: the game left the number out, as it does for 0.
PLAYER_STORED: Dict[str, Optional[str]] = {"condition": "76.66127", "calories": "1386.98242",
                                           "hydration": "21.7523441", "rest": "9.5124445", "warmth": None}


def player_save(stored: Optional[Dict[str, JsonValue]] = None,
                records: Optional[Dict[str, JsonValue]] = None) -> bytes:
    """A save with the five records of the player's own values, the way the game writes them.

    stored gives what a record holds for a value, by the name of the value: text for a number,
    None to leave the number out, or any other value to put in its place. records puts another
    value where a whole record belongs, by the name of the record.
    """
    held: Dict[str, JsonValue] = dict(PLAYER_STORED)
    held.update(stored or {})
    document: Dict[str, JsonValue] = {}
    for name, (record, field) in PLAYER_FIELDS.items():
        content: Dict[str, JsonValue] = {}
        if held[name] is not None:
            content[field] = Number(held[name]) if isinstance(held[name], str) else held[name]
        content["m_NumSecondsSinceLastVoiceOver"] = Number("Infinity")
        if name == "hydration":
            content["m_ThirstLevelForPreviousVoiceOver"] = "SlightlyThirsty"
        document[record] = NestedJson(content)
        if name == "condition":
            document["m_Encumber_Serialized"] = NestedJson(
                {"m_NumSecondsSinceLastVoiceOver": Number("147.943771")})
    document.update(records or {})
    document["m_Inventory_Serialized"] = NestedJson(
        {"m_SerializedItems": [item(*spec) for spec in DEFAULT_ITEMS], "m_QuickSelectInstanceIDs": []})
    return build_save(global_document=document)


def player_path(name: str) -> str:
    record, field = PLAYER_FIELDS[name]
    return f"m_Dict > global > {record} > {field}"


class PlayerListTests(unittest.TestCase):
    def read(self, save: bytes) -> List[PlayerValue]:
        return edit.read_player(codec.decode_save(save))

    def test_the_five_values_are_read_the_way_the_game_shows_them(self) -> None:
        self.assertEqual(self.read(player_save()), [
            PlayerValue("condition", "m_CurrentHPProxy", "76.66127", Decimal("76.66127")),
            PlayerValue("calories", "m_CurrentReserveCaloriesProxy", "1386.98242", Decimal("1386.98242")),
            # The save holds the inverse of the three bars: 0 is a full bar.
            PlayerValue("hydration", "m_CurrentThirstProxy", "21.7523441", Decimal("78.2476559")),
            PlayerValue("rest", "m_CurrentFatigueProxy", "9.5124445", Decimal("90.4875555")),
            PlayerValue("warmth", "m_CurrentFreezingProxy", None, Decimal("100")),
        ])

    def test_a_number_the_game_left_out_counts_as_zero(self) -> None:
        values = self.read(player_save(dict.fromkeys(PLAYER_FIELDS)))
        self.assertEqual([(value.name, value.stored, value.shown) for value in values],
                         [("condition", None, Decimal(0)), ("calories", None, Decimal(0)),
                          ("hydration", None, Decimal(100)), ("rest", None, Decimal(100)),
                          ("warmth", None, Decimal(100))])

    def test_the_ends_of_the_bars(self) -> None:
        values = self.read(player_save({"hydration": "100.0", "rest": "0.0", "warmth": "12.5"}))
        self.assertEqual([(value.name, value.stored, value.shown) for value in values[2:]],
                         [("hydration", "100.0", Decimal("0.0")), ("rest", "0.0", Decimal("100.0")),
                          ("warmth", "12.5", Decimal("87.5"))])

    def test_values_that_cannot_be_read_are_marked(self) -> None:
        thirst = "m_Thirst_Serialized"
        cases = {
            "no record": (player_save(records={thirst: None}), None),
            "a record that is no nested document": (player_save(records={thirst: {"m_CurrentThirstProxy":
                                                                                  Number("21.75")}}), None),
            "a nested document that is no record": (player_save(records={thirst: NestedJson([])}), None),
            "a number that is not finite": (player_save({"hydration": "Infinity"}), "Infinity"),
            "no number at all": (player_save({"hydration": Number("NaN")}), "NaN"),
            "text": (player_save(records={thirst: NestedJson({"m_CurrentThirstProxy": "21.75"})}), None),
            "true": (player_save({"hydration": True}), None),
        }
        for name, (save, stored) in cases.items():
            with self.subTest(name):
                values = self.read(save)
                self.assertEqual(values[2], PlayerValue("hydration", "m_CurrentThirstProxy", stored, None))
                # The other four are read as before.
                self.assertEqual([value.shown is not None for value in values], [True, True, False, True, True])


class SetPlayerTests(unittest.TestCase):
    def refusal(self, values: Dict[str, float], save: Optional[bytes] = None) -> str:
        with self.assertRaises(EditError) as refused:
            edit.set_player_values(player_save() if save is None else save, values)
        return str(refused.exception)

    def test_changes_exactly_one_value(self) -> None:
        original = player_save()
        result = edit.set_player_values(original, {"condition": 100})
        self.assertEqual(result.changes, (("condition", "76.66127", "100.0"),))
        # Built independently, with that one number.
        self.assertEqual(result.data, player_save({"condition": "100.0"}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(player_path("condition"), "76.66127", "100.0")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_the_bars_are_written_as_their_inverse(self) -> None:
        with_cold = player_save({"warmth": "82.56313"})
        for name, given, stored, save in (("hydration", 80, "20.0", player_save()),
                                          ("hydration", 0, "100.0", player_save()),
                                          ("rest", 100, "0.0", player_save()),
                                          ("rest", 12.5, "87.5", player_save()),
                                          ("warmth", 25, "75.0", with_cold)):
            with self.subTest(name=name, given=given):
                result = edit.set_player_values(save, {name: given})
                self.assertEqual(result.changes, ((name, edit._player_record(
                    codec.decode_save(save), name)[PLAYER_FIELDS[name][1]].literal, stored),))
                expected = {name: stored} if save is not with_cold else {"warmth": stored}
                self.assertEqual(result.data, player_save(expected))

    def test_condition_and_calories_are_written_as_given(self) -> None:
        result = edit.set_player_values(player_save(), {"calories": 2500, "condition": 1})
        self.assertEqual(result.changes, (("condition", "76.66127", "1.0"),
                                          ("calories", "1386.98242", "2500.0")))
        self.assertEqual(result.data, player_save({"condition": "1.0", "calories": "2500.0"}))
        emptied = edit.set_player_values(player_save(), {"calories": 0})
        self.assertEqual(emptied.data, player_save({"calories": "0.0"}))

    def test_several_values_change_in_one_edit_in_the_order_of_the_save(self) -> None:
        original = player_save()
        result = edit.set_player_values(original, {"rest": 50, "hydration": 100, "condition": 90.5})
        self.assertEqual(result.changes, (("condition", "76.66127", "90.5"), ("hydration", "21.7523441", "0.0"),
                                          ("rest", "9.5124445", "50.0")))
        self.assertEqual(result.data, player_save({"condition": "90.5", "hydration": "0.0", "rest": "50.0"}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [(player_path("condition"), "76.66127", "90.5"),
                                 (player_path("hydration"), "21.7523441", "0.0"),
                                 (player_path("rest"), "9.5124445", "50.0")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))

    def test_numbers_are_written_the_way_the_game_writes_a_32_bit_float(self) -> None:
        # 1234.56789 is not a 32-bit float. The nearest one is written, with the digits it needs.
        self.assertEqual(edit.stored_player_value("calories", 1234.56789), "1234.56787")
        self.assertEqual(edit.stored_player_value("hydration", 33.3), "66.7")
        self.assertEqual(edit.stored_player_value("condition", 100), "100.0")
        self.assertEqual(edit.stored_player_value("warmth", 100), "0.0")
        result = edit.set_player_values(player_save(), {"calories": 1234.56789})
        self.assertEqual(result.data, player_save({"calories": "1234.56787"}))

    def test_numbers_outside_the_limits_are_refused(self) -> None:
        for name, low, high in (("condition", 1, 100), ("calories", 0, 2500), ("hydration", 0, 100),
                                ("rest", 0, 100), ("warmth", 0, 100)):
            for given in (low - 0.5, high + 0.5, float("nan"), float("inf"), float("-inf")):
                with self.subTest(name=name, given=given):
                    self.assertIn(f"{name} must be a number from {low} to {high}, not {given:g}",
                                  self.refusal({name: given}))
            for given in (low, high):
                with self.subTest(name=name, given=given):
                    self.assertIsInstance(edit.stored_player_value(name, given), str)

    def test_a_number_too_close_to_an_end_to_write_is_refused(self) -> None:
        self.assertIn("hydration 99.99999 is too close to 100 for this tool to write. Use 100",
                      self.refusal({"hydration": 99.99999}))
        self.assertIn("calories 1e-05 is too close to 0 for this tool to write. Use 0",
                      self.refusal({"calories": 0.00001}))
        self.assertEqual(edit.stored_player_value("hydration", 99.9999), "0.0001")
        self.assertEqual(edit.stored_player_value("calories", 0.0001), "0.0001")

    def test_a_value_the_save_holds_already_is_refused(self) -> None:
        self.assertIn("condition is already 76.7%. Nothing to change", self.refusal({"condition": 76.66127}))
        self.assertIn("hydration is already 78.2% of the bar. Nothing to change",
                      self.refusal({"hydration": 78.2476559}))
        self.assertIn("calories is already 1387.0. Nothing to change", self.refusal({"calories": 1386.98242}))
        # The same number written another way is the same number.
        self.assertIn("rest is already 100.0% of the bar",
                      self.refusal({"rest": 100}, player_save({"rest": "0"})))

    def test_a_number_the_game_left_out_is_put_in_front_of_its_record(self) -> None:
        original = player_save()
        result = edit.set_player_values(original, {"warmth": 50})
        self.assertEqual(result.changes, (("warmth", None, "50.0"),))
        # Built independently: the builder writes the number first in its record, as the game does.
        self.assertEqual(result.data, player_save({"warmth": "50.0"}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual(found, [("m_Dict > global > m_Freezing_Serialized",
                                  "fields m_NumSecondsSinceLastVoiceOver",
                                  "fields m_CurrentFreezingProxy, m_NumSecondsSinceLastVoiceOver")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_every_value_but_the_condition_can_be_put_in(self) -> None:
        nothing = player_save(dict.fromkeys(PLAYER_FIELDS))
        result = edit.set_player_values(nothing, {"warmth": 25, "rest": 50, "hydration": 75, "calories": 1000})
        self.assertEqual(result.changes, (("calories", None, "1000.0"), ("hydration", None, "25.0"),
                                          ("rest", None, "50.0"), ("warmth", None, "75.0")))
        self.assertEqual(result.data, player_save({"condition": None, "calories": "1000.0", "hydration": "25.0",
                                                   "rest": "50.0", "warmth": "75.0"}))

    def test_numbers_held_and_numbers_left_out_change_in_one_edit(self) -> None:
        original = player_save()
        result = edit.set_player_values(original, {"warmth": 25, "condition": 100, "rest": 100})
        self.assertEqual(result.changes, (("condition", "76.66127", "100.0"), ("rest", "9.5124445", "0.0"),
                                          ("warmth", None, "75.0")))
        self.assertEqual(result.data, player_save({"condition": "100.0", "rest": "0.0", "warmth": "75.0"}))
        found = list(edit.differences(codec.decode_save(original).slot, codec.decode_save(result.data).slot))
        self.assertEqual([path for path, _, _ in found], [player_path("condition"), player_path("rest"),
                                                          "m_Dict > global > m_Freezing_Serialized"])

    def test_a_number_left_out_that_would_stay_zero_is_refused(self) -> None:
        self.assertIn("warmth is already 100.0% of the bar: the game leaves m_CurrentFreezingProxy out of the "
                      "save when it is 0. Nothing to change", self.refusal({"warmth": 100}))
        self.assertIn("calories is already 0.0: the game leaves m_CurrentReserveCaloriesProxy out of the save "
                      "when it is 0", self.refusal({"calories": 0}, player_save({"calories": None})))

    def test_only_a_positive_stored_condition_can_be_changed(self) -> None:
        living = player_save({"condition": "0.01"})
        raised = edit.set_player_values(living, {"condition": 100})
        self.assertEqual(raised.changes, (("condition", "0.01", "100.0"),))
        for old in ("0.0", "-0.0", "-1.25"):
            with self.subTest(old=old):
                source = player_save({"condition": old})
                with self.assertRaises(EditError) as caught:
                    edit.set_player_values(source, {"condition": 100})
                self.assertEqual(str(caught.exception), f"this save holds a condition of {old}, which means "
                                 "the character is dead. This tool does not bring it back")

    def test_a_missing_condition_is_not_put_in(self) -> None:
        dead = player_save({"condition": None})
        self.assertIn("this save holds no condition, which means 0: the character is dead. This tool does not "
                      "bring it back", self.refusal({"condition": 50}, dead))
        # The other values of that save can still be changed.
        self.assertEqual(edit.set_player_values(dead, {"rest": 100}).data,
                         player_save({"condition": None, "rest": "0.0"}))

    def put_in_wrongly(self, cold: Dict[str, JsonValue]) -> str:
        """What the tool says when the encoder writes this record of the cold where warmth 50 was asked for."""
        wrong = player_save(records={"m_Freezing_Serialized": NestedJson(cold)})
        with encoding_as(player_save({"warmth": "50.0"}), wrong):
            with self.assertRaises(EditError) as refused:
                edit.set_player_values(player_save(), {"warmth": 50})
        return str(refused.exception)

    def test_a_number_put_in_with_another_value_is_caught(self) -> None:
        message = self.put_in_wrongly({"m_CurrentFreezingProxy": Number("75.0"),
                                       "m_NumSecondsSinceLastVoiceOver": Number("Infinity")})
        self.assertIn("m_Freezing_Serialized in the edited save is not the original one with "
                      "m_CurrentFreezingProxy 50.0 put in front: m_CurrentFreezingProxy is 75.0", message)

    def test_a_number_put_in_next_to_another_change_in_its_record_is_caught(self) -> None:
        # The comparison of the two saves stops at a record whose fields changed, so this takes its own check.
        message = self.put_in_wrongly({"m_CurrentFreezingProxy": Number("50.0"),
                                       "m_NumSecondsSinceLastVoiceOver": Number("5.0")})
        self.assertIn("put in front: m_NumSecondsSinceLastVoiceOver changed as well", message)

    def test_a_number_put_in_at_the_end_of_its_record_is_caught(self) -> None:
        message = self.put_in_wrongly({"m_NumSecondsSinceLastVoiceOver": Number("Infinity"),
                                       "m_CurrentFreezingProxy": Number("50.0")})
        self.assertIn("fields m_NumSecondsSinceLastVoiceOver, m_CurrentFreezingProxy", message)

    def test_the_check_of_a_number_put_in(self) -> None:
        original, good = player_save(), player_save({"warmth": "50.0"})
        edit._prove_number_put_in(original, good, "warmth", "50.0")
        cases = {
            "nothing put in": (original, "its fields are m_NumSecondsSinceLastVoiceOver"),
            "put in at the end": (player_save(records={"m_Freezing_Serialized": NestedJson(
                {"m_NumSecondsSinceLastVoiceOver": Number("Infinity"),
                 "m_CurrentFreezingProxy": Number("50.0")})}),
                "its fields are m_NumSecondsSinceLastVoiceOver, m_CurrentFreezingProxy"),
            "text put in": (player_save(records={"m_Freezing_Serialized": NestedJson(
                {"m_CurrentFreezingProxy": "50.0", "m_NumSecondsSinceLastVoiceOver": Number("Infinity")})}),
                'm_CurrentFreezingProxy is "50.0"'),
            "the record is gone": (player_save(records={"m_Freezing_Serialized": None}),
                                   "the record cannot be read"),
        }
        for name, (edited, problem) in cases.items():
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit._prove_number_put_in(original, edited, "warmth", "50.0")
                self.assertIn("safety check failed: m_Freezing_Serialized in the edited save is not the "
                              f"original one with m_CurrentFreezingProxy 50.0 put in front: {problem}",
                              str(caught.exception))
        with self.assertRaises(EditError):
            edit._prove_number_put_in(player_save(records={"m_Freezing_Serialized": None}), good, "warmth",
                                      "50.0")

    def test_values_that_cannot_be_read_are_refused(self) -> None:
        thirst = "m_Thirst_Serialized"
        record = f"hydration is not stored the way this tool expects ({thirst}), so it is not changed"
        number = "hydration is not stored as a number (m_CurrentThirstProxy), so it is not changed"
        cases = {
            "no record": (player_save(records={thirst: None}), record),
            "a record that is no nested document": (player_save(records={thirst: {}}), record),
            "a number that is not finite": (player_save({"hydration": "Infinity"}), number),
            "text": (player_save(records={thirst: NestedJson({"m_CurrentThirstProxy": "21.75"})}), number),
            "true": (player_save({"hydration": True}), number),
        }
        for name, (save, message) in cases.items():
            with self.subTest(name):
                self.assertIn(message, self.refusal({"hydration": 50}, save))

    def test_names_that_are_no_values_are_refused(self) -> None:
        names = "condition, calories, hydration, rest, warmth"
        self.assertEqual(self.refusal({"mood": 50, "rest": 50}), f"name one or more of {names}, not mood")
        self.assertEqual(self.refusal({}), f"name one or more of {names}")

    def test_one_refused_value_stops_the_whole_edit(self) -> None:
        original = player_save()
        kept = bytes(original)
        # The first value could be changed. The second one cannot, and no save comes back.
        for values in ({"condition": 100, "warmth": 100}, {"condition": 100, "rest": 100.5},
                       {"condition": 100, "rest": 90.4875555}):
            with self.subTest(values=values):
                with self.assertRaises(EditError):
                    edit.set_player_values(original, values)
        self.assertEqual(original, kept)

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = player_save()
        kept = bytes(original)
        edit.set_player_values(original, {"condition": 100, "rest": 100})
        self.assertEqual(original, kept)

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        with self.assertRaises(EditError) as refused:
            edit.set_player_values(b"not a save", {"condition": 100})
        self.assertIn("does not pass the exact rebuild check", str(refused.exception))

    def caught(self, wrong: bytes) -> str:
        """What the tool says when the encoder hands back wrong in place of the save with two values changed."""
        with encoding_as(player_save({"condition": "100.0", "rest": "0.0"}), wrong):
            with self.assertRaises(EditError) as refused:
                edit.set_player_values(player_save(), {"condition": 100, "rest": 100})
        return str(refused.exception)

    def test_a_value_that_was_not_written_is_caught(self) -> None:
        message = self.caught(player_save({"condition": "100.0"}))
        self.assertIn("did not change exactly the 2 values asked for", message)
        self.assertIn("1 asked for and not made", message)
        self.assertIn("m_CurrentFatigueProxy: 9.5124445 -> 0.0", message)

    def test_a_value_written_wrongly_is_caught(self) -> None:
        message = self.caught(player_save({"condition": "100.0", "rest": "100.0"}))
        self.assertIn("m_CurrentFatigueProxy: 9.5124445 -> 100.0", message)

    def test_a_change_that_was_not_asked_for_is_caught(self) -> None:
        message = self.caught(player_save({"condition": "100.0", "rest": "0.0", "hydration": "0.0"}))
        self.assertIn("1 not asked for", message)
        self.assertIn("m_CurrentThirstProxy: 21.7523441 -> 0.0", message)


# The nine skills in the order of the save: (the name this tool takes, the record of the skill).
SKILL_RECORDS: Tuple[Tuple[str, str], ...] = (
    ("fire-starting", "m_Skill_FirestartingSerialized"),
    ("carcass-harvesting", "m_Skill_CarcassHarvestingSerialized"),
    ("cooking", "m_Skill_CookingSerialized"),
    ("ice-fishing", "m_Skill_IceFishingSerialized"),
    ("rifle", "m_Skill_RifleSerialized"),
    ("archery", "m_Skill_ArcherySerialized"),
    ("mending", "m_Skill_ClothingRepairSerialized"),
    ("revolver", "m_Skill_RevolverSerialized"),
    ("gunsmithing", "m_Skill_GunsmithingSerialized"),
)
# The points the save holds for each of them. None: the game left the number out, as it does for 0.
SKILL_POINTS: Dict[str, Optional[str]] = {
    "fire-starting": "12", "carcass-harvesting": None, "cooking": "264", "ice-fishing": None, "rifle": None,
    "archery": "22", "mending": None, "revolver": None, "gunsmithing": None}
SKILL_NAMES = ", ".join(name for name, _ in SKILL_RECORDS)


def skills_save(points: Optional[Dict[str, JsonValue]] = None, records: Optional[Dict[str, JsonValue]] = None,
                hours: str = "0.783333361", document: Optional[Dict[str, JsonValue]] = None) -> bytes:
    """A save with the records of the nine skills, the way the game writes them.

    points gives what a record holds as its points, by the name of the skill: text for a number,
    None to leave the number out, or any other value to put in its place. records puts another
    value where the record of a skill belongs, by the name of the record. hours is the one other
    number a skill record holds, in the record of carcass harvesting. document puts other values
    into the global entry, by the name of the field.
    """
    held: Dict[str, JsonValue] = dict(SKILL_POINTS)
    held.update(points or {})
    skills: Dict[str, JsonValue] = {}
    for name, record in SKILL_RECORDS:
        content: Dict[str, JsonValue] = {}
        if held[name] is not None:
            content["m_Points"] = Number(held[name]) if isinstance(held[name], str) else held[name]
        if name == "carcass-harvesting":
            content["m_NumHoursToConvertToSkillPoints"] = Number(hours)
        skills[record] = NestedJson(content)
    skills.update(records or {})
    whole: Dict[str, JsonValue] = {
        "m_SkillsManagerSerialized": NestedJson(skills),
        # Another record with "skill" in its name, which is not one of the nine skills.
        "m_PlayerSkillsSerialized": NestedJson({"m_RepairSkill": Number("60"), "m_CleanSkill": Number("30"),
                                                "m_SharpenSkill": Number("30")}),
        "m_Inventory_Serialized": NestedJson(
            {"m_SerializedItems": [item(*spec) for spec in DEFAULT_ITEMS], "m_QuickSelectInstanceIDs": []}),
    }
    whole.update(document or {})
    return build_save(global_document=whole)


def skill_path(name: str) -> str:
    return f"m_Dict > global > m_SkillsManagerSerialized > {dict(SKILL_RECORDS)[name]}"


def slot_differences(before: bytes, after: bytes) -> List[Tuple[str, str, str]]:
    return list(edit.differences(codec.decode_save(before).slot, codec.decode_save(after).slot))


class SkillListTests(unittest.TestCase):
    def listed(self, save: bytes) -> List[Skill]:
        return edit.list_skills(codec.decode_save(save))

    def test_the_nine_skills_are_listed_in_the_order_of_the_save(self) -> None:
        self.assertEqual(self.listed(skills_save()), [
            Skill("fire-starting", "m_Skill_FirestartingSerialized", "12", 12),
            Skill("carcass-harvesting", "m_Skill_CarcassHarvestingSerialized", None, 0),
            Skill("cooking", "m_Skill_CookingSerialized", "264", 264),
            Skill("ice-fishing", "m_Skill_IceFishingSerialized", None, 0),
            Skill("rifle", "m_Skill_RifleSerialized", None, 0),
            Skill("archery", "m_Skill_ArcherySerialized", "22", 22),
            Skill("mending", "m_Skill_ClothingRepairSerialized", None, 0),
            Skill("revolver", "m_Skill_RevolverSerialized", None, 0),
            Skill("gunsmithing", "m_Skill_GunsmithingSerialized", None, 0),
        ])

    def test_the_tool_takes_the_nine_skills_by_these_names(self) -> None:
        self.assertEqual(tuple(edit.SKILLS.items()), SKILL_RECORDS)

    def test_a_zero_the_save_holds_is_read_as_zero(self) -> None:
        # The old C# editor writes every number of a record, a 0 as well.
        skills = self.listed(skills_save({"rifle": "0", "carcass-harvesting": "57"}))
        self.assertEqual(skills[4], Skill("rifle", "m_Skill_RifleSerialized", "0", 0))
        self.assertEqual(skills[1],
                         Skill("carcass-harvesting", "m_Skill_CarcassHarvestingSerialized", "57", 57))

    def test_skills_that_cannot_be_read_are_marked(self) -> None:
        cooking = "m_Skill_CookingSerialized"
        cases = {
            "no record": (skills_save(records={cooking: None}), None),
            "a record that is no nested document": (skills_save(records={cooking: {"m_Points": Number("3")}}),
                                                    None),
            "a nested document that is no record": (skills_save(records={cooking: NestedJson([])}), None),
            "points with a fraction": (skills_save({"cooking": "3.5"}), "3.5"),
            "points below zero": (skills_save({"cooking": "-3"}), "-3"),
            "text": (skills_save(records={cooking: NestedJson({"m_Points": "3"})}), None),
            "true": (skills_save({"cooking": True}), None),
        }
        for name, (save, stored) in cases.items():
            with self.subTest(name):
                skills = self.listed(save)
                self.assertEqual(skills[2], Skill("cooking", cooking, stored, None))
                # The other eight are read as before.
                self.assertEqual([skill for skill in skills if skill.name != "cooking"],
                                 [skill for skill in self.listed(skills_save()) if skill.name != "cooking"])

    def test_no_skill_can_be_read_without_the_record_that_holds_the_nine(self) -> None:
        # The nine records as they are in a save, only not inside a nested document.
        plain: Dict[str, JsonValue] = {record: NestedJson({"m_Points": Number("5")})
                                       for _, record in SKILL_RECORDS}
        for name, manager in (("nothing", None), ("no nested document", plain),
                              ("a nested document that is no record", NestedJson([]))):
            with self.subTest(name):
                skills = self.listed(skills_save(document={"m_SkillsManagerSerialized": manager}))
                self.assertEqual([(skill.name, skill.stored, skill.points) for skill in skills],
                                 [(skill, None, None) for skill, _ in SKILL_RECORDS])


class SetSkillTests(unittest.TestCase):
    def refusal(self, skill: str, points: int, save: Optional[bytes] = None) -> str:
        with self.assertRaises(EditError) as refused:
            edit.set_skill_points(skills_save() if save is None else save, skill, points)
        return str(refused.exception)

    def test_changes_exactly_one_value(self) -> None:
        original = skills_save()
        result = edit.set_skill_points(original, "cooking", 150)
        self.assertEqual((result.skill, result.old_points, result.new_points), ("cooking", "264", "150"))
        # Built independently, with that one number.
        self.assertEqual(result.data, skills_save({"cooking": "150"}))
        self.assertEqual(slot_differences(original, result.data),
                         [(skill_path("cooking") + " > m_Points", "264", "150")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_every_skill_can_be_changed(self) -> None:
        original = skills_save()
        for name, _ in SKILL_RECORDS:
            with self.subTest(name):
                result = edit.set_skill_points(original, name, 77)
                self.assertEqual((result.skill, result.old_points, result.new_points),
                                 (name, SKILL_POINTS[name], "77"))
                self.assertEqual(result.data, skills_save({name: "77"}))

    def test_points_the_game_left_out_are_put_into_an_empty_record(self) -> None:
        original = skills_save()
        result = edit.set_skill_points(original, "rifle", 40)
        self.assertEqual((result.skill, result.old_points, result.new_points), ("rifle", None, "40"))
        self.assertEqual(result.data, skills_save({"rifle": "40"}))
        self.assertEqual(slot_differences(original, result.data),
                         [(skill_path("rifle"), "fields ", "fields m_Points")])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_points_the_game_left_out_are_put_in_front_of_what_the_record_holds(self) -> None:
        original = skills_save()
        result = edit.set_skill_points(original, "carcass-harvesting", 7)
        self.assertEqual((result.old_points, result.new_points), (None, "7"))
        # Built independently: the builder writes the points first in the record, as the game does.
        self.assertEqual(result.data, skills_save({"carcass-harvesting": "7"}))
        self.assertEqual(slot_differences(original, result.data),
                         [(skill_path("carcass-harvesting"), "fields m_NumHoursToConvertToSkillPoints",
                           "fields m_Points, m_NumHoursToConvertToSkillPoints")])

    def test_points_next_to_another_number_change_alone(self) -> None:
        original = skills_save({"carcass-harvesting": "57"})
        result = edit.set_skill_points(original, "carcass-harvesting", 58)
        self.assertEqual(result.data, skills_save({"carcass-harvesting": "58"}))
        self.assertEqual(slot_differences(original, result.data),
                         [(skill_path("carcass-harvesting") + " > m_Points", "57", "58")])

    def test_the_ends_of_the_range(self) -> None:
        self.assertEqual(edit.set_skill_points(skills_save(), "cooking", 0).data, skills_save({"cooking": "0"}))
        self.assertEqual(edit.set_skill_points(skills_save(), "cooking", 9999).data,
                         skills_save({"cooking": "9999"}))
        self.assertEqual(edit.set_skill_points(skills_save(), "rifle", 9999).data,
                         skills_save({"rifle": "9999"}))
        self.assertEqual(edit.set_skill_points(skills_save(), "rifle", 1).data, skills_save({"rifle": "1"}))
        for points in (-1, 10000):
            for skill in ("cooking", "rifle"):
                with self.subTest(skill=skill, points=points):
                    self.assertEqual(self.refusal(skill, points),
                                     f"the points must be a whole number from 0 to 9999, not {points}")

    def test_points_that_are_no_whole_number_are_refused(self) -> None:
        for points in (12.5, 12.0, "12", True, False, None):
            with self.subTest(points=points):
                self.assertEqual(self.refusal("cooking", points),
                                 f"the points must be a whole number from 0 to 9999, not {points}")

    def test_points_the_save_holds_already_are_refused(self) -> None:
        self.assertEqual(self.refusal("cooking", 264), "cooking has 264 points already. Nothing to change")
        # A 0 the save holds, as the old C# editor writes it.
        self.assertEqual(self.refusal("rifle", 0, skills_save({"rifle": "0"})),
                         "rifle has 0 points already. Nothing to change")

    def test_points_left_out_that_would_stay_zero_are_refused(self) -> None:
        self.assertEqual(self.refusal("rifle", 0), "rifle has 0 points already: the game leaves m_Points out "
                                                   "of the save when it is 0. Nothing to change")

    def test_a_zero_the_save_holds_can_be_changed(self) -> None:
        result = edit.set_skill_points(skills_save({"rifle": "0"}), "rifle", 5)
        self.assertEqual((result.old_points, result.new_points), ("0", "5"))
        self.assertEqual(result.data, skills_save({"rifle": "5"}))

    def test_names_that_are_no_skill_are_refused(self) -> None:
        self.assertEqual(self.refusal("cookin", 5),
                         f"there is no skill named cookin. Did you mean cooking? The skills are {SKILL_NAMES}")
        self.assertEqual(self.refusal("xyz", 5), f"there is no skill named xyz. The skills are {SKILL_NAMES}")
        # The name of a record in the save is not a name this tool takes.
        self.assertIn("there is no skill named m_Skill_CookingSerialized. ",
                      self.refusal("m_Skill_CookingSerialized", 5))
        self.assertIn("there is no skill named . ", self.refusal("", 5))

    def test_skills_that_cannot_be_read_are_refused(self) -> None:
        cooking = "m_Skill_CookingSerialized"
        record = f"the skill cooking is not stored the way this tool expects ({cooking}), so it is not changed"
        number = "the points of cooking are not stored as a plain whole number, so they are not changed"
        cases = {
            "no record": (skills_save(records={cooking: None}), record),
            "a record that is no nested document": (skills_save(records={cooking: {}}), record),
            "a nested document that is no record": (skills_save(records={cooking: NestedJson([])}), record),
            "no record that holds the nine": (skills_save(document={"m_SkillsManagerSerialized": None}),
                                              record),
            "the nine outside a nested document": (skills_save(document={"m_SkillsManagerSerialized": {
                cooking: NestedJson({"m_Points": Number("5")})}}), record),
            "points with a fraction": (skills_save({"cooking": "3.5"}), number),
            "points below zero": (skills_save({"cooking": "-3"}), number),
            "text": (skills_save(records={cooking: NestedJson({"m_Points": "3"})}), number),
            "true": (skills_save({"cooking": True}), number),
        }
        for name, (save, message) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refusal("cooking", 50, save), message)

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = skills_save()
        kept = bytes(original)
        edit.set_skill_points(original, "cooking", 150)
        edit.set_skill_points(original, "rifle", 40)
        self.assertEqual(original, kept)

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        self.assertIn("does not pass the exact rebuild check", self.refusal("cooking", 150, b"not a save"))

    def caught(self, meant: bytes, wrong: bytes, skill: str, points: int) -> str:
        """What the tool says when the encoder hands back wrong in place of the save it meant to write."""
        with encoding_as(meant, wrong):
            with self.assertRaises(EditError) as refused:
                edit.set_skill_points(skills_save(), skill, points)
        return str(refused.exception)

    def test_points_written_wrongly_are_caught(self) -> None:
        message = self.caught(skills_save({"cooking": "150"}), skills_save({"cooking": "151"}), "cooking", 150)
        self.assertIn("did not change exactly the one value asked for", message)
        self.assertIn("m_Skill_CookingSerialized > m_Points: 264 -> 151", message)

    def test_points_written_to_another_skill_are_caught(self) -> None:
        message = self.caught(skills_save({"cooking": "150"}), skills_save({"archery": "150"}), "cooking", 150)
        self.assertIn("did not change exactly the one value asked for", message)
        self.assertIn("m_Skill_ArcherySerialized > m_Points: 22 -> 150", message)

    def test_a_change_that_was_not_asked_for_is_caught(self) -> None:
        message = self.caught(skills_save({"cooking": "150"}), skills_save({"cooking": "150", "archery": "23"}),
                              "cooking", 150)
        self.assertIn("did not change exactly the one value asked for", message)
        self.assertIn("m_Skill_ArcherySerialized > m_Points: 22 -> 23", message)

    def test_points_that_were_not_written_are_caught(self) -> None:
        for skill, meant in (("cooking", skills_save({"cooking": "150"})),
                             ("rifle", skills_save({"rifle": "150"}))):
            with self.subTest(skill):
                message = self.caught(meant, skills_save(), skill, 150)
                self.assertIn("Found: no difference at all", message)

    def test_points_put_in_with_another_value_are_caught(self) -> None:
        # The comparison of the two saves sees a record that got a field, and not what the field holds.
        message = self.caught(skills_save({"rifle": "40"}), skills_save({"rifle": "41"}), "rifle", 40)
        self.assertEqual(message, "safety check failed: m_Skill_RifleSerialized in the edited save is not the "
                                  "original one with m_Points 40 put in front: m_Points is 41")

    def test_points_put_in_as_text_are_caught(self) -> None:
        wrong = skills_save(records={"m_Skill_RifleSerialized": NestedJson({"m_Points": "40"})})
        message = self.caught(skills_save({"rifle": "40"}), wrong, "rifle", 40)
        self.assertIn('with m_Points 40 put in front: m_Points is "40"', message)

    def test_points_put_in_next_to_another_change_in_the_record_are_caught(self) -> None:
        # The comparison of the two saves stops at a record whose fields changed, so this takes its own check.
        message = self.caught(skills_save({"carcass-harvesting": "7"}),
                              skills_save({"carcass-harvesting": "7"}, hours="0.5"), "carcass-harvesting", 7)
        self.assertIn("m_Skill_CarcassHarvestingSerialized in the edited save is not the original one with "
                      "m_Points 7 put in front: m_NumHoursToConvertToSkillPoints changed as well", message)

    def test_points_put_in_behind_what_the_record_holds_are_caught(self) -> None:
        wrong = skills_save(records={"m_Skill_CarcassHarvestingSerialized": NestedJson(
            {"m_NumHoursToConvertToSkillPoints": Number("0.783333361"), "m_Points": Number("7")})})
        message = self.caught(skills_save({"carcass-harvesting": "7"}), wrong, "carcass-harvesting", 7)
        self.assertIn("fields m_NumHoursToConvertToSkillPoints, m_Points", message)

    def test_points_put_into_the_record_of_another_skill_are_caught(self) -> None:
        message = self.caught(skills_save({"rifle": "40"}), skills_save({"revolver": "40"}), "rifle", 40)
        self.assertIn("did not change exactly the one value asked for", message)
        self.assertIn("m_Skill_RevolverSerialized: fields  -> fields m_Points", message)

    def test_the_check_of_a_number_put_in_needs_both_records(self) -> None:
        after: Dict[str, JsonValue] = {"m_Points": Number("40")}
        edit._prove_put_in({}, after, "m_Skill_RifleSerialized", "m_Points", "40")
        for name, first, second in (("no original record", None, after), ("no edited record", {}, None)):
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit._prove_put_in(first, second, "m_Skill_RifleSerialized", "m_Points", "40")
                self.assertEqual(str(caught.exception),
                                 "safety check failed: m_Skill_RifleSerialized in the edited save is not the "
                                 "original one with m_Points 40 put in front: the record cannot be read")


SPRAIN_LISTS = ("m_CausesLocIDs", "m_Locations", "m_ElapsedHoursList", "m_DurationHoursList",
                "m_ElapsedRestList")
# The kinds of affliction the tool reads, in the order of the save: (the name the tool prints, the
# record of the kind, the lists that hold one item for each case). A kind without lists has a switch.
AFFLICTION_RECORDS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("hypothermia", "m_HypothermiaSerialized", ()),
    ("sprain-pain", "m_SprainPainSerialized", ("m_Locations", "m_Causes", "m_EndTimes")),
    ("sprained-ankle", "m_SprainedAnkleSerialized", SPRAIN_LISTS),
    ("sprained-wrist", "m_SprainedWristSerialized", SPRAIN_LISTS),
    ("burns", "m_BurnsSerialized", ()),
)
EVERY_KIND = tuple(name for name, _, _ in AFFLICTION_RECORDS)


def afflictions_save(hypothermia: JsonValue = None, burns: JsonValue = None, pain: int = 0, ankles: int = 0,
                     wrists: int = 0, cured: Sequence[str] = (), records: Optional[Dict[str, JsonValue]] = None,
                     burns_first: bool = False) -> bytes:
    """A save with the five records of the afflictions the tool reads, the way the game writes them.

    hypothermia and burns give the switch of the record: True or False, None to leave it out as
    the game does while it is off, or any other value to put in its place. pain, ankles and
    wrists give the number of cases. cured names the kinds to write the way they look once the
    tool has cured them: a switch that was on is off, the lists are empty, and every other field
    is as it was. records puts another value where a whole record belongs, by the name of the
    record. burns_first moves the record of the burns in front of the others.
    """
    def switch(name: str, held: JsonValue, others: Dict[str, JsonValue]) -> NestedJson:
        content: Dict[str, JsonValue] = {}
        if held is not None:
            content["m_Active"] = False if held is True and name in cured else held
        content.update(others)
        return NestedJson(content)

    def sprains(name: str, count: int, others: Dict[str, JsonValue]) -> NestedJson:
        cases = 0 if name in cured else count
        content = dict(others)
        content["m_CausesLocIDs"] = ["GAMEPLAY_Fall"] * cases
        content["m_Locations"] = [Number(str(3 + 2 * case)) for case in range(cases)]
        content["m_ElapsedHoursList"] = [Number(f"{3 + case}.31633782") for case in range(cases)]
        content["m_DurationHoursList"] = [Number(f"{61 + case}.7218") for case in range(cases)]
        content["m_ElapsedRestList"] = [Number("0.0")] * cases
        return NestedJson(content)

    aching = 0 if "sprain-pain" in cured else pain
    pulse: Dict[str, JsonValue] = {"m_SecondsSinceLastPulseFx": Number("10.0")} if pain else {}
    whole: Dict[str, JsonValue] = {
        "m_HypothermiaSerialized": switch("hypothermia", hypothermia, {
            "m_ElapsedWarmTime": Number("8.781072"), "m_CauseLocID": "GAMEPLAY_ColdWeather"}),
        # Four records the tool does not read. Each of them holds a case, or a bonus that is on.
        "m_WellFedSerialized": NestedJson({"m_Active": True,
                                           "m_ElapsedHoursNotStarving": Number("72.0000458")}),
        "m_FrostbiteSerialized": NestedJson({
            "m_LocationsWithActiveFrostbite": [Number("2")], "m_LocationsWithFrostbiteRisk": [],
            "m_LocationsCurrentFrostbiteDamage": [Number("0.0")] * 12}),
        "m_FoodPoisoningSerialized": NestedJson({"m_Active": True, "m_CauseLocID": "GAMEPLAY_TaintedFood"}),
        "m_SprainPainSerialized": NestedJson({
            **pulse,
            "m_Locations": [Number(str(5 + 6 * case)) for case in range(aching)],
            "m_Causes": ["GAMEPLAY_Fall"] * aching,
            "m_EndTimes": [Number(f"{14 + case}.8732967") for case in range(aching)]}),
        "m_SprainedAnkleSerialized": sprains("sprained-ankle", ankles, {
            "m_SecondsSinceLastPainAudio": Number("8.35467"),
            "m_SecondsUntilNextPainAudio": Number("40.6274529")}),
        "m_SprainedWristSerialized": sprains("sprained-wrist", wrists, {}),
        "m_SprainsSerialized": NestedJson({"m_HadSprainRiskAffliction": True}),
        "m_BurnsSerialized": switch("burns", burns, {
            "m_ElapsedHours": Number("0.0009635717"), "m_DurationHours": Number("35.1657333"),
            "m_SecondsUntilNextBurnReminder": Number("7.71092844"), "m_CauseLocID": "GAMEPLAY_Campfire"}),
        "m_BurnsElectricSerialized": NestedJson({"m_Active": True}),
        "m_Inventory_Serialized": NestedJson(
            {"m_SerializedItems": [item(*spec) for spec in DEFAULT_ITEMS], "m_QuickSelectInstanceIDs": []}),
    }
    whole.update(records or {})
    if burns_first:
        whole = {"m_BurnsSerialized": whole["m_BurnsSerialized"], **whole}
    return build_save(global_document=whole)


def affliction_path(record: str, field: str) -> str:
    return f"m_Dict > global > {record} > {field}"


class AfflictionListTests(unittest.TestCase):
    def listed(self, save: bytes) -> List[Affliction]:
        return edit.list_afflictions(codec.decode_save(save))

    def cases(self, save: bytes) -> List[Optional[int]]:
        return [kind.cases for kind in self.listed(save)]

    def test_a_save_without_a_case_lists_the_five_kinds_with_none(self) -> None:
        # The save holds frostbite, food poisoning, an electric burn and the Well Fed bonus. None counts.
        self.assertEqual(self.listed(afflictions_save()), [
            Affliction("hypothermia", "m_HypothermiaSerialized", 0),
            Affliction("sprain-pain", "m_SprainPainSerialized", 0),
            Affliction("sprained-ankle", "m_SprainedAnkleSerialized", 0),
            Affliction("sprained-wrist", "m_SprainedWristSerialized", 0),
            Affliction("burns", "m_BurnsSerialized", 0),
        ])

    def test_the_tool_reads_these_five_kinds(self) -> None:
        self.assertEqual([(name, kind.record, kind.lists) for name, kind in edit.AFFLICTIONS.items()],
                         list(AFFLICTION_RECORDS))

    def test_the_cases_of_each_kind_are_counted(self) -> None:
        self.assertEqual(self.cases(afflictions_save(hypothermia=True, burns=True, pain=1, ankles=3, wrists=2)),
                         [1, 1, 3, 2, 1])
        self.assertEqual(self.cases(afflictions_save(hypothermia=True)), [1, 0, 0, 0, 0])
        self.assertEqual(self.cases(afflictions_save(burns=True)), [0, 0, 0, 0, 1])
        self.assertEqual(self.cases(afflictions_save(pain=2)), [0, 2, 0, 0, 0])
        self.assertEqual(self.cases(afflictions_save(ankles=1)), [0, 0, 1, 0, 0])
        self.assertEqual(self.cases(afflictions_save(wrists=1)), [0, 0, 0, 1, 0])

    def test_a_switch_that_is_off_is_no_case(self) -> None:
        # The game leaves the switch out while it is off. The old C# editor writes it as false.
        self.assertEqual(self.cases(afflictions_save(hypothermia=False, burns=False)), [0, 0, 0, 0, 0])
        self.assertEqual(self.cases(afflictions_save(hypothermia=False, burns=True)), [0, 0, 0, 0, 1])

    def test_kinds_that_cannot_be_read_are_marked(self) -> None:
        wrist = "m_SprainedWristSerialized"
        two = {"m_CausesLocIDs": ["GAMEPLAY_Fall"] * 2, "m_Locations": [Number("3"), Number("5")],
               "m_ElapsedHoursList": [Number("3.3"), Number("0.0")],
               "m_DurationHoursList": [Number("61.7"), Number("65.3")],
               "m_ElapsedRestList": [Number("0.0"), Number("0.0")]}
        cases = {
            "no record": {wrist: None},
            "a record that is no nested document": {wrist: two},
            "a nested document that is no record": {wrist: NestedJson([])},
            "a list is missing": {wrist: NestedJson({key: held for key, held in two.items()
                                                     if key != "m_ElapsedRestList"})},
            "a list is no list": {wrist: NestedJson({**two, "m_Locations": Number("2")})},
            "one list is shorter": {wrist: NestedJson({**two, "m_DurationHoursList": [Number("61.7")]})},
            "one list is longer": {wrist: NestedJson({**two, "m_CausesLocIDs": ["GAMEPLAY_Fall"] * 3})},
        }
        self.assertEqual(self.cases(afflictions_save(records={wrist: NestedJson(two)})), [0, 0, 0, 2, 0])
        for name, records in cases.items():
            with self.subTest(name):
                self.assertEqual(self.listed(afflictions_save(hypothermia=True, records=records)), [
                    Affliction("hypothermia", "m_HypothermiaSerialized", 1),
                    Affliction("sprain-pain", "m_SprainPainSerialized", 0),
                    Affliction("sprained-ankle", "m_SprainedAnkleSerialized", 0),
                    Affliction("sprained-wrist", wrist, None),
                    Affliction("burns", "m_BurnsSerialized", 0),
                ])

    def test_a_switch_that_is_neither_on_nor_off_cannot_be_read(self) -> None:
        for held in (Number("1"), "true", [True]):
            with self.subTest(held=held):
                self.assertEqual(self.cases(afflictions_save(hypothermia=held, burns=True)), [None, 0, 0, 0, 1])
        nothing = afflictions_save(records={"m_BurnsSerialized": NestedJson({"m_Active": None})})
        self.assertEqual(self.cases(nothing), [0, 0, 0, 0, None])
        for name, record in (("no record", None), ("a record that is no nested document", {"m_Active": True}),
                             ("a nested document that is no record", NestedJson([True]))):
            with self.subTest(name):
                self.assertEqual(self.cases(afflictions_save(records={"m_BurnsSerialized": record})),
                                 [0, 0, 0, 0, None])


class CureTests(unittest.TestCase):
    def refusal(self, save: bytes) -> str:
        with self.assertRaises(EditError) as refused:
            edit.cure_afflictions(save)
        return str(refused.exception)

    def test_every_case_of_every_kind_is_ended(self) -> None:
        original = afflictions_save(hypothermia=True, burns=True, pain=1, ankles=1, wrists=2)
        result = edit.cure_afflictions(original)
        self.assertEqual(result.cured, (("hypothermia", 1), ("sprain-pain", 1), ("sprained-ankle", 1),
                                        ("sprained-wrist", 2), ("burns", 1)))
        # Built independently: the switches off, the lists empty, and every other field as it was.
        self.assertEqual(result.data, afflictions_save(hypothermia=True, burns=True, pain=1, ankles=1, wrists=2,
                                                       cured=EVERY_KIND))
        self.assertEqual(slot_differences(original, result.data), [
            (affliction_path("m_HypothermiaSerialized", "m_Active"), "true", "false"),
            (affliction_path("m_SprainPainSerialized", "m_Locations"), "1 items", "0 items"),
            (affliction_path("m_SprainPainSerialized", "m_Causes"), "1 items", "0 items"),
            (affliction_path("m_SprainPainSerialized", "m_EndTimes"), "1 items", "0 items"),
            *[(affliction_path("m_SprainedAnkleSerialized", held), "1 items", "0 items")
              for held in SPRAIN_LISTS],
            *[(affliction_path("m_SprainedWristSerialized", held), "2 items", "0 items")
              for held in SPRAIN_LISTS],
            (affliction_path("m_BurnsSerialized", "m_Active"), "true", "false"),
        ])
        self.assertEqual((result.changes, result.entries, result.unchanged_entries), (15, 3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)
        after = edit.list_afflictions(codec.decode_save(result.data))
        self.assertEqual([kind.cases for kind in after], [0] * 5)

    def test_one_kind_alone_is_ended(self) -> None:
        for given, name, cases, changes in (({"hypothermia": True}, "hypothermia", 1, 1),
                                            ({"burns": True}, "burns", 1, 1),
                                            ({"pain": 2}, "sprain-pain", 2, 3),
                                            ({"ankles": 1}, "sprained-ankle", 1, 5),
                                            ({"wrists": 3}, "sprained-wrist", 3, 5)):
            with self.subTest(name):
                original = afflictions_save(**given)
                result = edit.cure_afflictions(original)
                self.assertEqual(result.cured, ((name, cases),))
                self.assertEqual(result.data, afflictions_save(**given, cured=(name,)))
                self.assertEqual((result.changes, result.entries, result.unchanged_entries), (changes, 3, 2))

    def test_what_else_a_record_holds_stays(self) -> None:
        result = edit.cure_afflictions(afflictions_save(hypothermia=True, pain=2, ankles=1))
        document = codec.decode_save(result.data).entries["global"]
        self.assertEqual(document["m_HypothermiaSerialized"].value, {
            "m_Active": False, "m_ElapsedWarmTime": Number("8.781072"), "m_CauseLocID": "GAMEPLAY_ColdWeather"})
        self.assertEqual(document["m_SprainPainSerialized"].value, {
            "m_SecondsSinceLastPulseFx": Number("10.0"), "m_Locations": [], "m_Causes": [], "m_EndTimes": []})
        self.assertEqual(list(document["m_SprainedAnkleSerialized"].value),
                         ["m_SecondsSinceLastPainAudio", "m_SecondsUntilNextPainAudio", *SPRAIN_LISTS])

    def test_afflictions_the_tool_does_not_read_are_left_as_they_are(self) -> None:
        cured = codec.decode_save(edit.cure_afflictions(afflictions_save(hypothermia=True, burns=True)).data)
        document = cured.entries["global"]
        self.assertIs(document["m_FoodPoisoningSerialized"].value["m_Active"], True)
        self.assertIs(document["m_BurnsElectricSerialized"].value["m_Active"], True)
        self.assertIs(document["m_WellFedSerialized"].value["m_Active"], True)
        self.assertEqual(document["m_FrostbiteSerialized"].value["m_LocationsWithActiveFrostbite"],
                         [Number("2")])

    def test_a_switch_the_save_holds_as_off_is_left_alone(self) -> None:
        original = afflictions_save(hypothermia=False, burns=True)
        result = edit.cure_afflictions(original)
        self.assertEqual(result.cured, (("burns", 1),))
        self.assertEqual(result.data, afflictions_save(hypothermia=False, burns=True, cured=("burns",)))
        self.assertEqual(slot_differences(original, result.data),
                         [(affliction_path("m_BurnsSerialized", "m_Active"), "true", "false")])

    def test_the_changes_are_made_in_the_order_of_the_save(self) -> None:
        original = afflictions_save(hypothermia=True, burns=True, wrists=1, burns_first=True)
        result = edit.cure_afflictions(original)
        self.assertEqual(result.cured, (("burns", 1), ("hypothermia", 1), ("sprained-wrist", 1)))
        self.assertEqual(result.data, afflictions_save(hypothermia=True, burns=True, wrists=1, burns_first=True,
                                                       cured=EVERY_KIND))

    def test_the_lists_of_a_record_are_emptied_in_the_order_the_record_holds_them(self) -> None:
        def pain(cases: int) -> Dict[str, JsonValue]:
            return {"m_SprainPainSerialized": NestedJson({
                "m_EndTimes": [Number("14.8732967")] * cases, "m_Causes": ["GAMEPLAY_Fall"] * cases,
                "m_Other": [Number("1")], "m_Locations": [Number("5")] * cases})}

        original = afflictions_save(records=pain(1))
        result = edit.cure_afflictions(original)
        self.assertEqual(result.data, afflictions_save(records=pain(0)))
        self.assertEqual([path.rsplit(" > ", 1)[1] for path, _, _ in slot_differences(original, result.data)],
                         ["m_EndTimes", "m_Causes", "m_Locations"])

    def test_a_save_without_a_case_is_refused(self) -> None:
        message = ("there is nothing to cure: this save holds no hypothermia, sprain-pain, sprained-ankle, "
                   "sprained-wrist or burns")
        self.assertEqual(self.refusal(afflictions_save()), message)
        self.assertEqual(self.refusal(afflictions_save(hypothermia=False, burns=False)), message)

    def test_a_cured_save_has_nothing_left_to_cure(self) -> None:
        cured = edit.cure_afflictions(afflictions_save(hypothermia=True, wrists=2)).data
        self.assertIn("there is nothing to cure", self.refusal(cured))

    def test_one_kind_that_cannot_be_read_stops_the_whole_cure(self) -> None:
        broken = {"m_SprainedWristSerialized": NestedJson({"m_Locations": [Number("3")]})}
        self.assertEqual(self.refusal(afflictions_save(hypothermia=True, records=broken)),
                         "sprained-wrist (m_SprainedWristSerialized) is not stored the way this tool expects, "
                         "so nothing is cured")
        self.assertEqual(self.refusal(afflictions_save(hypothermia=Number("1"), burns="yes", wrists=1)),
                         "hypothermia (m_HypothermiaSerialized), burns (m_BurnsSerialized) are not stored the "
                         "way this tool expects, so nothing is cured")
        # Also when nothing else needs a cure.
        self.assertIn("so nothing is cured", self.refusal(afflictions_save(records=broken)))

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = afflictions_save(hypothermia=True, burns=True, pain=1, ankles=1, wrists=2)
        kept = bytes(original)
        edit.cure_afflictions(original)
        self.assertEqual(original, kept)

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        self.assertIn("does not pass the exact rebuild check", self.refusal(b"not a save"))

    def caught(self, wrong: bytes) -> str:
        """What the tool says when the encoder hands back wrong for a cure of hypothermia and two wrists."""
        meant = afflictions_save(hypothermia=True, wrists=2, cured=EVERY_KIND)
        with encoding_as(meant, wrong):
            with self.assertRaises(EditError) as refused:
                edit.cure_afflictions(afflictions_save(hypothermia=True, wrists=2))
        return str(refused.exception)

    def test_a_cure_that_was_not_written_is_caught(self) -> None:
        message = self.caught(afflictions_save(hypothermia=True, wrists=2, cured=("sprained-wrist",)))
        self.assertIn("did not change exactly the 6 values asked for", message)
        self.assertIn("1 asked for and not made (m_Dict > global > m_HypothermiaSerialized > m_Active: true -> "
                      "false)", message)

    def test_a_list_that_still_holds_a_case_is_caught(self) -> None:
        message = self.caught(afflictions_save(hypothermia=True, wrists=1, cured=("hypothermia",)))
        self.assertIn("did not change exactly the 6 values asked for", message)
        self.assertIn("m_SprainedWristSerialized > m_CausesLocIDs: 2 items -> 1 items", message)

    def test_one_list_left_as_it_was_is_caught(self) -> None:
        left = {"m_SprainedWristSerialized": NestedJson({
            "m_CausesLocIDs": [], "m_Locations": [Number("3"), Number("5")], "m_ElapsedHoursList": [],
            "m_DurationHoursList": [], "m_ElapsedRestList": []})}
        message = self.caught(afflictions_save(hypothermia=True, cured=("hypothermia",), records=left))
        self.assertIn("1 asked for and not made (m_Dict > global > m_SprainedWristSerialized > m_Locations: "
                      "2 items -> 0 items)", message)

    def test_a_switch_taken_out_of_its_record_is_caught(self) -> None:
        message = self.caught(afflictions_save(wrists=2, cured=("sprained-wrist",)))
        self.assertIn("m_HypothermiaSerialized: fields m_Active, m_ElapsedWarmTime, m_CauseLocID -> fields "
                      "m_ElapsedWarmTime, m_CauseLocID", message)

    def test_a_change_to_what_else_a_record_holds_is_caught(self) -> None:
        changed = {"m_HypothermiaSerialized": NestedJson({
            "m_Active": False, "m_ElapsedWarmTime": Number("0.0"), "m_CauseLocID": "GAMEPLAY_ColdWeather"})}
        message = self.caught(afflictions_save(wrists=2, cured=("sprained-wrist",), records=changed))
        self.assertIn("1 not asked for (m_Dict > global > m_HypothermiaSerialized > m_ElapsedWarmTime: "
                      "8.781072 -> 0.0)", message)

    def test_an_affliction_the_tool_does_not_read_that_changed_is_caught(self) -> None:
        for name, records, change in (
                ("food poisoning", {"m_FoodPoisoningSerialized": NestedJson(
                    {"m_Active": False, "m_CauseLocID": "GAMEPLAY_TaintedFood"})},
                 "m_FoodPoisoningSerialized > m_Active: true -> false"),
                ("frostbite", {"m_FrostbiteSerialized": NestedJson({
                    "m_LocationsWithActiveFrostbite": [], "m_LocationsWithFrostbiteRisk": [],
                    "m_LocationsCurrentFrostbiteDamage": [Number("0.0")] * 12})},
                 "m_FrostbiteSerialized > m_LocationsWithActiveFrostbite: 1 items -> 0 items")):
            with self.subTest(name):
                message = self.caught(afflictions_save(hypothermia=True, wrists=2, cured=EVERY_KIND,
                                                       records=records))
                self.assertIn(f"1 not asked for (m_Dict > global > {change})", message)


STANDS = ("-1.464066", "-0.5301075", "-1.0880059")
POSITION_PATH = "m_Dict > global > m_PlayerManagerSerialized > m_SaveGamePosition"


def position_save(position: Sequence[JsonValue] = STANDS, switches: Optional[Dict[str, JsonValue]] = None,
                  rope: JsonValue = "", records: Optional[Dict[str, JsonValue]] = None) -> bytes:
    """A save with the player's record, the way the game writes it, in the scene GreyMothersHouseA.

    position gives the numbers of the position: text for a number, or any other value to put in
    its place. switches adds fields to the player's record, such as m_PlayerInVehicle. rope is
    what the save holds as the rope the character is on. records puts another value where a
    whole record belongs, by the name of the record.
    """
    manager: Dict[str, JsonValue] = {
        "m_SaveGamePosition": [Number(held) if isinstance(held, str) else held for held in position],
        "m_SaveGameRotation": [Number("0.00301616569"), Number("0.9909711"), Number("-0.13211897"),
                               Number("0.0226230379")],
        "m_StartGearAppliedProxy": True,
        **(switches or {}),
        # In real saves this is the position with the height of the eyes added to y.
        "m_PlayerInVehicleCameraPos": [Number("-1.464066"), Number("1.2198925"), Number("-1.0880059")],
        "m_KnownCodes": [],
    }
    whole: Dict[str, JsonValue] = {
        "m_GameManagerSerialized": NestedJson({"m_SceneTransitionDataSerialized": NestedJson({
            "m_TeleportPlayerSaveGamePosition": True,
            "m_PosBeforeInteriorLoad": [Number("1105.48022"), Number("271.216553"), Number("1783.8783")],
            "m_SceneSaveFilenameCurrent": "GreyMothersHouseA"})}),
        "m_PlayerManagerSerialized": NestedJson(manager),
        "m_PlayerClimbRopeSerialized": NestedJson({"m_RopeGuid": rope}),
        "m_Inventory_Serialized": NestedJson(
            {"m_SerializedItems": [item(*spec) for spec in DEFAULT_ITEMS], "m_QuickSelectInstanceIDs": []}),
    }
    whole.update(records or {})
    return build_save(scene="GreyMothersHouseA", global_document=whole)


class PositionListTests(unittest.TestCase):
    def read(self, save: bytes) -> Position:
        return edit.read_position(codec.decode_save(save))

    def test_the_scene_and_the_three_numbers_are_read(self) -> None:
        self.assertEqual(self.read(position_save()), Position("GreyMothersHouseA", STANDS, None))
        self.assertEqual(edit.AXES, ("x", "y", "z"))

    def test_a_number_is_read_the_way_the_save_holds_it(self) -> None:
        stands = ("6.53425", "-1.1920929E-07", "8.391384")
        self.assertEqual(self.read(position_save(stands)).stored, stands)

    def test_a_save_that_does_not_name_its_scene(self) -> None:
        for name, boot in (("no name", {}), ("an empty name", {"m_SceneName": ""}),
                           ("a name that is no text", {"m_SceneName": Number("5")}), ("no boot entry", None)):
            with self.subTest(name):
                save = codec.decode_save(position_save())
                save.slot["m_Dict"]["boot"] = boot
                self.assertEqual(edit.read_position(save), Position(None, STANDS, None))

    def test_what_holds_the_character_in_place(self) -> None:
        cases = {
            "in a vehicle": position_save(switches={"m_PlayerInVehicle": True}),
            "in a snow shelter": position_save(switches={"m_PlayerInSnowShelter": True}),
            "on a rope": position_save(rope="b7c1f2de-0d7c-4f43-9d6a-3f5f0f6f2a10"),
            "in a state this tool cannot read (m_PlayerInVehicle)": position_save(
                switches={"m_PlayerInVehicle": Number("1")}),
            "in a state this tool cannot read (m_PlayerInSnowShelter)": position_save(
                switches={"m_PlayerInSnowShelter": None}),
            "in a state this tool cannot read (m_PlayerClimbRopeSerialized)": position_save(
                records={"m_PlayerClimbRopeSerialized": {"m_RopeGuid": "b7c1f2de"}}),
        }
        for held, save in cases.items():
            with self.subTest(held):
                self.assertEqual(self.read(save), Position("GreyMothersHouseA", STANDS, held))

    def test_what_does_not_hold_the_character_in_place(self) -> None:
        cases = {
            # The old C# editor writes the two switches when they are off.
            "switches that are off": position_save(switches={"m_PlayerInVehicle": False,
                                                             "m_PlayerInSnowShelter": False}),
            "no record of a rope": position_save(records={"m_PlayerClimbRopeSerialized": None}),
            "a rope that is no text": position_save(rope=None),
        }
        for name, save in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.read(save).held)

    def test_positions_that_cannot_be_read_are_marked(self) -> None:
        manager = "m_PlayerManagerSerialized"
        cases = {
            "two numbers": position_save(STANDS[:2]),
            "four numbers": position_save(STANDS + ("1.0",)),
            "text for a number": position_save(("-1.464066", NestedJson("-0.5301075"), "-1.0880059")),
            "a number that is not finite": position_save(("-1.464066", "Infinity", "-1.0880059")),
            "no number at all": position_save(("-1.464066", "NaN", "-1.0880059")),
            "true for a number": position_save(("-1.464066", True, "-1.0880059")),
            "no list": position_save(records={manager: NestedJson({"m_SaveGamePosition": Number("3")})}),
            "no position": position_save(records={manager: NestedJson({"m_KnownCodes": []})}),
            "no record of the player": position_save(records={manager: None}),
            "a record of the player that is no nested document": position_save(
                records={manager: {"m_SaveGamePosition": [Number("1.0"), Number("2.0"), Number("3.0")]}}),
            "a nested document that is no record": position_save(records={manager: NestedJson([])}),
        }
        for name, save in cases.items():
            with self.subTest(name):
                self.assertEqual(self.read(save), Position("GreyMothersHouseA", None, None))


class SetPositionTests(unittest.TestCase):
    def refusal(self, values: Dict[str, float], save: Optional[bytes] = None) -> str:
        with self.assertRaises(EditError) as refused:
            edit.set_position(position_save() if save is None else save, values)
        return str(refused.exception)

    def test_the_three_numbers_change_and_nothing_else(self) -> None:
        original = position_save()
        result = edit.set_position(original, {"x": -0.797, "y": 0.0165060163, "z": -1.38356638})
        self.assertEqual(result.changes, (("x", "-1.464066", "-0.797"), ("y", "-0.5301075", "0.0165060163"),
                                          ("z", "-1.0880059", "-1.38356638")))
        self.assertEqual((result.scene, result.kept), ("GreyMothersHouseA", ()))
        # Built independently, with those three numbers.
        self.assertEqual(result.data, position_save(("-0.797", "0.0165060163", "-1.38356638")))
        self.assertEqual(slot_differences(original, result.data), [
            (f"{POSITION_PATH}[0]", "-1.464066", "-0.797"),
            (f"{POSITION_PATH}[1]", "-0.5301075", "0.0165060163"),
            (f"{POSITION_PATH}[2]", "-1.0880059", "-1.38356638"),
        ])
        self.assertEqual((result.entries, result.unchanged_entries), (3, 2))
        self.assertTrue(codec.verify_save(result.data).identical)

    def test_one_number_alone_changes(self) -> None:
        for axis, given, stands in (("x", 12.5, ("12.5", "-0.5301075", "-1.0880059")),
                                    ("y", 0, ("-1.464066", "0.0", "-1.0880059")),
                                    ("z", -250.75, ("-1.464066", "-0.5301075", "-250.75"))):
            with self.subTest(axis):
                place = edit.AXES.index(axis)
                result = edit.set_position(position_save(), {axis: given})
                self.assertEqual(result.changes, ((axis, STANDS[place], stands[place]),))
                self.assertEqual(result.data, position_save(stands))
                self.assertEqual(result.kept, ())

    def test_the_numbers_are_changed_in_the_order_of_the_save(self) -> None:
        result = edit.set_position(position_save(), {"z": 3, "x": 1, "y": 2})
        self.assertEqual([axis for axis, _, _ in result.changes], ["x", "y", "z"])
        self.assertEqual(result.data, position_save(("1.0", "2.0", "3.0")))

    def test_a_number_the_save_holds_already_stays(self) -> None:
        original = position_save()
        result = edit.set_position(original, {"x": 4.25, "y": -0.5301075, "z": -1.0880059})
        self.assertEqual(result.changes, (("x", "-1.464066", "4.25"),))
        self.assertEqual(result.kept, (("y", "-0.5301075"), ("z", "-1.0880059")))
        self.assertEqual(result.data, position_save(("4.25", "-0.5301075", "-1.0880059")))
        self.assertEqual(slot_differences(original, result.data),
                         [(f"{POSITION_PATH}[0]", "-1.464066", "4.25")])

    def test_a_number_this_tool_could_not_write_stays_when_the_save_holds_it(self) -> None:
        # The game writes a number this close to 0 with an exponent. Given back as it is, it stays.
        original = position_save(("6.53425", "-1.1920929E-07", "8.391384"))
        result = edit.set_position(original, {"x": 7, "y": -1.1920929e-07, "z": 8.391384})
        self.assertEqual(result.changes, (("x", "6.53425", "7.0"),))
        self.assertEqual(result.kept, (("y", "-1.1920929E-07"), ("z", "8.391384")))
        self.assertEqual(result.data, position_save(("7.0", "-1.1920929E-07", "8.391384")))
        self.assertEqual(self.refusal({"y": -1.1920929e-07}, original),
                         "the character is at that position already. Nothing to change")
        self.assertEqual(self.refusal({"y": -1.2e-07}, original),
                         "y -1.2e-07 is too close to 0 for this tool to write. Use 0")

    def test_a_position_the_save_holds_already_is_refused(self) -> None:
        message = "the character is at that position already. Nothing to change"
        self.assertEqual(self.refusal({"x": -1.464066, "y": -0.5301075, "z": -1.0880059}), message)
        self.assertEqual(self.refusal({"y": -0.5301075}), message)
        # The same number written another way is the same number.
        self.assertEqual(self.refusal({"x": 2}, position_save(("2", "-0.5301075", "-1.0880059"))), message)

    def test_numbers_are_written_the_way_the_game_writes_a_32_bit_float(self) -> None:
        for given, stored in ((1234.56789, "1234.56787"), (1980.29382, "1980.29382"),
                              (-283.880341, "-283.880341"),
                              (0, "0.0"), (-0.0, "0.0"), (0.0001, "0.0001"), (-0.0001, "-0.0001"),
                              (100000, "100000.0"), (-100000, "-100000.0"), (99999.99, "99999.99"),
                              (687.839, "687.839"), (-6.333225, "-6.333225")):
            with self.subTest(given=given):
                self.assertEqual(edit.stored_coordinate("x", given), stored)
        result = edit.set_position(position_save(), {"x": 1234.56789, "y": -0.0})
        self.assertEqual(result.data, position_save(("1234.56787", "0.0", "-1.0880059")))

    def test_a_number_the_game_wrote_with_an_exponent_can_be_replaced(self) -> None:
        original = position_save(("6.53425", "-1.1920929E-07", "8.391384"))
        result = edit.set_position(original, {"y": 0})
        self.assertEqual(result.changes, (("y", "-1.1920929E-07", "0.0"),))
        self.assertEqual(result.data, position_save(("6.53425", "0.0", "8.391384")))

    def test_numbers_outside_the_limits_are_refused(self) -> None:
        # 1e39 is too large for a 32-bit float.
        for given in (100000.5, -100000.5, 1e39, -1e39, float("nan"), float("inf"), float("-inf")):
            for axis in edit.AXES:
                with self.subTest(axis=axis, given=given):
                    self.assertEqual(self.refusal({axis: given}),
                                     f"{axis} must be a number from -100000 to 100000, not {given:g}")

    def test_the_limits_are_checked_before_the_save_is_read(self) -> None:
        self.assertEqual(self.refusal({"x": 5, "z": 100001}, b"not a save"),
                         "z must be a number from -100000 to 100000, not 100001")

    def test_a_number_too_close_to_zero_to_write_is_refused(self) -> None:
        self.assertEqual(self.refusal({"y": 0.00001}),
                         "y 1e-05 is too close to 0 for this tool to write. Use 0")
        self.assertEqual(self.refusal({"z": -0.00009}),
                         "z -9e-05 is too close to 0 for this tool to write. Use 0")

    def test_names_that_are_no_axis_are_refused(self) -> None:
        self.assertEqual(self.refusal({"w": 1, "x": 2}), "name one or more of x, y, z, not w")
        self.assertEqual(self.refusal({}), "name one or more of x, y, z")

    def test_a_character_held_in_place_is_not_moved(self) -> None:
        advice = (" in this save, so this tool does not move it. Leave that state in the game, let the game "
                  "save, and try again")
        cases = {
            "in a vehicle": position_save(switches={"m_PlayerInVehicle": True}),
            "in a snow shelter": position_save(switches={"m_PlayerInSnowShelter": True}),
            "on a rope": position_save(rope="b7c1f2de-0d7c-4f43-9d6a-3f5f0f6f2a10"),
            "in a state this tool cannot read (m_PlayerInVehicle)": position_save(
                switches={"m_PlayerInVehicle": "yes"}),
        }
        for held, save in cases.items():
            with self.subTest(held):
                self.assertEqual(self.refusal({"x": 5}, save), f"the character is {held}{advice}")
        off = position_save(switches={"m_PlayerInVehicle": False, "m_PlayerInSnowShelter": False})
        self.assertEqual(edit.set_position(off, {"x": 5}).changes, (("x", "-1.464066", "5.0"),))

    def test_positions_that_cannot_be_read_are_refused(self) -> None:
        message = ("the position is not stored the way this tool expects (m_PlayerManagerSerialized > "
                   "m_SaveGamePosition), so it is not changed")
        gone = {"m_PlayerManagerSerialized": None}
        for name, save in (("two numbers", position_save(STANDS[:2])),
                           ("a number that is not finite", position_save(("1.0", "Infinity", "3.0"))),
                           ("text for a number", position_save(("1.0", NestedJson("2.0"), "3.0"))),
                           ("no record of the player", position_save(records=gone))):
            with self.subTest(name):
                self.assertEqual(self.refusal({"x": 5}, save), message)

    def test_one_refused_number_stops_the_whole_edit(self) -> None:
        original = position_save()
        kept = bytes(original)
        for values in ({"x": 5, "y": 100000.5}, {"x": 5, "z": 0.00001}):
            with self.subTest(values=values):
                with self.assertRaises(EditError):
                    edit.set_position(original, values)
        self.assertEqual(original, kept)

    def test_what_else_the_save_holds_about_the_place_stays(self) -> None:
        result = edit.set_position(position_save(), {"x": 5, "y": 6, "z": 7})
        document = codec.decode_save(result.data).entries["global"]
        manager = document["m_PlayerManagerSerialized"].value
        self.assertEqual(manager["m_PlayerInVehicleCameraPos"],
                         [Number("-1.464066"), Number("1.2198925"), Number("-1.0880059")])
        self.assertEqual(manager["m_SaveGameRotation"][1], Number("0.9909711"))
        before = codec.decode_save(position_save())
        self.assertEqual(codec.decode_save(result.data).entries["boot"], before.entries["boot"])
        self.assertEqual(document["m_GameManagerSerialized"],
                         before.entries["global"]["m_GameManagerSerialized"])

    def test_the_original_bytes_are_left_alone(self) -> None:
        original = position_save()
        kept = bytes(original)
        edit.set_position(original, {"x": 5, "y": 6, "z": 7})
        self.assertEqual(original, kept)

    def test_saves_that_fail_the_rebuild_check_are_not_edited(self) -> None:
        self.assertIn("does not pass the exact rebuild check", self.refusal({"x": 5}, b"not a save"))

    def caught(self, wrong: bytes) -> str:
        """What the tool says when the encoder hands back wrong in place of the save moved to x 5 and z 7."""
        with encoding_as(position_save(("5.0", "-0.5301075", "7.0")), wrong):
            with self.assertRaises(EditError) as refused:
                edit.set_position(position_save(), {"x": 5, "z": 7})
        return str(refused.exception)

    def test_a_number_that_was_not_written_is_caught(self) -> None:
        message = self.caught(position_save(("5.0", "-0.5301075", "-1.0880059")))
        self.assertIn("did not change exactly the 2 values asked for", message)
        self.assertIn(f"1 asked for and not made ({POSITION_PATH}[2]: -1.0880059 -> 7.0)", message)

    def test_a_number_written_wrongly_is_caught(self) -> None:
        message = self.caught(position_save(("5.0", "-0.5301075", "7.5")))
        self.assertIn(f"{POSITION_PATH}[2]: -1.0880059 -> 7.5", message)

    def test_a_number_that_was_not_asked_for_is_caught(self) -> None:
        message = self.caught(position_save(("5.0", "0.0", "7.0")))
        self.assertIn(f"1 not asked for ({POSITION_PATH}[1]: -0.5301075 -> 0.0)", message)

    def test_a_change_to_the_place_of_the_camera_is_caught(self) -> None:
        save = codec.decode_save(position_save(("5.0", "-0.5301075", "7.0")))
        manager = save.entries["global"]["m_PlayerManagerSerialized"].value
        manager["m_PlayerInVehicleCameraPos"][0] = Number("5.0")
        message = self.caught(codec.encode_save(save))
        self.assertIn("1 not asked for (m_Dict > global > m_PlayerManagerSerialized > "
                      "m_PlayerInVehicleCameraPos[0]: -1.464066 -> 5.0)", message)


class UnreadDocumentTests(unittest.TestCase):
    def edits(self) -> Sequence[Tuple[str, bytes, Callable[[bytes], Any]]]:
        """One edit of every kind, each with a save that it changes."""
        return (
            ("set-count", build_save(), lambda b: edit.set_stack_count(b, "GEAR_WoodMatches", 99)),
            ("set-condition", build_save(), lambda b: edit.set_item_condition(b, "GEAR_Hatchet", 100)),
            ("set-liters", liquids_save(),
             lambda b: edit.set_item_liters(b, "GEAR_WaterSupplyPotable", Decimal(1))),
            ("set-calories", foods_save(), lambda b: edit.set_item_calories(b, "GEAR_MRE", 500, entry=1)),
            ("set-rounds", weapons_save(), lambda b: edit.set_weapon_rounds(b, "GEAR_Rifle", 10)),
            ("repair-all", repair_save(), edit.repair_inventory),
            ("remove-item", removal_save(), lambda b: edit.remove_item(b, "GEAR_Hatchet")),
            ("copy-item", copy_save(), lambda b: edit.copy_item(b, "GEAR_MRE")),
            ("set-player", player_save(), lambda b: edit.set_player_values(b, {"condition": 100})),
            ("set-skill", skills_save(), lambda b: edit.set_skill_points(b, "cooking", 150)),
            ("cure", afflictions_save(hypothermia=True), edit.cure_afflictions),
            ("set-position", position_save(), lambda b: edit.set_position(b, {"x": 10})),
        )

    def with_text_in_boot(self, original: bytes, text: str) -> bytes:
        save = codec.decode_save(original)
        save.entries["boot"]["details"] = NestedJson({"sample": [text]})
        return codec.encode_save(save)

    def test_all_edits_refuse_a_document_kept_as_text_elsewhere(self) -> None:
        for name, original, run in self.edits():
            for text in ('[{"x": 1}]', '{"m_WearingProxy":true}\n'):
                with self.subTest(command=name, text=text):
                    with self.assertRaises(EditError) as refused:
                        run(self.with_text_in_boot(original, text))
                    self.assertIn("this save holds a nested document at m_Dict > boot > details > sample[0] "
                                  "that is not written the way the game writes one", str(refused.exception))

    def test_ordinary_text_in_brackets_stops_no_edit(self) -> None:
        for name, original, run in self.edits():
            for text in ("[Day 3] wolves at the dam [twice]", "{cache}", "[x]"):
                with self.subTest(command=name, text=text):
                    result = run(self.with_text_in_boot(original, text))
                    self.assertTrue(codec.verify_save(result.data).identical)

    def test_worn_socks_in_spaced_json_cannot_be_removed_or_copied(self) -> None:
        for run in (edit.remove_item, edit.copy_item):
            with self.subTest(command=run.__name__):
                save = codec.decode_save(copy_save())
                gear = edit._gear_of(edit._inventory_items(save)[2])
                assert gear is not None
                gear["m_ClothingItemSerialized"] = '{"m_WearingProxy": true}'
                with self.assertRaisesRegex(EditError, "nested document at .*m_ClothingItemSerialized"):
                    run(codec.encode_save(save), "GEAR_WoolSocks", 2)

    def test_invalid_clothing_record_is_refused(self) -> None:
        for clothing in ("x", NestedJson([]), {"m_WearingProxy": True}):
            save = codec.decode_save(copy_save())
            gear = edit._gear_of(edit._inventory_items(save)[2])
            assert gear is not None
            gear["m_ClothingItemSerialized"] = clothing
            for run in (edit.remove_item, edit.copy_item):
                with self.subTest(clothing=clothing, command=run.__name__):
                    with self.assertRaisesRegex(EditError, "whether you are wearing it is not stored"):
                        run(codec.encode_save(save), "GEAR_WoolSocks", 2)

    def test_rope_in_spaced_json_prevents_a_move(self) -> None:
        data = position_save(records={"m_PlayerClimbRopeSerialized": '{"m_RopeGuid": "climbing-rope"}'})
        with self.assertRaisesRegex(EditError, "nested document at .*m_PlayerClimbRopeSerialized"):
            edit.set_position(data, {"x": 10})

    def test_unreadable_rope_records_and_guid_prevent_a_move(self) -> None:
        for record in ("x", NestedJson([]), {"m_RopeGuid": "rope"}, NestedJson({"m_RopeGuid": Number("5")})):
            with self.subTest(record=record):
                data = position_save(records={"m_PlayerClimbRopeSerialized": record})
                with self.assertRaisesRegex(EditError, r"in a state this tool cannot read \(m_PlayerClimbRope"):
                    edit.set_position(data, {"x": 10})


class NewFileTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_writes_a_new_file(self) -> None:
        path = self.folder / "sandbox15.edited"
        edit.write_new_file(path, b"new content")
        self.assertEqual(path.read_bytes(), b"new content")

    def test_never_overwrites_an_existing_file(self) -> None:
        path = self.folder / "sandbox15.edited"
        path.write_bytes(b"already here")
        with self.assertRaises(EditError) as caught:
            edit.write_new_file(path, b"new content")
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(path.read_bytes(), b"already here")

    def test_refuses_a_file_that_appears_at_the_last_moment(self) -> None:
        path = self.folder / "sandbox15.edited"
        with mock.patch.object(edit, "check_new_file_path"):
            path.write_bytes(b"already here")
            with self.assertRaises(EditError) as caught:
                edit.write_new_file(path, b"new content")
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(path.read_bytes(), b"already here")

    def test_refuses_names_that_look_like_live_saves(self) -> None:
        for name in ("sandbox16", "challenge3", "ep1sandbox2", "story1", "relentless4"):
            with self.subTest(name):
                with self.assertRaises(EditError) as caught:
                    edit.write_new_file(self.folder / name, b"new content")
                self.assertIn("is named like a live save", str(caught.exception))
                self.assertFalse((self.folder / name).exists())

    def test_refuses_a_folder_that_does_not_exist(self) -> None:
        path = self.folder / "missing" / "sandbox15.edited"
        with self.assertRaises(EditError) as caught:
            edit.write_new_file(path, b"new content")
        self.assertIn("does not exist", str(caught.exception))
        self.assertFalse((self.folder / "missing").exists())

    def test_a_file_that_reads_back_differently_is_removed(self) -> None:
        path = self.folder / "sandbox15.edited"
        with mock.patch.object(Path, "read_bytes", return_value=b"something else"):
            with self.assertRaises(EditError) as caught:
                edit.write_new_file(path, b"new content")
        self.assertIn("did not read back as written", str(caught.exception))
        self.assertFalse(path.exists())

    def test_a_failed_write_leaves_nothing_behind(self) -> None:
        path = self.folder / "sandbox15.edited"
        with mock.patch.object(os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(EditError) as caught:
                edit.write_new_file(path, b"new content")
        self.assertIn("could not write", str(caught.exception))
        self.assertFalse(path.exists())


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.save = self.folder / "sandbox15"
        self.save.write_bytes(build_save())
        os.utime(self.save, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        self.out = self.folder / "sandbox15.edited"

    def run_command(self, *arguments: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = edit.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def snapshot(self) -> Tuple[str, int, List[str]]:
        return (hashlib.sha256(self.save.read_bytes()).hexdigest(), self.save.stat().st_mtime_ns,
                sorted(entry.name for entry in self.folder.iterdir()))

    def test_stacks_lists_the_inventory(self) -> None:
        code, out, err = self.run_command("stacks", str(self.save))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["entry", "item", "count", "condition"])
        self.assertEqual([line.split() for line in lines[1:5]], [
            ["0", "GEAR_WoodMatches", "23", "100.0%"],
            ["2", "GEAR_BottleAntibiotics", "6", "50.0%"],
            ["3", "GEAR_BottleAntibiotics", "12", "25.0%"],
            ["4", "GEAR_Stick", "122", "100.0%"],
        ])
        self.assertEqual(lines[5], "4 stacks among 5 inventory items in sandbox15.")
        self.assertEqual(err, "")

    def test_items_lists_everything(self) -> None:
        code, out, err = self.run_command("items", str(self.save))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["entry", "item", "count", "condition"])
        self.assertEqual([line.split() for line in lines[1:6]], [
            ["0", "GEAR_WoodMatches", "23", "100.0%"],
            ["1", "GEAR_Hatchet", "75.0%"],
            ["2", "GEAR_BottleAntibiotics", "6", "50.0%"],
            ["3", "GEAR_BottleAntibiotics", "12", "25.0%"],
            ["4", "GEAR_Stick", "122", "100.0%"],
        ])
        self.assertEqual(lines[6], "5 inventory items in sandbox15.")
        self.assertEqual(err, "")

    def test_set_condition_writes_the_edited_save_to_a_new_file(self) -> None:
        before = self.snapshot()
        code, out, err = self.run_command("set-condition", str(self.save), "--item", "GEAR_Hatchet",
                                          "--condition", "100", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), build_save(with_condition(DEFAULT_ITEMS, 1, "1.0")))
        self.assertIn("GEAR_Hatchet, inventory entry 1", out)
        self.assertIn("condition 75.0% -> 100.0% (stored as 0.75 -> 1.0)", out)
        self.assertIn("differs from the original in exactly this one value", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_condition_with_an_entry_number_and_a_fraction_of_a_percent(self) -> None:
        code, out, _ = self.run_command("set-condition", str(self.save), "--item", "GEAR_BottleAntibiotics",
                                        "--entry", "2", "--condition", "62.5", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_bytes(), build_save(with_condition(DEFAULT_ITEMS, 2, "0.625")))

    def test_set_condition_refusals_write_nothing(self) -> None:
        hatchet = ["--item", "GEAR_Hatchet", "--condition", "100"]
        cases = {
            "several of the item": (["--item", "GEAR_BottleAntibiotics", "--condition", "90", "--out",
                                     str(self.out)], "Add --entry to say which one"),
            "unknown item": (["--item", "GEAR_Nope", "--condition", "90", "--out", str(self.out)],
                             "there is no GEAR_Nope in the inventory"),
            "same condition": (["--item", "GEAR_Hatchet", "--condition", "75", "--out", str(self.out)],
                               "already has a condition of 75%"),
            "condition too high": (["--item", "GEAR_Hatchet", "--condition", "101", "--out", str(self.out)],
                                   "from 1 to 100"),
            "output is the save itself": (hatchet + ["--out", str(self.save)], "is named like a live save"),
            "output folder missing": (hatchet + ["--out", str(self.folder / "nowhere" / "x.edited")],
                                      "does not exist"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("set-condition", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)

    def test_set_count_writes_the_edited_save_to_a_new_file(self) -> None:
        before = self.snapshot()
        code, out, err = self.run_command("set-count", str(self.save), "--item", "GEAR_WoodMatches",
                                          "--count", "40", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), build_save(with_count(DEFAULT_ITEMS, 0, "40")))
        self.assertIn("count 23 -> 40", out)
        self.assertIn("differs from the original in exactly this one value", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_count_with_an_entry_number(self) -> None:
        code, out, _ = self.run_command("set-count", str(self.save), "--item", "GEAR_BottleAntibiotics",
                                        "--entry", "3", "--count", "30", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("GEAR_BottleAntibiotics, inventory entry 3", out)
        self.assertEqual(self.out.read_bytes(), build_save(with_count(DEFAULT_ITEMS, 3, "30")))

    def test_refusals_write_nothing(self) -> None:
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        matches = ["--item", "GEAR_WoodMatches", "--count", "40"]
        cases = {
            "several stacks": (["--item", "GEAR_BottleAntibiotics", "--count", "30", "--out", str(self.out)],
                               "Add --entry to say which one"),
            "unknown item": (["--item", "GEAR_Nope", "--count", "30", "--out", str(self.out)],
                             "there is no GEAR_Nope in the inventory"),
            "same count": (["--item", "GEAR_WoodMatches", "--count", "23", "--out", str(self.out)],
                           "already has a count of 23"),
            "count too large": (["--item", "GEAR_WoodMatches", "--count", "10000", "--out", str(self.out)],
                                "from 1 to 9999"),
            "output exists": (matches + ["--out", str(existing)], "already exists"),
            "output is the save itself": (matches + ["--out", str(self.save)], "is named like a live save"),
            "output named like a live save": (matches + ["--out", str(self.folder / "sandbox16")],
                                              "is named like a live save"),
            "output folder missing": (matches + ["--out", str(self.folder / "nowhere" / "x.edited")],
                                      "does not exist"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("set-count", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_an_unusable_output_path_is_refused_before_any_edit_work(self) -> None:
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        for name, out in (("exists", existing), ("live save name", self.folder / "sandbox16"),
                          ("folder missing", self.folder / "nowhere" / "x.edited")):
            with self.subTest(name):
                with mock.patch.object(edit, "set_stack_count") as attempted:
                    code, _, _ = self.run_command("set-count", str(self.save), "--item", "GEAR_WoodMatches",
                                                  "--count", "40", "--out", str(out))
                self.assertEqual(code, 1)
                attempted.assert_not_called()
        self.assertEqual(existing.read_bytes(), b"already here")

    def test_a_save_that_is_not_a_save_is_refused(self) -> None:
        bad = self.folder / "broken"
        bad.write_bytes(b"this is not a save")
        code, _, err = self.run_command("stacks", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("Refused:", err)
        code, _, err = self.run_command("set-count", str(bad), "--item", "GEAR_Stick", "--count", "2",
                                        "--out", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("does not pass the exact rebuild check", err)
        self.assertFalse(self.out.exists())

    def test_a_missing_save_is_a_usage_error(self) -> None:
        for arguments in (["stacks", str(self.folder / "sandbox99")],
                          ["set-count", str(self.folder / "sandbox99"), "--item", "GEAR_Stick", "--count", "2",
                           "--out", str(self.out)]):
            with self.subTest(arguments[0]):
                code, out, err = self.run_command(*arguments)
                self.assertEqual(code, 2)
                self.assertIn("Not found", err)
                self.assertEqual(out, "")
        self.assertFalse(self.out.exists())

    def test_liquids_lists_the_items_that_hold_liquid(self) -> None:
        self.save.write_bytes(liquids_save())
        code, out, err = self.run_command("liquids", str(self.save))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["entry", "item", "liters"])
        self.assertEqual([line.split() for line in lines[1:6]], [
            ["0", "GEAR_WaterSupplyPotable", "1.9048"],
            ["2", "GEAR_LampFuel", "0.171669576"],
            ["3", "GEAR_LampFuel", "0.5"],
            ["4", "GEAR_KeroseneLampB", "1"],
            ["5", "GEAR_BottleHydrogenPeroxide", "not", "set"],
        ])
        self.assertEqual(lines[6], "5 items that hold liquid among 10 inventory items in sandbox15.")
        self.assertEqual(err, "")

    def test_liquids_on_a_save_without_liquid(self) -> None:
        code, out, _ = self.run_command("liquids", str(self.save))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "0 items that hold liquid among 5 inventory items in sandbox15.")

    def test_set_liters_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(liquids_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-liters", str(self.save), "--item", "GEAR_WaterSupplyPotable",
                                          "--liters", "2.5", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(amount_in(self.out.read_bytes(), 0, "m_WaterSupplySerialized", "m_VolumeProxy"),
                         Number("2500000000"))
        self.assertEqual(self.out.read_bytes(),
                         edit.set_item_liters(liquids_save(), "GEAR_WaterSupplyPotable", Decimal("2.5")).data)
        self.assertIn("GEAR_WaterSupplyPotable, inventory entry 0", out)
        self.assertIn("liters 1.9048 -> 2.5 (stored as 1904800000 -> 2500000000)", out)
        self.assertIn("differs from the original in exactly this one value", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_liters_no_longer_says_that_it_is_not_tested_in_the_game(self) -> None:
        self.save.write_bytes(liquids_save())
        code, out, _ = self.run_command("set-liters", str(self.save), "--item", "GEAR_BottleHydrogenPeroxide",
                                        "--liters", "0.5", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("liters not set -> 0.5 (stored as -214748364800000 -> 500000000)", out)
        self.assertNotIn("Untested", out)

    def test_whole_liters_are_printed_plainly(self) -> None:
        self.save.write_bytes(liquids_save())
        code, out, _ = self.run_command("set-liters", str(self.save), "--item", "GEAR_WaterSupplyPotable",
                                        "--liters", "20", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("liters 1.9048 -> 20 (stored as 1904800000 -> 20000000000)", out)

    def test_edits_tested_in_the_game_do_not_carry_that_line(self) -> None:
        code, out, _ = self.run_command("set-count", str(self.save), "--item", "GEAR_WoodMatches",
                                        "--count", "40", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertNotIn("Untested", out)
        self.assertEqual(edit.NOT_TESTED_IN_GAME, frozenset())
        self.assertEqual(edit.CURE_NOT_TESTED_IN_GAME, frozenset())

    def test_set_liters_refusals_write_nothing(self) -> None:
        self.save.write_bytes(liquids_save())
        water = ["--item", "GEAR_WaterSupplyPotable"]
        cases = {
            "several": (["--item", "GEAR_LampFuel", "--liters", "1"], "Add --entry to say which one"),
            "no liquid": (["--item", "GEAR_Hatchet", "--liters", "1"], "holds no amount of liquid"),
            "unknown item": (["--item", "GEAR_Nope", "--liters", "1"], "there is no GEAR_Nope in the"),
            "same amount": (water + ["--liters", "1.9048"], "already holds 1.9048"),
            "too much": (water + ["--liters", "1000"], "from 0 to 999"),
            "negative": (water + ["--liters=-1"], "from 0 to 999"),
            "too fine": (water + ["--liters", "0.0000000001"], "nine decimal places"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                before = self.snapshot()
                code, out, err = self.run_command("set-liters", str(self.save), *options,
                                                  "--out", str(self.out))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)

    def test_a_liters_value_that_is_not_a_number_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                edit.main(["set-liters", str(self.save), "--item", "GEAR_LampFuel", "--liters", "plenty",
                           "--out", str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("'plenty' is not a number of liters", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_foods_lists_the_items_with_calories(self) -> None:
        self.save.write_bytes(foods_save())
        code, out, err = self.run_command("foods", str(self.save))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "entry  item                      count    calories  full")
        self.assertEqual([line.split() for line in lines[1:8]], [
            ["0", "GEAR_CookedLakeWhiteFish", "689.2364", "461.6905"],
            ["1", "GEAR_MRE", "846.509155", "1750.0"],
            ["2", "GEAR_MRE", "1750.0", "1750.0"],
            ["3", "GEAR_CookedBarPemmican", "3", "450.0", "450.0"],
            ["4", "GEAR_EnergyBar", "250", "500.0"],
            ["5", "GEAR_GranolaBar", "29.59488"],
            ["11", "GEAR_CattailStalk", "1", "150.0", "150.0"],
        ])
        self.assertEqual(lines[4], "    3  GEAR_CookedBarPemmican        3       450.0  450.0")
        self.assertEqual(lines[6], "    5  GEAR_GranolaBar                    29.59488")
        self.assertEqual(lines[8], "7 items with calories among 12 inventory items in sandbox15.")
        self.assertEqual(err, "")

    def test_foods_on_a_save_without_food(self) -> None:
        code, out, _ = self.run_command("foods", str(self.save))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["entry  item  count  calories  full",
                                            "0 items with calories among 5 inventory items in sandbox15."])

    def test_set_calories_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(foods_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-calories", str(self.save), "--item", "GEAR_CookedLakeWhiteFish",
                                          "--calories", "500", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), foods_save({0: "500.0"}))
        self.assertIn("GEAR_CookedLakeWhiteFish, inventory entry 0", out)
        self.assertIn("Change:   calories 689.2364 -> 500.0\n", out)
        self.assertIn("differs from the original in exactly this one value", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertNotIn("Untested", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_calories_on_a_stack_says_that_the_figure_is_for_one_unit(self) -> None:
        self.save.write_bytes(foods_save())
        code, out, _ = self.run_command("set-calories", str(self.save), "--item", "GEAR_CookedBarPemmican",
                                        "--calories", "300.5", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_bytes(), foods_save({3: "300.5"}))
        self.assertIn("Change:   calories 450.0 -> 300.5 per unit, in a stack of 3\n", out)

    def test_a_stack_of_one_is_not_called_a_stack(self) -> None:
        self.save.write_bytes(foods_save())
        code, out, _ = self.run_command("set-calories", str(self.save), "--item", "GEAR_CattailStalk",
                                        "--calories", "75", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   calories 150.0 -> 75.0\n", out)

    def test_set_calories_with_an_entry_number(self) -> None:
        self.save.write_bytes(foods_save())
        code, out, _ = self.run_command("set-calories", str(self.save), "--item", "GEAR_MRE", "--entry", "2",
                                        "--calories", "875", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("GEAR_MRE, inventory entry 2", out)
        self.assertEqual(self.out.read_bytes(), foods_save({2: "875.0"}))

    def test_set_calories_refusals_write_nothing(self) -> None:
        self.save.write_bytes(foods_save())
        fish = ["--item", "GEAR_CookedLakeWhiteFish"]
        cases = {
            "several": (["--item", "GEAR_MRE", "--calories", "100"], "Add --entry to say which one"),
            "no calories": (["--item", "GEAR_Hatchet", "--calories", "100"], "holds no calories"),
            "unknown item": (["--item", "GEAR_Nope", "--calories", "100"], "there is no GEAR_Nope in the"),
            "same amount": (fish + ["--calories", "689.2364"], "already holds 689.2364 calories"),
            "too much": (fish + ["--calories", "10000"], "from 1 to 9999, not 10000"),
            "nothing left": (fish + ["--calories", "0"], "from 1 to 9999, not 0"),
            "negative": (fish + ["--calories=-1"], "from 1 to 9999, not -1"),
            "not a number": (fish + ["--calories", "nan"], "from 1 to 9999, not nan"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                before = self.snapshot()
                code, out, err = self.run_command("set-calories", str(self.save), *options,
                                                  "--out", str(self.out))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)

    def test_a_calories_value_that_is_not_a_number_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                edit.main(["set-calories", str(self.save), "--item", "GEAR_MRE", "--calories", "plenty",
                           "--out", str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--calories", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_weapons_lists_the_items_that_can_hold_rounds(self) -> None:
        self.save.write_bytes(weapons_save())
        code, out, err = self.run_command("weapons", str(self.save))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "entry  item                rounds  spent casings  holds")
        self.assertEqual([line.split() for line in lines[1:9]], [
            ["0", "GEAR_Revolver", "5", "1", "6"],
            ["1", "GEAR_Revolver", "2", "0", "6"],
            ["2", "GEAR_Rifle", "0", "0", "10"],
            ["3", "GEAR_Rifle_Vaughns", "10", "0", "10"],
            ["4", "GEAR_FlareGun", "0", "0", "1"],
            ["5", "GEAR_Camera", "0", "0"],
            ["6", "GEAR_RevolverGreen", "4", "0", "6"],
            ["7", "GEAR_RevolverFancy", "0", "6", "6"],
        ])
        self.assertEqual(lines[1], "    0  GEAR_Revolver            5              1      6")
        self.assertEqual(lines[6], "    5  GEAR_Camera              0              0")
        self.assertEqual(lines[9], "8 items that can hold rounds among 14 inventory items in sandbox15.")
        self.assertEqual(err, "")

    def test_weapons_on_a_save_without_weapons(self) -> None:
        code, out, _ = self.run_command("weapons", str(self.save))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "entry  item  rounds  spent casings  holds",
            "0 items that can hold rounds among 5 inventory items in sandbox15."])

    def test_weapons_counts_a_single_item_as_one_item(self) -> None:
        inventory: Dict[str, JsonValue] = {"m_SerializedItems": [weapon_item("GEAR_Rifle", clip())],
                                           "m_QuickSelectInstanceIDs": []}
        self.save.write_bytes(build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)}))
        code, out, _ = self.run_command("weapons", str(self.save))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1],
                         "1 item that can hold rounds among 1 inventory item in sandbox15.")

    def test_set_rounds_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(weapons_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-rounds", str(self.save), "--item", "GEAR_Revolver",
                                          "--entry", "1", "--rounds", "6", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), weapons_save({1: ["100"] * 6}))
        self.assertIn("GEAR_Revolver, inventory entry 1", out)
        self.assertIn("Change:   rounds 2 -> 6 (GEAR_Revolver holds 6)\n", out)
        self.assertIn("differs from the original in exactly this one value", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertNotIn("Untested", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_rounds_without_an_entry_number(self) -> None:
        self.save.write_bytes(weapons_save())
        code, out, _ = self.run_command("set-rounds", str(self.save), "--item", "GEAR_Rifle", "--rounds", "3",
                                        "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   rounds 0 -> 3 (GEAR_Rifle holds 10)\n", out)
        self.assertEqual(self.out.read_bytes(), weapons_save({2: ["100"] * 3}))

    def test_set_rounds_refusals_write_nothing(self) -> None:
        self.save.write_bytes(weapons_save())
        rifle = ["--item", "GEAR_Rifle"]
        cases = {
            "several": (["--item", "GEAR_Revolver", "--rounds", "6"], "Add --entry to say which one"),
            "no rounds": (["--item", "GEAR_Hatchet", "--rounds", "1"], "holds no rounds"),
            "unknown item": (["--item", "GEAR_Nope", "--rounds", "1"], "there is no GEAR_Nope in the"),
            "same number": (rifle + ["--rounds", "0"], "already holds 0 rounds"),
            "too many": (rifle + ["--rounds", "11"], "GEAR_Rifle holds 10 rounds, so 11 do not fit"),
            "spent casings in the way": (["--item", "GEAR_Revolver", "--entry", "0", "--rounds", "6"],
                                         "so at most 5 fit"),
            "no number for it": (["--item", "GEAR_Camera", "--rounds", "1"], "has no number for the rounds"),
            "negative": (rifle + ["--rounds=-1"], "from 0 up, not -1"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                before = self.snapshot()
                code, out, err = self.run_command("set-rounds", str(self.save), *options,
                                                  "--out", str(self.out))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)

    def test_a_rounds_value_that_is_not_a_whole_number_is_a_usage_error(self) -> None:
        for value in ("two", "2.5"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as caught:
                        edit.main(["set-rounds", str(self.save), "--item", "GEAR_Rifle", "--rounds", value,
                                   "--out", str(self.out)])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--rounds", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_repair_all_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(repair_save())
        before = self.snapshot()
        code, out, err = self.run_command("repair-all", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), repair_save(repaired=REPAIRED))
        self.assertIn("Change:   condition set to 100% for 7 of the 25 items in the inventory\n", out)
        self.assertIn("Left:     5 at 100% already, 2 ruined, 4 burned torches or flares, 7 with a condition "
                      "this tool does not change\n", out)
        self.assertIn("Checked:  the edited save differs from the original in exactly these 7 values\n", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertNotIn("Untested", out)
        self.assertNotIn("Item:", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_repair_all_of_one_item_says_one_value(self) -> None:
        self.save.write_bytes(build_save(with_condition(with_condition(DEFAULT_ITEMS, 2, "1.0"), 3, "1.0")))
        code, out, _ = self.run_command("repair-all", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   condition set to 100% for 1 of the 5 items in the inventory\n", out)
        self.assertIn("Left:     4 at 100% already, 0 ruined, 0 burned torches or flares, 0 with a condition "
                      "this tool does not change\n", out)
        self.assertIn("Checked:  the edited save differs from the original in exactly this one value\n", out)
        self.assertEqual(self.out.read_bytes(), build_save([(prefab, count, "1.0")
                                                            for prefab, count, _ in DEFAULT_ITEMS]))

    def test_repair_all_refusals_write_nothing(self) -> None:
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        full = self.folder / "full"
        full.write_bytes(build_save([(prefab, count, "1.0") for prefab, count, _ in DEFAULT_ITEMS]))
        cases = {
            "nothing to repair": ([str(full), "--out", str(self.out)],
                                  "there is nothing to repair in the inventory"),
            "output exists": ([str(self.save), "--out", str(existing)], "already exists"),
            "output named like a live save": ([str(self.save), "--out", str(self.folder / "sandbox16")],
                                              "is named like a live save"),
            "output folder missing": ([str(self.save), "--out", str(self.folder / "nowhere" / "x.edited")],
                                      "does not exist"),
        }
        before = self.snapshot()
        for name, (arguments, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("repair-all", *arguments)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_repair_all_checks_the_output_path_before_any_edit_work(self) -> None:
        with mock.patch.object(edit, "repair_inventory") as attempted:
            code, _, _ = self.run_command("repair-all", str(self.save), "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_repair_all_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("repair-all", str(self.folder / "sandbox99"), "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)
        self.assertFalse(self.out.exists())

    def test_repair_all_takes_no_item(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                edit.main(["repair-all", str(self.save), "--item", "GEAR_Hatchet", "--out", str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.out.exists())

    def test_remove_item_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(removal_save())
        before = self.snapshot()
        code, out, err = self.run_command("remove-item", str(self.save), "--item", "GEAR_Hatchet",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), removal_save(without=1))
        self.assertIn("Item:     GEAR_Hatchet, inventory entry 1\n", out)
        self.assertIn("Change:   removed (condition 75.0%). 18 entries are left in the inventory\n", out)
        self.assertIn("Note:     every entry number above 1 is now one lower. Run items again before the next "
                      "edit\n", out)
        self.assertIn("Checked:  the edited inventory is the original one without this entry, and nothing else "
                      "differs\n", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertNotIn("Untested", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_remove_item_says_what_a_stack_held(self) -> None:
        self.save.write_bytes(removal_save())
        code, out, _ = self.run_command("remove-item", str(self.save), "--item", "GEAR_BottleAntibiotics",
                                        "--entry", "3", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   removed (a stack of 12, condition 25.0%). 18 entries are left", out)
        self.assertEqual(self.out.read_bytes(), removal_save(without=3))

    def test_remove_item_with_one_entry_left_and_no_condition(self) -> None:
        made = [item("GEAR_Stone", None, "1.0"), item("GEAR_Stick", None, "1.0")]
        stone, stick = (one["m_SerializedGear"].value for one in made)          # type: ignore[union-attr]
        stone["m_InstanceIDProxy"] = Number("-2002000")
        stick["m_InstanceIDProxy"] = Number("-2002001")
        stick["m_NormalizedCondition"] = "good"
        inventory: Dict[str, JsonValue] = {"m_SerializedItems": list(made), "m_QuickSelectInstanceIDs": []}
        self.save.write_bytes(build_save(global_document={"m_Inventory_Serialized": NestedJson(inventory)}))
        code, out, _ = self.run_command("remove-item", str(self.save), "--item", "GEAR_Stick",
                                        "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   removed (no condition). 1 entry is left in the inventory\n", out)

    def test_remove_item_refusals_write_nothing(self) -> None:
        self.save.write_bytes(removal_save())
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        cases = {
            "in the player's hands": (["--item", "GEAR_Charcoal", "--out", str(self.out)],
                                      "it is in your hands"),
            "worn": (["--item", "GEAR_WoolSocks", "--entry", "7", "--out", str(self.out)],
                     "you are wearing it"),
            "water": (["--item", "GEAR_WaterSupplyPotable", "--out", str(self.out)], "the water you carry"),
            "several": (["--item", "GEAR_BottleAntibiotics", "--out", str(self.out)], "Add --entry"),
            "unknown item": (["--item", "GEAR_Nope", "--out", str(self.out)], "there is no GEAR_Nope in the"),
            "output exists": (["--item", "GEAR_Hatchet", "--out", str(existing)], "already exists"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("remove-item", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_remove_item_checks_the_output_path_before_any_edit_work(self) -> None:
        with mock.patch.object(edit, "remove_item") as attempted:
            code, _, _ = self.run_command("remove-item", str(self.save), "--item", "GEAR_Hatchet",
                                          "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_remove_item_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("remove-item", str(self.folder / "sandbox99"), "--item", "GEAR_Stick",
                                          "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_copy_item_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(copy_save())
        before = self.snapshot()
        code, out, err = self.run_command("copy-item", str(self.save), "--item", "GEAR_MRE",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), copy_save(copy_of=0))
        self.assertIn("Item:     GEAR_MRE, inventory entry 0\n", out)
        self.assertIn("Change:   copied to a new entry at the end of the inventory, entry 17\n", out)
        self.assertIn("Number:   the copy has the instance number -5240390. The original keeps -1001000\n", out)
        self.assertIn("Checked:  the edited inventory is the original one plus this copy, and nothing else "
                      "differs\n", out)
        self.assertIn("2 of 3 entries are byte for byte unchanged", out)
        self.assertNotIn("Untested", out)
        self.assertIn(hashlib.sha256(self.out.read_bytes()).hexdigest(), out)
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_copy_item_says_what_a_stack_holds(self) -> None:
        self.save.write_bytes(copy_save())
        code, out, _ = self.run_command("copy-item", str(self.save), "--item", "GEAR_WoodMatches",
                                        "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("Change:   copied (a stack of 23) to a new entry at the end of the inventory, entry 17\n",
                      out)

    def test_copy_item_with_an_entry_number(self) -> None:
        self.save.write_bytes(copy_save())
        code, out, _ = self.run_command("copy-item", str(self.save), "--item", "GEAR_WoolSocks", "--entry", "3",
                                        "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_bytes(), copy_save(copy_of=3))

    def test_copy_item_refusals_write_nothing(self) -> None:
        self.save.write_bytes(copy_save())
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        cases = {
            "worn": (["--item", "GEAR_WoolSocks", "--entry", "2", "--out", str(self.out)],
                     "you are wearing it"),
            "water": (["--item", "GEAR_WaterSupplyPotable", "--out", str(self.out)], "the water you carry"),
            "named in another way": (["--item", "GEAR_BedRoll", "--out", str(self.out)],
                                     "names this one item in another way too"),
            "several": (["--item", "GEAR_WoolSocks", "--out", str(self.out)], "Add --entry"),
            "unknown item": (["--item", "GEAR_Nope", "--out", str(self.out)], "there is no GEAR_Nope in the"),
            "output exists": (["--item", "GEAR_MRE", "--out", str(existing)], "already exists"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("copy-item", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_copy_item_checks_the_output_path_before_any_edit_work(self) -> None:
        with mock.patch.object(edit, "copy_item") as attempted:
            code, _, _ = self.run_command("copy-item", str(self.save), "--item", "GEAR_Hatchet",
                                          "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_copy_item_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("copy-item", str(self.folder / "sandbox99"), "--item", "GEAR_Stick",
                                          "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_player_lists_the_five_values(self) -> None:
        self.save.write_bytes(player_save())
        before = self.snapshot()
        code, out, err = self.run_command("player", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "value      in the game        in the save",
            "condition  76.7%              m_CurrentHPProxy 76.66127",
            "calories   1387.0             m_CurrentReserveCaloriesProxy 1386.98242",
            "hydration  78.2% of the bar   m_CurrentThirstProxy 21.7523441",
            "rest       90.5% of the bar   m_CurrentFatigueProxy 9.5124445",
            "warmth     100.0% of the bar  m_CurrentFreezingProxy is left out, which means 0",
            "The player's 5 values in sandbox15.",
            "For hydration, rest and warmth the save holds the inverse of the bar: 0 is a full bar, and 100 an "
            "empty one.",
        ])
        self.assertEqual(self.snapshot(), before)

    def test_player_marks_a_value_it_cannot_read(self) -> None:
        self.save.write_bytes(player_save(records={"m_Thirst_Serialized": None}))
        code, out, err = self.run_command("player", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[3], "hydration  cannot be read     m_CurrentThirstProxy is not "
                                              "stored the way this tool expects")

    def test_player_on_something_that_is_no_save(self) -> None:
        self.save.write_bytes(b"not a save")
        code, out, err = self.run_command("player", str(self.save))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Refused:", err)
        code, out, err = self.run_command("player", str(self.folder / "sandbox99"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_set_player_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(player_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-player", str(self.save), "--rest", "100", "--condition", "100",
                                          "--calories", "2500", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(),
                         player_save({"condition": "100.0", "calories": "2500.0", "rest": "0.0"}))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:     sandbox15, {len(player_save()):,} bytes, passes the exact "
                                   "rebuild check")
        self.assertEqual(lines[1:6], [
            "Change:   condition 76.7% -> 100.0% (stored as m_CurrentHPProxy 76.66127 -> 100.0)",
            "Change:   calories 1387.0 -> 2500.0 (stored as m_CurrentReserveCaloriesProxy 1386.98242 -> "
            "2500.0)",
            "Change:   rest 90.5% of the bar -> 100.0% of the bar (stored as m_CurrentFatigueProxy 9.5124445 "
            "-> 0.0)",
            "Checked:  the edited save differs from the original in exactly these 3 values",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])
        self.assertEqual(lines[6], f"Written:  {self.out}, {len(self.out.read_bytes()):,} bytes")
        self.assertEqual(lines[7], f"SHA-256:  {hashlib.sha256(self.out.read_bytes()).hexdigest()}")
        self.assertEqual(lines[8:], ["sandbox15 itself was not changed."])
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_player_says_when_it_puts_a_number_in(self) -> None:
        self.save.write_bytes(player_save())
        code, out, err = self.run_command("set-player", str(self.save), "--warmth", "60", "--rest", "100",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), player_save({"rest": "0.0", "warmth": "40.0"}))
        self.assertEqual(out.splitlines()[1:6], [
            "Change:   rest 90.5% of the bar -> 100.0% of the bar (stored as m_CurrentFatigueProxy 9.5124445 "
            "-> 0.0)",
            "Change:   warmth 100.0% of the bar -> 60.0% of the bar (the game had left m_CurrentFreezingProxy "
            "out of the save, which means 0. It is put in as 40.0)",
            "Checked:  the edited save differs from the original in exactly these 2 values",
            "Checked:  the number that was put in is the first in its record, where the game writes it, and "
            "nothing else in that record differs",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])

    def test_set_player_counts_the_numbers_it_puts_in(self) -> None:
        self.save.write_bytes(player_save({"rest": None}))
        code, out, err = self.run_command("set-player", str(self.save), "--warmth", "60", "--rest", "50",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("Checked:  the 2 numbers that were put in are the first in its record, where the game "
                      "writes it, and nothing else in that record differs\n", out)

    def test_set_player_takes_each_value_by_its_own_option(self) -> None:
        held = player_save({"warmth": "82.56313"})
        for option, given, name, stored in (("--condition", "55.5", "condition", "55.5"),
                                            ("--calories", "2000", "calories", "2000.0"),
                                            ("--hydration", "100", "hydration", "0.0"),
                                            ("--rest", "25", "rest", "75.0"),
                                            ("--warmth", "90", "warmth", "10.0"),
                                            # 0 is a number like any other, and not a value left out.
                                            ("--calories", "0", "calories", "0.0"),
                                            ("--hydration", "0", "hydration", "100.0")):
            with self.subTest(option=option):
                self.save.write_bytes(held)
                out_file = self.folder / f"sandbox15.{name}{given}"
                code, out, err = self.run_command("set-player", str(self.save), option, given,
                                                  "--out", str(out_file))
                self.assertEqual(code, 0, err)
                self.assertEqual(out_file.read_bytes(), player_save({"warmth": "82.56313", name: stored}))
                self.assertIn("Checked:  the edited save differs from the original in exactly this one value\n",
                              out)

    def test_set_player_refusals_write_nothing(self) -> None:
        self.save.write_bytes(player_save())
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        cases = {
            "too high": (["--hydration", "101", "--out", str(self.out)],
                         "hydration must be a number from 0 to 100"),
            "left out and still 0": (["--warmth", "100", "--out", str(self.out)],
                                     "warmth is already 100.0% of the bar: the game leaves"),
            "no change": (["--condition", "76.66127", "--out", str(self.out)], "condition is already 76.7%"),
            "one of two is refused": (["--condition", "100", "--warmth", "100", "--out", str(self.out)],
                                      "warmth is already 100.0% of the bar"),
            "output exists": (["--condition", "100", "--out", str(existing)], "already exists"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_command("set-player", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(out, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_set_player_checks_the_output_path_before_any_edit_work(self) -> None:
        self.save.write_bytes(player_save())
        with mock.patch.object(edit, "set_player_values") as attempted:
            code, _, _ = self.run_command("set-player", str(self.save), "--condition", "100",
                                          "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_set_player_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("set-player", str(self.folder / "sandbox99"), "--condition", "100",
                                          "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_set_player_without_a_value_names_the_options(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            edit.main(["set-player", str(self.save), "--out", str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("set-player needs one or more of --condition, --calories, --hydration, --rest, --warmth",
                      err.getvalue())
        self.assertFalse(self.out.exists())

    def test_skills_lists_the_nine_skills(self) -> None:
        self.save.write_bytes(skills_save({"revolver": "0"}))
        before = self.snapshot()
        code, out, err = self.run_command("skills", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "skill               points  in the save",
            "fire-starting           12  m_Points 12",
            "carcass-harvesting       0  m_Points is left out, which means 0",
            "cooking                264  m_Points 264",
            "ice-fishing              0  m_Points is left out, which means 0",
            "rifle                    0  m_Points is left out, which means 0",
            "archery                 22  m_Points 22",
            "mending                  0  m_Points is left out, which means 0",
            "revolver                 0  m_Points 0",
            "gunsmithing              0  m_Points is left out, which means 0",
            "The 9 skills in sandbox15.",
            "The save holds points and no level. The game works the level out from the points.",
        ])
        self.assertEqual(self.snapshot(), before)

    def test_skills_marks_a_skill_it_cannot_read(self) -> None:
        self.save.write_bytes(skills_save(records={"m_Skill_CookingSerialized": None}))
        code, out, err = self.run_command("skills", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[3], "cooking                     cannot be read: m_Skill_Cooking"
                                              "Serialized is not stored the way this tool expects")
        self.assertEqual(out.splitlines()[2], "carcass-harvesting       0  m_Points is left out, which means 0")

    def test_skills_on_something_that_is_no_save(self) -> None:
        self.save.write_bytes(b"not a save")
        code, out, err = self.run_command("skills", str(self.save))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Refused:", err)
        code, out, err = self.run_command("skills", str(self.folder / "sandbox99"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_set_skill_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(skills_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-skill", str(self.save), "--skill", "cooking", "--points", "150",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), skills_save({"cooking": "150"}))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:     sandbox15, {len(skills_save()):,} bytes, passes the exact "
                                   "rebuild check")
        self.assertEqual(lines[1:5], [
            "Skill:    cooking (m_Skill_CookingSerialized)",
            "Change:   points 264 -> 150",
            "Checked:  the edited save differs from the original in exactly this one value",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])
        self.assertEqual(lines[5], f"Written:  {self.out}, {len(self.out.read_bytes()):,} bytes")
        self.assertEqual(lines[6], f"SHA-256:  {hashlib.sha256(self.out.read_bytes()).hexdigest()}")
        self.assertEqual(lines[7:], ["sandbox15 itself was not changed."])
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_skill_says_when_it_puts_the_points_in(self) -> None:
        self.save.write_bytes(skills_save())
        code, out, err = self.run_command("set-skill", str(self.save), "--skill", "carcass-harvesting",
                                          "--points", "7", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), skills_save({"carcass-harvesting": "7"}))
        self.assertEqual(out.splitlines()[1:6], [
            "Skill:    carcass-harvesting (m_Skill_CarcassHarvestingSerialized)",
            "Change:   points 0 -> 7 (the game had left m_Points out of the save, which means 0. It is put in "
            "as 7)",
            "Checked:  the edited save differs from the original in exactly this one value",
            "Checked:  the number that was put in is the first in its record, where the game writes it, and "
            "nothing else in that record differs",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])

    def test_set_skill_takes_zero_points(self) -> None:
        self.save.write_bytes(skills_save())
        code, out, err = self.run_command("set-skill", str(self.save), "--skill", "cooking", "--points", "0",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), skills_save({"cooking": "0"}))
        self.assertIn("Change:   points 264 -> 0\n", out)
        self.assertNotIn("put in", out)

    def test_set_skill_refusals_write_nothing(self) -> None:
        self.save.write_bytes(skills_save())
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        out = ["--out", str(self.out)]
        cases = {
            "too high": (["--skill", "cooking", "--points", "10000"] + out,
                         "the points must be a whole number from 0 to 9999, not 10000"),
            "below zero": (["--skill", "cooking", "--points", "-1"] + out,
                           "the points must be a whole number from 0 to 9999, not -1"),
            "no change": (["--skill", "cooking", "--points", "264"] + out, "cooking has 264 points already"),
            "left out and still 0": (["--skill", "rifle", "--points", "0"] + out,
                                     "rifle has 0 points already: the game leaves m_Points out"),
            "no such skill": (["--skill", "cookin", "--points", "5"] + out,
                              "there is no skill named cookin. Did you mean cooking?"),
            "output exists": (["--skill", "cooking", "--points", "150", "--out", str(existing)],
                              "already exists"),
        }
        before = self.snapshot()
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, printed, err = self.run_command("set-skill", str(self.save), *options)
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(printed, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")

    def test_set_skill_checks_the_output_path_before_any_edit_work(self) -> None:
        self.save.write_bytes(skills_save())
        with mock.patch.object(edit, "set_skill_points") as attempted:
            code, _, _ = self.run_command("set-skill", str(self.save), "--skill", "cooking", "--points", "150",
                                          "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_set_skill_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("set-skill", str(self.folder / "sandbox99"), "--skill", "cooking",
                                          "--points", "150", "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_afflictions_lists_the_five_kinds(self) -> None:
        self.save.write_bytes(afflictions_save(hypothermia=True, pain=1, wrists=2))
        before = self.snapshot()
        code, out, err = self.run_command("afflictions", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "affliction      cases  in the save",
            "hypothermia         1  m_HypothermiaSerialized",
            "sprain-pain         1  m_SprainPainSerialized",
            "sprained-ankle      0  m_SprainedAnkleSerialized",
            "sprained-wrist      2  m_SprainedWristSerialized",
            "burns               0  m_BurnsSerialized",
            "4 cases of these 5 kinds of affliction in sandbox15.",
            "A save can hold other afflictions, such as frostbite or food poisoning. This tool does not read "
            "them.",
        ])
        self.assertEqual(self.snapshot(), before)

    def test_afflictions_counts_one_case_as_one_case(self) -> None:
        self.save.write_bytes(afflictions_save(burns=True))
        code, out, err = self.run_command("afflictions", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[6], "1 case of these 5 kinds of affliction in sandbox15.")

    def test_afflictions_marks_a_kind_it_cannot_read(self) -> None:
        self.save.write_bytes(afflictions_save(wrists=1, records={"m_SprainPainSerialized": None}))
        code, out, err = self.run_command("afflictions", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[2], "sprain-pain            cannot be read: m_SprainPainSerialized "
                                              "is not stored the way this tool expects")
        self.assertEqual(out.splitlines()[6], "1 case of these 5 kinds of affliction in sandbox15.")

    def test_afflictions_on_something_that_is_no_save(self) -> None:
        self.save.write_bytes(b"not a save")
        code, out, err = self.run_command("afflictions", str(self.save))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Refused:", err)
        code, out, err = self.run_command("afflictions", str(self.folder / "sandbox99"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_cure_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(afflictions_save(hypothermia=True, pain=1, wrists=2))
        before = self.snapshot()
        code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(),
                         afflictions_save(hypothermia=True, pain=1, wrists=2, cured=EVERY_KIND))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:     sandbox15, {len(self.save.read_bytes()):,} bytes, passes the "
                                   "exact rebuild check")
        self.assertEqual(lines[1:6], [
            "Cured:    hypothermia, 1 case (m_HypothermiaSerialized: m_Active true -> false)",
            "Cured:    sprain-pain, 1 case (m_SprainPainSerialized: 3 lists emptied)",
            "Cured:    sprained-wrist, 2 cases (m_SprainedWristSerialized: 5 lists emptied)",
            "Checked:  the edited save differs from the original in exactly these 9 values",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])
        self.assertEqual(lines[6], f"Written:  {self.out}, {len(self.out.read_bytes()):,} bytes")
        self.assertEqual(lines[7], f"SHA-256:  {hashlib.sha256(self.out.read_bytes()).hexdigest()}")
        self.assertEqual(lines[8:], ["sandbox15 itself was not changed."])
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_cure_of_one_switch_says_one_value(self) -> None:
        self.save.write_bytes(afflictions_save(burns=True))
        code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), afflictions_save(burns=True, cured=("burns",)))
        self.assertEqual(out.splitlines()[1:3], [
            "Cured:    burns, 1 case (m_BurnsSerialized: m_Active true -> false)",
            "Checked:  the edited save differs from the original in exactly this one value",
        ])

    def test_cure_of_all_five_kinds_does_not_say_untested(self) -> None:
        # All five were loaded in the game on 2026-11-27.
        self.save.write_bytes(afflictions_save(hypothermia=True, burns=True, pain=1, ankles=1, wrists=1))
        code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        for name in edit.AFFLICTIONS:
            self.assertIn(f"Cured:    {name}, 1 case", out)
        self.assertNotIn("Untested", out)

    def test_cure_names_each_kind_that_is_not_tested_in_the_game(self) -> None:
        # No kind is in the set today. This keeps the line working for the next kind that cure learns.
        self.save.write_bytes(afflictions_save(hypothermia=True, burns=True, pain=1, ankles=1, wrists=1))
        waiting = frozenset({"hypothermia", "sprain-pain", "sprained-ankle"})
        with mock.patch.object(edit, "CURE_NOT_TESTED_IN_GAME", waiting):
            code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("Untested: no save in which cure ended hypothermia, sprain-pain or sprained-ankle has "
                      "been loaded in the game yet. Try it on a save you can afford to lose", out.splitlines())

    def test_cure_names_one_kind_that_is_not_tested_in_the_game(self) -> None:
        self.save.write_bytes(afflictions_save(ankles=2, wrists=1))
        with mock.patch.object(edit, "CURE_NOT_TESTED_IN_GAME", frozenset({"sprained-ankle"})):
            code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("Untested: no save in which cure ended sprained-ankle has been loaded in the game yet. "
                      "Try it on a save you can afford to lose", out.splitlines())

    def test_cure_of_one_kind_with_lists_counts_each_list(self) -> None:
        self.save.write_bytes(afflictions_save(wrists=1))
        code, out, err = self.run_command("cure", str(self.save), "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), afflictions_save(wrists=1, cured=("sprained-wrist",)))
        self.assertEqual(out.splitlines()[1:3], [
            "Cured:    sprained-wrist, 1 case (m_SprainedWristSerialized: 5 lists emptied)",
            "Checked:  the edited save differs from the original in exactly these 5 values",
        ])

    def test_cure_refusals_write_nothing(self) -> None:
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        broken = {"m_SprainedAnkleSerialized": NestedJson({"m_Locations": []})}
        cases = {
            "nothing to cure": (afflictions_save(), self.out, "there is nothing to cure: this save holds no "
                                                              "hypothermia, sprain-pain, sprained-ankle, "
                                                              "sprained-wrist or burns"),
            "a kind that cannot be read": (afflictions_save(burns=True, records=broken), self.out,
                                           "sprained-ankle (m_SprainedAnkleSerialized) is not stored the way "
                                           "this tool expects, so nothing is cured"),
            "output exists": (afflictions_save(burns=True), existing, "already exists"),
        }
        for name, (save, out_file, message) in cases.items():
            with self.subTest(name):
                self.save.write_bytes(save)
                before = self.snapshot()
                code, printed, err = self.run_command("cure", str(self.save), "--out", str(out_file))
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(printed, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")
                self.assertFalse(self.out.exists())

    def test_cure_checks_the_output_path_before_any_edit_work(self) -> None:
        self.save.write_bytes(afflictions_save(burns=True))
        with mock.patch.object(edit, "cure_afflictions") as attempted:
            code, _, _ = self.run_command("cure", str(self.save), "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_cure_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("cure", str(self.folder / "sandbox99"), "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_position_prints_the_scene_and_the_three_numbers(self) -> None:
        self.save.write_bytes(position_save())
        before = self.snapshot()
        code, out, err = self.run_command("position", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "Scene:     GreyMothersHouseA",
            "Position:  x -1.464066, y -0.5301075, z -1.0880059",
            "Where the character stands in sandbox15. y is the height. The numbers count inside this scene "
            "only.",
        ])
        self.assertEqual(self.snapshot(), before)

    def test_position_says_what_holds_the_character_in_place(self) -> None:
        self.save.write_bytes(position_save(switches={"m_PlayerInVehicle": True}))
        code, out, err = self.run_command("position", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1:3], [
            "Position:  x -1.464066, y -0.5301075, z -1.0880059",
            "Held:      the character is in a vehicle, so set-position does not move it",
        ])

    def test_position_marks_what_it_cannot_read(self) -> None:
        save = codec.decode_save(position_save(STANDS[:2]))
        save.slot["m_Dict"]["boot"] = {}
        self.save.write_bytes(codec.encode_save(save))
        code, out, err = self.run_command("position", str(self.save))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[:2], [
            "Scene:     the save does not name it",
            "Position:  cannot be read: m_PlayerManagerSerialized > m_SaveGamePosition is not stored the way "
            "this tool expects",
        ])

    def test_position_on_something_that_is_no_save(self) -> None:
        self.save.write_bytes(b"not a save")
        code, out, err = self.run_command("position", str(self.save))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Refused:", err)
        code, out, err = self.run_command("position", str(self.folder / "sandbox99"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_set_position_writes_the_edited_save_to_a_new_file(self) -> None:
        self.save.write_bytes(position_save())
        before = self.snapshot()
        code, out, err = self.run_command("set-position", str(self.save), "--x", "-0.797",
                                          "--y", "0.0165060163", "--z", "-1.38356638", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), position_save(("-0.797", "0.0165060163", "-1.38356638")))
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:     sandbox15, {len(position_save()):,} bytes, passes the exact "
                                   "rebuild check")
        self.assertEqual(lines[1:7], [
            "Scene:    GreyMothersHouseA. It is not changed, and the numbers count inside it only",
            "Change:   x -1.464066 -> -0.797",
            "Change:   y -0.5301075 -> 0.0165060163",
            "Change:   z -1.0880059 -> -1.38356638",
            "Checked:  the edited save differs from the original in exactly these 3 values",
            "Checked:  2 of 3 entries are byte for byte unchanged (all except global)",
        ])
        self.assertEqual(lines[7], f"Written:  {self.out}, {len(self.out.read_bytes()):,} bytes")
        self.assertEqual(lines[8], f"SHA-256:  {hashlib.sha256(self.out.read_bytes()).hexdigest()}")
        self.assertEqual(lines[9:], ["sandbox15 itself was not changed."])
        after = self.snapshot()
        self.assertEqual(after[:2], before[:2], "the original save changed")
        self.assertEqual(after[2], sorted(before[2] + ["sandbox15.edited"]))

    def test_set_position_names_a_number_that_stays(self) -> None:
        self.save.write_bytes(position_save())
        code, out, err = self.run_command("set-position", str(self.save), "--y", "-0.5301075", "--z", "0",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), position_save(("-1.464066", "-0.5301075", "0.0")))
        self.assertEqual(out.splitlines()[2:5], [
            "Change:   z -1.0880059 -> 0.0",
            "Same:     y is -0.5301075 already",
            "Checked:  the edited save differs from the original in exactly this one value",
        ])

    def test_set_position_takes_each_number_by_its_own_option(self) -> None:
        for option, given, stands in (("--x", "-5.5", ("-5.5", "-0.5301075", "-1.0880059")),
                                      ("--y", "12", ("-1.464066", "12.0", "-1.0880059")),
                                      ("--z", "0.25", ("-1.464066", "-0.5301075", "0.25")),
                                      # 0 is a number like any other, and not a number left out.
                                      ("--x", "0", ("0.0", "-0.5301075", "-1.0880059"))):
            with self.subTest(option=option, given=given):
                out_file = self.folder / f"sandbox15.{option[2:]}{given}"
                self.save.write_bytes(position_save())
                code, out, err = self.run_command("set-position", str(self.save), option, given,
                                                  "--out", str(out_file))
                self.assertEqual(code, 0, err)
                self.assertEqual(out_file.read_bytes(), position_save(stands))

    def test_set_position_takes_a_number_with_an_exponent_after_an_equals_sign(self) -> None:
        # The game writes a number this close to 0 with an exponent. Given back as it is, it stays.
        self.save.write_bytes(position_save(("6.53425", "-1.1920929E-07", "8.391384")))
        code, out, err = self.run_command("set-position", str(self.save), "--x", "7", "--y=-1.1920929E-07",
                                          "--out", str(self.out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.out.read_bytes(), position_save(("7.0", "-1.1920929E-07", "8.391384")))
        self.assertEqual(out.splitlines()[2:4], ["Change:   x 6.53425 -> 7.0",
                                                 "Same:     y is -1.1920929E-07 already"])

    def test_set_position_refusals_write_nothing(self) -> None:
        existing = self.folder / "taken.edited"
        existing.write_bytes(b"already here")
        cases = {
            "held in place": (position_save(switches={"m_PlayerInVehicle": True}), ["--x", "5"], self.out,
                              "the character is in a vehicle in this save"),
            "there already": (position_save(), ["--x", "-1.464066"], self.out,
                              "the character is at that position already"),
            "too far": (position_save(), ["--x", "100001"], self.out,
                        "x must be a number from -100000 to 100000, not 100001"),
            "one of two is refused": (position_save(), ["--x", "5", "--z", "0.00001"], self.out,
                                      "z 1e-05 is too close to 0"),
            "output exists": (position_save(), ["--x", "5"], existing, "already exists"),
        }
        for name, (save, options, out_file, message) in cases.items():
            with self.subTest(name):
                self.save.write_bytes(save)
                before = self.snapshot()
                code, printed, err = self.run_command("set-position", str(self.save), *options,
                                                      "--out", str(out_file))
                self.assertEqual(code, 1)
                self.assertIn("Refused:", err)
                self.assertIn(message, err)
                self.assertIn("Nothing was written.", err)
                self.assertEqual(printed, "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(existing.read_bytes(), b"already here")
                self.assertFalse(self.out.exists())

    def test_set_position_checks_the_output_path_before_any_edit_work(self) -> None:
        self.save.write_bytes(position_save())
        with mock.patch.object(edit, "set_position") as attempted:
            code, _, _ = self.run_command("set-position", str(self.save), "--x", "5",
                                          "--out", str(self.folder / "sandbox16"))
        self.assertEqual(code, 1)
        attempted.assert_not_called()

    def test_set_position_on_a_missing_save_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("set-position", str(self.folder / "sandbox99"), "--x", "5",
                                          "--out", str(self.out))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Not found", err)

    def test_set_position_without_a_number_names_the_options(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            edit.main(["set-position", str(self.save), "--out", str(self.out)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("set-position needs one or more of --x, --y, --z", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_missing_options_are_a_usage_error(self) -> None:
        for arguments in ([], ["set-count", str(self.save)], ["repair-all", str(self.save)],
                          ["set-position", str(self.save), "--x", "5"],
                          ["set-position", str(self.save), "--x", "east", "--out", str(self.out)],
                          ["set-position", str(self.save), "--x", "--out", str(self.out)],
                          ["cure", str(self.save)],
                          ["cure", str(self.save), "--only", "burns", "--out", str(self.out)],
                          ["set-skill", str(self.save), "--skill", "cooking", "--out", str(self.out)],
                          ["set-skill", str(self.save), "--points", "5", "--out", str(self.out)],
                          ["set-skill", str(self.save), "--skill", "cooking", "--points", "5"],
                          ["set-skill", str(self.save), "--skill", "cooking", "--points", "12.5",
                           "--out", str(self.out)],
                          ["set-player", str(self.save), "--condition", "100"],
                          ["set-player", str(self.save), "--condition", "--out", str(self.out)],
                          ["copy-item", str(self.save), "--out", str(self.out)],
                          ["remove-item", str(self.save), "--out", str(self.out)],
                          ["set-count", str(self.save), "--item", "GEAR_Stick", "--out", str(self.out)],
                          ["set-liters", str(self.save), "--item", "GEAR_LampFuel", "--out", str(self.out)],
                          ["set-calories", str(self.save), "--item", "GEAR_MRE", "--out", str(self.out)],
                          ["set-rounds", str(self.save), "--item", "GEAR_Rifle", "--out", str(self.out)],
                          ["set-count", str(self.save), "--item", "GEAR_Stick", "--count", "two",
                           "--out", str(self.out)]):
            with self.subTest(" ".join(arguments[:1]) or "no arguments"):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        edit.main(arguments)
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.out.exists())


@unittest.skipUnless(os.environ.get("TLD_SAVE_DIR"),
                     "set TLD_SAVE_DIR to a folder of real saves to run this")
class RealSaveTests(unittest.TestCase):
    def test_one_count_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_stacks(codec.decode_save(original))
                if not before:
                    continue
                target = before[0]
                new_count = target.count + 1
                result = edit.set_stack_count(original, target.prefab, new_count, entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_stacks(codec.decode_save(result.data))
                expected = [Stack(stack.entry, stack.prefab, new_count if stack.entry == target.entry
                                  else stack.count, stack.condition) for stack in before]
                self.assertEqual(after, expected)

    def test_one_condition_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_items(codec.decode_save(original))
                target = next((item for item in before if item.condition and item.condition != 50.0), None)
                if target is None:
                    continue
                result = edit.set_item_condition(original, target.prefab, 50, entry=target.entry)
                self.assertEqual(result.new_condition, "0.5")
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_items(codec.decode_save(result.data))
                expected = [Item(item.entry, item.prefab, item.count, 50.0 if item.entry == target.entry
                                 else item.condition) for item in before]
                self.assertEqual(after, expected)

    def test_one_amount_of_liquid_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        edited = 0
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                save = codec.decode_save(original)
                items = edit._inventory_items(save)
                before = edit.list_liquids(save)
                target = next((liquid for liquid in before
                               if liquid_literal(items, liquid).lstrip("-").isdigit()), None)
                if target is None:
                    continue
                liters = Decimal("0.125") if target.liters != Decimal("0.125") else Decimal("0.25")
                result = edit.set_item_liters(original, target.prefab, liters, entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_liquids(codec.decode_save(result.data))
                expected = [Liquid(liquid.entry, liquid.prefab, liters if liquid.entry == target.entry
                                   else liquid.liters) for liquid in before]
                self.assertEqual(after, expected)
                edited += 1
        self.assertTrue(edited, "no real save holds liquid stored as whole billionths")

    def test_each_real_liquid_with_ambiguous_units_is_refused(self) -> None:
        for path in codec.find_saves(Path(os.environ["TLD_SAVE_DIR"])):
            original = path.read_bytes()
            save = codec.decode_save(original)
            items = edit._inventory_items(save)
            for liquid in edit.list_liquids(save):
                literal = liquid_literal(items, liquid)
                if not literal.lstrip("-").isdigit():
                    with self.subTest(save=path.name, entry=liquid.entry, literal=literal):
                        check_ambiguous_liquid(self, original, liquid, literal)

    def test_every_amount_of_liquid_in_a_real_inventory_can_be_read(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for entry, item in enumerate(edit._inventory_items(save)):
                gear = item["m_SerializedGear"].value
                for record_field, amount_field in edit._LIQUID_FIELDS:
                    record = gear.get(record_field)
                    if isinstance(record, NestedJson) and isinstance(record.value, dict) \
                            and amount_field in record.value:
                        with self.subTest(save=path.name, entry=entry):
                            self.assertIsNotNone(edit._liquid_of(item))

    def test_one_amount_of_calories_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        edited = 0
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_foods(codec.decode_save(original))
                if not before:
                    continue
                target = before[0]
                calories = Decimal("125.5") if target.calories != Decimal("125.5") else Decimal("250.5")
                result = edit.set_item_calories(original, target.prefab, float(calories), entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_foods(codec.decode_save(result.data))
                expected = [Food(food.entry, food.prefab, food.count,
                                 calories if food.entry == target.entry else food.calories, food.full)
                            for food in before]
                self.assertEqual(after, expected)
                edited += 1
        self.assertTrue(edited, "no real save holds an item with calories")

    def test_every_amount_of_calories_in_a_real_inventory_can_be_read(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for entry, item in enumerate(edit._inventory_items(save)):
                record = item["m_SerializedGear"].value.get("m_FoodItemSerialized")
                if isinstance(record, NestedJson) and isinstance(record.value, dict) \
                        and "m_CaloriesRemainingProxy" in record.value:
                    with self.subTest(save=path.name, entry=entry):
                        self.assertIsNotNone(edit._food_of(item))

    def test_every_stored_amount_of_calories_is_in_the_form_this_tool_writes(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        checked = 0
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for entry, item in enumerate(edit._inventory_items(save)):
                food = edit._food_of(item)
                if food is not None and edit.MIN_CALORIES <= food.calories <= edit.MAX_CALORIES:
                    with self.subTest(save=path.name, entry=entry):
                        self.assertEqual(edit.float32_literal(float(food.literal)), food.literal)
                    checked += 1
        self.assertTrue(checked, "no real save holds an item with calories")

    def test_the_rounds_of_one_weapon_can_be_changed_in_every_real_save_that_carries_one(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        edited = 0
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_weapons(codec.decode_save(original))
                target = next((weapon for weapon in before
                               if weapon.holds is not None and weapon.holds > weapon.spent), None)
                if target is None or target.holds is None:
                    continue
                fits = target.holds - target.spent
                rounds = fits if target.rounds != fits else 0
                result = edit.set_weapon_rounds(original, target.prefab, rounds, entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_weapons(codec.decode_save(result.data))
                expected = [Weapon(weapon.entry, weapon.prefab,
                                   rounds if weapon.entry == target.entry else weapon.rounds,
                                   weapon.spent, weapon.holds) for weapon in before]
                self.assertEqual(after, expected)
                edited += 1
        if not edited:
            self.skipTest("no real save carries a weapon that this tool has a number for")

    def test_every_weapon_record_in_a_real_inventory_can_be_read(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for entry, item in enumerate(edit._inventory_items(save)):
                record = item["m_SerializedGear"].value.get("m_WeaponItemSerialized")
                if isinstance(record, NestedJson) and isinstance(record.value, dict) \
                        and "m_Clip" in record.value:
                    with self.subTest(save=path.name, entry=entry):
                        self.assertIsNotNone(edit._weapon_of(item))

    def test_no_weapon_anywhere_in_a_real_save_holds_more_than_this_tool_says_it_can(self) -> None:
        # Everywhere means the inventory, containers and the ground. Items outside the inventory keep
        # their record under the game's other spelling, m_SearializedGear.
        def weapons_in(value: JsonValue) -> Iterator[Tuple[str, int]]:
            if isinstance(value, NestedJson):
                yield from weapons_in(value.value)
            elif isinstance(value, dict):
                prefab = value.get("m_PrefabName")
                gear = value.get("m_SerializedGear") or value.get("m_SearializedGear")
                if isinstance(prefab, str) and isinstance(gear, NestedJson):
                    weapon = edit._weapon_of({"m_PrefabName": prefab, "m_SerializedGear": gear})
                    if weapon is not None:
                        yield prefab, len(weapon.clip) + weapon.spent
                for inner in value.values():
                    yield from weapons_in(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from weapons_in(inner)

        folder = Path(os.environ["TLD_SAVE_DIR"])
        checked = 0
        for path in codec.find_saves(folder):
            for prefab, held in weapons_in(codec.decode_save(path.read_bytes()).slot):
                if prefab in edit.ROUNDS_HELD:
                    with self.subTest(save=path.name, prefab=prefab):
                        self.assertLessEqual(held, edit.ROUNDS_HELD[prefab])
                    checked += 1
        if not checked:
            self.skipTest("no real save holds a weapon that this tool has a number for")

    def test_every_real_inventory_can_be_repaired(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        repaired = 0
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_items(codec.decode_save(original))
                try:
                    result = edit.repair_inventory(original)
                except EditError as refused:
                    self.assertIn("Nothing to change", str(refused))
                    continue
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                decoded = codec.decode_save(result.data)
                changed = {entry for entry, _, _ in result.repaired}
                expected = [Item(one.entry, one.prefab, one.count,
                                 100.0 if one.entry in changed else one.condition) for one in before]
                self.assertEqual(edit.list_items(decoded), expected)
                # Whatever is still below 100% is a torch or a flare that has burned, or something that
                # set-condition refuses too.
                items = edit._inventory_items(decoded)
                left = 0
                for one in expected:
                    if one.condition != 100.0:
                        gear = edit._gear_of(items[one.entry])
                        if gear is None or not edit._has_burned(gear):
                            with self.assertRaises(EditError):
                                edit._editable_condition(items[one.entry], one.entry, one.prefab)
                        left += 1
                self.assertLessEqual(left, result.ruined + result.burned + result.unreadable)
                repaired += 1
        if not repaired:
            self.skipTest("no real save has an inventory item below 100%")

    def test_one_item_can_be_removed_from_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                decoded = codec.decode_save(original)
                before = edit.list_items(decoded)
                target = next((one for one in before
                               if edit._why_it_stays(decoded, one.entry, one.prefab) is None), None)
                self.assertIsNotNone(target, "no item in this inventory may be removed")
                result = edit.remove_item(original, target.prefab, entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                expected = [Item(one.entry - (one.entry > target.entry), one.prefab, one.count, one.condition)
                            for one in before if one.entry != target.entry]
                self.assertEqual(edit.list_items(codec.decode_save(result.data)), expected)

    def test_what_a_real_save_points_at_is_left_in_place(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        for path in codec.find_saves(folder):
            decoded = codec.decode_save(path.read_bytes())
            player = decoded.entries["global"].get("m_PlayerManagerSerialized")
            record = player.value if isinstance(player, NestedJson) else None
            fields = ("m_ItemInHandsInstanceID", "m_LastUnequippedItemInstanceID")
            held = {record[field].literal for field in fields
                    if isinstance(record, dict) and isinstance(record.get(field), Number)}
            for entry, one in enumerate(edit._inventory_items(decoded)):
                gear = one["m_SerializedGear"].value
                if gear["m_InstanceIDProxy"].literal in held or gear.get("m_WaterSupplySerialized") is not None:
                    with self.subTest(save=path.name, entry=entry):
                        self.assertIsNotNone(edit._why_it_stays(decoded, entry, one["m_PrefabName"]))

    def test_one_item_can_be_copied_in_every_real_save(self) -> None:
        """In memory only. No file is written."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                decoded = codec.decode_save(original)
                items = edit._inventory_items(decoded)
                before = edit.list_items(decoded)
                target = next((one for one in before if edit._why_no_copy(items[one.entry]) is None), None)
                self.assertIsNotNone(target, "no item in this inventory may be copied")
                result = edit.copy_item(original, target.prefab, entry=target.entry)
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                self.assertEqual(result.entry, len(items))
                self.assertEqual(edit.list_items(codec.decode_save(result.data)),
                                 before + [Item(len(items), target.prefab, target.count, target.condition)])
                number = int(result.new_number)
                self.assertTrue(number < 0 and number % 2 == 0, number)

    def test_the_players_values_can_be_read_in_every_real_save(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        held = 0
        for path in saves:
            values = edit.read_player(codec.decode_save(path.read_bytes()))
            self.assertEqual([value.name for value in values], list(edit.PLAYER_VALUES))
            for value in values:
                with self.subTest(save=path.name, value=value.name):
                    self.assertIsNotNone(value.shown)
                    highest = 2500 if value.name == "calories" else 105
                    self.assertTrue(-5 <= value.shown <= highest, value.shown)
                    # A number the save holds is in the form this tool writes.
                    if value.stored is not None and (Decimal(value.stored) == 0
                                                     or Decimal(value.stored) >= Decimal("0.0001")):
                        self.assertEqual(edit.float32_literal(float(value.stored)), value.stored)
                        held += 1
        self.assertGreaterEqual(held, 3 * len(saves))

    def test_a_value_a_real_save_holds_is_the_first_field_of_its_record(self) -> None:
        # That is where this tool puts a number the game left out.
        folder = Path(os.environ["TLD_SAVE_DIR"])
        held = 0
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for name, where in edit.PLAYER_VALUES.items():
                record = edit._player_record(save, name)
                self.assertIsNotNone(record)
                if where.field in record:
                    with self.subTest(save=path.name, value=name):
                        self.assertEqual(list(record)[0], where.field)
                    held += 1
        self.assertTrue(held, "no real save holds one of the player's values")

    def test_the_players_values_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written. All five values of a save are changed in one edit."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        best = {"condition": (100.0, 99.0), "calories": (2500.0, 2400.0), "hydration": (100.0, 90.0),
                "rest": (100.0, 90.0), "warmth": (100.0, 90.0)}
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.read_player(codec.decode_save(original))
                # A number the game left out is 0, which is the best value of a bar. It gets the other one.
                wanted = {value.name: best[value.name][value.shown == Decimal(repr(best[value.name][0]))]
                          for value in before}
                self.assertEqual(len(wanted), 5)
                result = edit.set_player_values(original, wanted)
                self.assertEqual({name for name, _, _ in result.changes}, set(wanted))
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.read_player(codec.decode_save(result.data))
                for old, new in zip(before, after):
                    if old.name in wanted:
                        self.assertEqual(new.shown, Decimal(repr(wanted[old.name])), old.name)
                        self.assertEqual(new.stored, edit.stored_player_value(old.name, wanted[old.name]))
                    else:
                        self.assertEqual(new, old)

    def test_the_skills_can_be_read_in_every_real_save(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            skills = edit.list_skills(codec.decode_save(path.read_bytes()))
            self.assertEqual([(skill.name, skill.record) for skill in skills], list(edit.SKILLS.items()))
            for skill in skills:
                with self.subTest(save=path.name, skill=skill.name):
                    self.assertIsNotNone(skill.points)
                    self.assertTrue(0 <= skill.points <= edit.MAX_SKILL_POINTS, skill.points)
                    # Points the save holds are in the form this tool writes.
                    if skill.stored is not None:
                        self.assertEqual(skill.stored, str(skill.points))

    def test_points_a_real_save_holds_are_the_first_field_of_their_record(self) -> None:
        # That is where this tool puts points the game left out.
        folder = Path(os.environ["TLD_SAVE_DIR"])
        held = 0
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for name in edit.SKILLS:
                record = edit._skill_record(save, name)
                self.assertIsNotNone(record)
                if "m_Points" in record:
                    with self.subTest(save=path.name, skill=name):
                        self.assertEqual(list(record)[0], "m_Points")
                    held += 1
        self.assertTrue(held, "no real save holds the points of a skill")

    def test_the_points_of_a_skill_can_be_changed_in_every_real_save(self) -> None:
        """In memory only. No file is written. Per save: one skill with points, and one with points left out."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            original = path.read_bytes()
            before = edit.list_skills(codec.decode_save(original))
            held = next((skill for skill in before if skill.stored is not None), None)
            left_out = next((skill for skill in before if skill.stored is None), None)
            for target in (held, left_out):
                if target is None:
                    continue
                with self.subTest(save=path.name, skill=target.name, stored=target.stored):
                    points = target.points + 5
                    result = edit.set_skill_points(original, target.name, points)
                    self.assertEqual((result.skill, result.old_points, result.new_points),
                                     (target.name, target.stored, str(points)))
                    self.assertEqual(result.unchanged_entries, result.entries - 1)
                    self.assertTrue(codec.verify_save(result.data).identical)
                    after = edit.list_skills(codec.decode_save(result.data))
                    self.assertEqual(after, [Skill(skill.name, skill.record, str(points), points)
                                             if skill is target else skill for skill in before])

    def test_the_afflictions_can_be_read_in_every_real_save(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            listed = edit.list_afflictions(codec.decode_save(path.read_bytes()))
            self.assertEqual([(kind.name, kind.record) for kind in listed],
                             [(name, kind.record) for name, kind in edit.AFFLICTIONS.items()])
            for kind in listed:
                with self.subTest(save=path.name, affliction=kind.name):
                    self.assertIsNotNone(kind.cases)

    def test_every_real_save_with_an_affliction_can_be_cured(self) -> None:
        """In memory only. No file is written. A save without a case must be refused."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.list_afflictions(codec.decode_save(original))
                held = {kind.name: kind.cases for kind in before if kind.cases}
                if not held:
                    with self.assertRaises(EditError) as refused:
                        edit.cure_afflictions(original)
                    self.assertIn("there is nothing to cure", str(refused.exception))
                    continue
                result = edit.cure_afflictions(original)
                self.assertEqual(dict(result.cured), held)
                self.assertEqual(result.changes, sum(len(edit.AFFLICTIONS[name].lists) or 1 for name in held))
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                after = edit.list_afflictions(codec.decode_save(result.data))
                self.assertEqual([(kind.name, kind.cases) for kind in after],
                                 [(kind.name, 0) for kind in before])

    def test_the_position_can_be_read_in_every_real_save(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                position = edit.read_position(codec.decode_save(path.read_bytes()))
                self.assertIsNotNone(position.scene)
                self.assertIsNotNone(position.stored)
                for number in position.stored:
                    self.assertLessEqual(abs(float(number)), edit.MAX_COORDINATE)
                    # A number the save holds is in the form this tool writes.
                    if abs(float(number)) >= 0.0001:
                        self.assertEqual(edit.stored_coordinate("x", float(number)), number)

    def test_the_character_can_be_moved_in_every_real_save(self) -> None:
        """In memory only. No file is written. A character that is held in place must be refused."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                original = path.read_bytes()
                before = edit.read_position(codec.decode_save(original))
                # x moves, y is given as it is and stays, z moves.
                wanted = {"x": float(before.stored[0]) + 1.5, "y": float(before.stored[1]),
                          "z": float(before.stored[2]) - 2.25}
                if before.held is not None:
                    with self.assertRaises(EditError) as refused:
                        edit.set_position(original, wanted)
                    self.assertIn(f"the character is {before.held} in this save", str(refused.exception))
                    continue
                result = edit.set_position(original, wanted)
                moved = (edit.stored_coordinate("x", wanted["x"]), before.stored[1],
                         edit.stored_coordinate("z", wanted["z"]))
                self.assertEqual(result.changes, (("x", before.stored[0], moved[0]),
                                                  ("z", before.stored[2], moved[2])))
                self.assertEqual(result.kept, (("y", before.stored[1]),))
                self.assertEqual(result.unchanged_entries, result.entries - 1)
                self.assertTrue(codec.verify_save(result.data).identical)
                self.assertEqual(edit.read_position(codec.decode_save(result.data)),
                                 Position(before.scene, moved, None))

    def test_every_stored_condition_is_in_the_form_this_tool_writes(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        for path in codec.find_saves(folder):
            save = codec.decode_save(path.read_bytes())
            for entry, item in enumerate(edit._inventory_items(save)):
                literal = item["m_SerializedGear"].value["m_NormalizedCondition"].literal
                if float(literal) >= 0.0001:
                    with self.subTest(save=path.name, entry=entry):
                        self.assertEqual(edit.float32_literal(float(literal)), literal)


if __name__ == "__main__":
    unittest.main()
