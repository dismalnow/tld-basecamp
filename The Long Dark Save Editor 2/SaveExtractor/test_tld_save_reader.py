r"""Tests for tld_save_reader.py: where it writes its CSV, its region labels, how it decompresses,
and the weight, liters and calories it reads.

Run from this folder:
    python -m unittest -v

The tests build their own small save in a temporary folder and give the reader a temporary
home folder, so nothing is written to your real one. One more test reads each real save and
writes its CSV to the temporary home folder. It is skipped unless TLD_SAVE_DIR names their
folder:
    $env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
    python -m unittest -v
"""

from __future__ import annotations

import base64
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

import tld_save_codec as codec
import tld_save_reader as reader
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number


def small_save(matches: str = "23") -> bytes:
    """A save with one stack of matches in the player's inventory."""
    gear: Dict[str, JsonValue] = {
        "m_NormalizedCondition": Number("1.0"),
        "m_StackableItemSerialized": NestedJson({"m_UnitsProxy": Number(matches)}),
    }
    inventory: Dict[str, JsonValue] = {
        "m_SerializedItems": [{"m_PrefabName": "GEAR_WoodMatches", "m_SerializedGear": NestedJson(gear)}]}
    slot: Dict[str, JsonValue] = {
        "m_InternalName": "sandbox15",
        "m_Dict": {
            "boot": {"m_SceneName": "LakeRegion"},
            "global": {"m_Inventory_Serialized": NestedJson(inventory)},
        },
    }
    return codec.encode_save(DecodedSave(slot))


def town_save() -> bytes:
    """A save with matches in the inventory, on the ground in Mountain Town, and in its post office."""
    def scene_with_matches(x: str, y: str, z: str) -> Dict[str, JsonValue]:
        gear: Dict[str, JsonValue] = {
            "m_Position": [Number(x), Number(y), Number(z)],
            "m_NormalizedCondition": Number("1.0"),
        }
        items: Dict[str, JsonValue] = {
            "m_SerializedItems": [{"m_PrefabName": "GEAR_WoodMatches", "m_SearializedGear": NestedJson(gear)}]}
        return {"m_GearManagerSerialized": NestedJson(items)}

    slot = codec.decode_save(small_save()).slot
    entries = slot["m_Dict"]
    assert isinstance(entries, dict)
    entries["MountainTownRegion"] = scene_with_matches("444", "10", "716")
    entries["PostOfficeA"] = scene_with_matches("1", "2", "3")
    return codec.encode_save(DecodedSave(slot))


# One of each kind of item that stores a weight, a liquid amount or calories:
# (prefab, the fields the save holds for it, unit_weight_kg, liters, calories).
# The game writes weights and volumes as whole numbers of billionths of a kilogram or a liter.
SUPPLIES: List[Tuple[str, Dict[str, JsonValue], Optional[float], Optional[float], Optional[float]]] = [
    ("GEAR_Stick", {"m_WeightKG": Number("150000000"),
                    "m_StackableItemSerialized": NestedJson({"m_UnitsProxy": Number("6")})}, 0.15, None, None),
    ("GEAR_MRE", {"m_WeightKG": Number("500000000"),
                  "m_FoodItemSerialized": NestedJson({"m_CaloriesRemainingProxy": Number("122.235107"),
                                                      "m_CaloriesTotal": Number("1750.0")})}, 0.5, None, 122.2),
    ("GEAR_RawCohoSalmon", {"m_WeightKG": Number("3442150528"),
                            "m_FoodItemSerialized": NestedJson(
                                {"m_CaloriesRemainingProxy": Number("1032.64514"),
                                 "m_CaloriesTotal": Number("1032.64514")})}, 3.442, None, 1032.6),
    ("GEAR_WaterSupplyPotable", {"m_WaterSupplySerialized": NestedJson(
        {"m_VolumeProxy": Number("2000000000")})}, None, 2.0, None),
    ("GEAR_WaterSupplyNotPotable", {"m_WaterSupplySerialized": NestedJson({})}, None, None, None),
    ("GEAR_KeroseneLampB", {"m_WeightKG": Number("1000000000"),
                            "m_KeroseneLampItemSerialized": NestedJson(
                                {"m_CurrentFuelLitersProxy": Number("396388192")})}, 1.0, 0.396, None),
    ("GEAR_JerrycanRusty", {"m_WeightKG": Number("100000000"),
                            "m_LiquidItemSerialized": NestedJson(
                                {"m_LiquidLitersProxy": Number("1147736448"),
                                 "m_LiquidQuality": "NonPotable"})}, 0.1, 1.148, None),
    # A bottle whose amount the game has not set yet: it writes a large negative number.
    ("GEAR_BottleHydrogenPeroxide", {"m_LiquidItemSerialized": NestedJson(
        {"m_LiquidLitersProxy": Number("-214748364800000"), "m_LiquidQuality": "NonPotable"})},
     None, None, None),
    ("GEAR_VisorNoteML3", {}, None, None, None),
    ("GEAR_Cloth", {"m_WeightKG": Number("100000000"), "m_LiquidItemSerialized": None,
                    "m_FoodItemSerialized": None}, 0.1, None, None),
    # As the C# editor in this repository leaves a save: exponent forms, and 0.0 where the game wrote nothing.
    ("GEAR_Prybar", {"m_WeightKG": Number("1E+09")}, 1.0, None, None),
    ("GEAR_HeavyBandage", {"m_WeightKG": Number("0.0")}, None, None, None),
    ("GEAR_KeroseneLamp_Spelunkers", {"m_WeightKG": Number("1E+09"),
                                      "m_KeroseneLampItemSerialized": NestedJson(
                                          {"m_CurrentFuelLitersProxy": Number("-2.14748365E+14")})},
     1.0, None, None),
]


def supplies_save() -> bytes:
    """A save with the SUPPLIES items in the player's inventory, and two of them in a scene as well."""
    def stored(prefab: str, fields: Dict[str, JsonValue], key: str) -> Dict[str, JsonValue]:
        return {"m_PrefabName": prefab, key: NestedJson(dict(fields))}

    carried: List[JsonValue] = [
        stored(prefab, fields, "m_SerializedGear") for prefab, fields, _, _, _ in SUPPLIES]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": carried}
    on_the_floor: Dict[str, JsonValue] = {
        "m_SerializedItems": [stored(SUPPLIES[1][0], SUPPLIES[1][1], "m_SearializedGear")]}
    locker: Dict[str, JsonValue] = {
        "m_PrefabName": "CONTAINER_Locker",
        "m_SearializedContainer": NestedJson({
            "m_SerializedItems": [stored(SUPPLIES[6][0], SUPPLIES[6][1], "m_SearializedGear")],
            "m_GearToInstantiateSerialized": ["GEAR_Stick"],
        }),
    }
    slot: Dict[str, JsonValue] = {
        "m_InternalName": "sandbox15",
        "m_Dict": {
            "boot": {"m_SceneName": "LakeRegion"},
            "global": {"m_Inventory_Serialized": NestedJson(inventory)},
            "LakeRegion": {
                "m_GearManagerSerialized": NestedJson(on_the_floor),
                "m_ContainerManagerSerialized": NestedJson({"m_SerializedContainers": [locker]}),
            },
        },
    }
    return codec.encode_save(DecodedSave(slot))


def rows_of(path: Path) -> List[List[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


class ReaderCase(unittest.TestCase):
    """A game save folder with one save, and an empty home folder."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.saves = self.root / "TheLongDark" / "Survival"
        self.saves.mkdir(parents=True)
        self.save = self.saves / "sandbox15"
        self.save.write_bytes(small_save())
        self.home = self.root / "home"
        self.home.mkdir()
        self.reports = self.home / "TLD-save-reports"          # not created here: making it is the tool's job
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_reader(self, *arguments: str) -> Tuple[object, str, str]:
        """Run the reader's command line. Returns its exit value, and what it printed."""
        out, err = io.StringIO(), io.StringIO()
        code: object = 0
        with mock.patch("sys.argv", ["tld_save_reader.py", *arguments]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                reader.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def files_under(self, folder: Path) -> List[str]:
        return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


class DefaultCsvPathTests(ReaderCase):
    def test_default_is_the_reports_folder_in_the_home_folder(self) -> None:
        self.assertEqual(reader.default_csv_path(self.save), self.home / "TLD-save-reports" / "sandbox15.csv")

    def test_the_folder_name(self) -> None:
        self.assertEqual(reader.DEFAULT_REPORT_FOLDER, "TLD-save-reports")

    def test_only_the_file_name_of_the_save_counts(self) -> None:
        elsewhere = self.root / "copies" / "deep" / "sandbox15"
        self.assertEqual(reader.default_csv_path(elsewhere), reader.default_csv_path(self.save))

    def test_a_backup_does_not_share_the_report_of_its_save(self) -> None:
        backup = self.root / "kept" / "sandbox15.backup-20261124-061443"
        self.assertEqual(reader.default_csv_path(backup).name, "sandbox15.backup-20261124-061443.csv")
        self.assertNotEqual(reader.default_csv_path(backup), reader.default_csv_path(self.save))


class CsvCommandTests(ReaderCase):
    def test_csv_goes_to_the_reports_folder(self) -> None:
        code, out, _ = self.run_reader(str(self.save), "--csv")
        self.assertEqual(code, 0)
        report = self.reports / "sandbox15.csv"
        self.assertTrue(report.is_file())
        rows = rows_of(report)
        self.assertEqual(rows[0], reader.HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][reader.HEADERS.index("prefab")], "GEAR_WoodMatches")
        self.assertEqual(rows[1][reader.HEADERS.index("count")], "23")
        self.assertIn(str(report), out)

    def test_csv_leaves_the_save_folder_alone(self) -> None:
        before = self.save.read_bytes()
        self.run_reader(str(self.save), "--csv")
        self.assertEqual(self.files_under(self.saves), ["sandbox15"])
        self.assertEqual(self.save.read_bytes(), before)

    def test_csv_works_when_the_reports_folder_is_already_there(self) -> None:
        self.reports.mkdir()
        (self.reports / "other.csv").write_text("kept", encoding="utf-8")
        code, _, _ = self.run_reader(str(self.save), "--csv")
        self.assertEqual(code, 0)
        self.assertEqual(self.files_under(self.reports), ["other.csv", "sandbox15.csv"])
        self.assertEqual((self.reports / "other.csv").read_text(encoding="utf-8"), "kept")

    def test_a_new_run_replaces_the_earlier_report(self) -> None:
        self.run_reader(str(self.save), "--csv")
        self.save.write_bytes(small_save(matches="40"))
        self.run_reader(str(self.save), "--csv")
        self.assertEqual(self.files_under(self.reports), ["sandbox15.csv"])
        self.assertEqual(rows_of(self.reports / "sandbox15.csv")[1][reader.HEADERS.index("count")], "40")

    def test_out_writes_to_the_named_file_only(self) -> None:
        target = self.root / "items.csv"
        code, out, _ = self.run_reader(str(self.save), "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(len(rows_of(target)), 2)
        self.assertIn(str(target), out)
        self.assertFalse(self.reports.exists())
        self.assertEqual(self.files_under(self.saves), ["sandbox15"])

    def test_out_wins_when_csv_is_given_too(self) -> None:
        target = self.root / "items.csv"
        code, _, _ = self.run_reader(str(self.save), "--csv", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        self.assertFalse(self.reports.exists())

    def test_out_in_a_missing_folder_stops_with_a_message(self) -> None:
        target = self.root / "no such folder" / "items.csv"
        code, _, _ = self.run_reader(str(self.save), "--out", str(target))
        self.assertIsInstance(code, str)
        self.assertIn("Could not write", str(code))
        self.assertIn(str(target), str(code))
        self.assertFalse(target.exists())

    def test_a_home_folder_that_cannot_be_found_stops_with_a_message(self) -> None:
        with mock.patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
            code, _, _ = self.run_reader(str(self.save), "--csv")
        self.assertIsInstance(code, str)
        self.assertIn("--out FILE", str(code))
        self.assertEqual(self.files_under(self.saves), ["sandbox15"])

    def test_a_reports_folder_that_cannot_be_made_stops_with_a_message(self) -> None:
        self.reports.write_text("a file sits where the folder should be", encoding="utf-8")
        code, _, _ = self.run_reader(str(self.save), "--csv")
        self.assertIsInstance(code, str)
        self.assertIn("default CSV folder", str(code))
        self.assertIn("--out FILE", str(code))
        self.assertEqual(self.reports.read_text(encoding="utf-8"), "a file sits where the folder should be")

    def test_summary_writes_no_file(self) -> None:
        code, out, _ = self.run_reader(str(self.save), "--summary")
        self.assertEqual(code, 0)
        self.assertIn("SAVE SUMMARY", out)
        self.assertFalse(self.reports.exists())
        self.assertEqual(self.files_under(self.saves), ["sandbox15"])

    def test_a_scene_filter_still_writes_to_the_reports_folder(self) -> None:
        code, _, _ = self.run_reader(str(self.save), "--scene", "player", "--csv")
        self.assertEqual(code, 0)
        self.assertEqual(len(rows_of(self.reports / "sandbox15.csv")), 2)
        code, _, _ = self.run_reader(str(self.save), "--scene", "nowhere", "--csv")
        self.assertEqual(code, 0)
        self.assertEqual(rows_of(self.reports / "sandbox15.csv"), [reader.HEADERS])

    def test_a_missing_save_writes_nothing(self) -> None:
        code, _, _ = self.run_reader(str(self.saves / "sandbox99"), "--csv")
        self.assertIsInstance(code, str)
        self.assertIn("File not found", str(code))
        self.assertFalse(self.reports.exists())

    def test_help_names_the_reports_folder(self) -> None:
        code, out, _ = self.run_reader("--help")
        self.assertEqual(code, 0)
        self.assertIn("The CSV goes to TLD-save-reports in your home folder", out)


# The game's own region names: RegionsWithMap_* in Properties\Resources.resx of the C# editor.
GAME_REGION_LABELS = {
    "CoastalRegion": "Coastal Highway",
    "LakeRegion": "Mystery Lake",
    "WhalingStationRegion": "Desolation Point",
    "RuralRegion": "Pleasant Valley",
    "CrashMountainRegion": "Timberwolf Mountain",
    "MarshRegion": "Forlorn Muskeg",
    "RavineTransitionZone": "Ravine",
    "HighwayTransitionZone": "Crumbling Highway",
    "MountainTownRegion": "Mountain Town",
    "TracksRegion": "Broken Railroad",
    "RiverValleyRegion": "Hushed River Valley",
    "CanneryRegion": "Bleak Inlet",
    "AshCanyonRegion": "Ash Canyon",
}
TOWN_INTERIORS = ("BankA", "ChurchB", "ConvenienceStoreA", "FarmHouseB", "GreyMothersHouseA", "MiltonHouse",
                  "PostOfficeA")


class RegionLabelTests(unittest.TestCase):
    def test_outdoor_zones_carry_the_games_labels(self) -> None:
        for zone, label in GAME_REGION_LABELS.items():
            with self.subTest(zone=zone):
                self.assertEqual(reader.region_for_scene(zone, [zone]), label)

    def test_the_region_around_milton_is_mountain_town(self) -> None:
        self.assertEqual(reader.region_for_scene("MountainTownRegion", ["MountainTownRegion"]), "Mountain Town")

    def test_interiors_of_the_town_are_in_mountain_town(self) -> None:
        for scene in TOWN_INTERIORS:
            with self.subTest(scene=scene):
                self.assertEqual(reader.region_for_scene(scene, ["LakeRegion", "MountainTownRegion"]),
                                 "Mountain Town")

    def test_no_region_is_labeled_milton(self) -> None:
        self.assertNotIn("Milton", set(reader.SCENE_TO_REGION.values()))

    def test_landmarks_still_name_the_town(self) -> None:
        names = [landmark[0] for landmark in reader.REGION_LANDMARKS["MountainTownRegion"]]
        self.assertIn("Milton (Lower Town)", names)


class RegionCsvTests(ReaderCase):
    def test_the_csv_names_the_region_mountain_town(self) -> None:
        self.save.write_bytes(town_save())
        code, _, _ = self.run_reader(str(self.save), "--csv")
        self.assertEqual(code, 0)
        rows = rows_of(self.reports / "sandbox15.csv")[1:]
        region, location = reader.HEADERS.index("region"), reader.HEADERS.index("location")
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row[region] for row in rows), ["", "Mountain Town", "Mountain Town"])
        self.assertIn("Near Milton (Lower Town)", [row[location] for row in rows])


class DecompressionTests(unittest.TestCase):
    """The reader decompresses with tld_save_codec.py and keeps no decompressor of its own."""

    PLAIN = b"plain text with no repeats: 0123456789"

    def test_the_decompressor_is_the_one_of_the_codec(self) -> None:
        self.assertIs(reader.lzf_decompress, codec.lzf_decompress)
        self.assertFalse(hasattr(reader, "_lzf_decompress"))

    def test_packed_data_comes_back_as_it_went_in(self) -> None:
        for plain in (self.PLAIN, b"abc" * 500, b""):
            with self.subTest(length=len(plain)):
                self.assertEqual(reader._decompress(codec.lzf_compress(plain)), plain)

    def test_one_stray_byte_in_front_is_skipped(self) -> None:
        # 0x20 asks for a copy of earlier output when there is none yet, so the first try fails at once.
        self.assertEqual(reader._decompress(b"\x20" + codec.lzf_compress(self.PLAIN)), self.PLAIN)

    def test_data_that_is_cut_off_is_refused(self) -> None:
        with self.assertRaises(ValueError) as stop:
            reader._decompress(codec.lzf_compress(self.PLAIN)[:-5])
        self.assertEqual(str(stop.exception), "LZF decompression failed")

    def test_a_stray_byte_after_the_end_is_refused(self) -> None:
        with self.assertRaises(ValueError) as stop:
            reader._decompress(codec.lzf_compress(self.PLAIN) + b"\x00")
        self.assertEqual(str(stop.exception), "LZF decompression failed")


class DamagedSaveTests(ReaderCase):
    def test_a_save_that_is_cut_off_is_refused(self) -> None:
        self.save.write_bytes(small_save()[:-7])
        code, out, _ = self.run_reader(str(self.save))
        self.assertTrue(str(code).startswith(f"Could not read {self.save}: "), code)
        self.assertEqual(out, "")

    def test_a_scene_that_cannot_be_decoded_is_named_and_the_rest_is_read(self) -> None:
        slot = json.loads(codec.lzf_decompress(town_save()))
        whole = base64.b64decode(slot["m_Dict"]["PostOfficeA"])
        slot["m_Dict"]["PostOfficeA"] = base64.b64encode(whole[:-4]).decode("ascii")
        self.save.write_bytes(codec.lzf_compress(json.dumps(slot).encode("utf-8")))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            contents = reader.read_save(self.save)
        self.assertEqual(contents.failed_scenes, ["PostOfficeA"])
        self.assertEqual(len(contents.records), 2)          # the inventory, and the ground in Mountain Town
        self.assertIn("could not decode scene 'PostOfficeA'", errors.getvalue())


class StoredAmountTests(ReaderCase):
    """unit_weight_kg, liters and calories, as the save stores them."""

    def setUp(self) -> None:
        super().setUp()
        self.save.write_bytes(supplies_save())

    def test_each_kind_of_item_gives_its_amounts(self) -> None:
        records = [record for record in reader.read_save(self.save).records
                   if record.location_type == "player_inventory"]
        found = [(record.prefab, record.unit_weight_kg, record.liters, record.calories) for record in records]
        self.assertEqual(found, [(prefab, weight, liters, calories)
                                 for prefab, _, weight, liters, calories in SUPPLIES])

    def test_the_weight_is_for_one_unit_of_a_stack(self) -> None:
        sticks = reader.read_save(self.save).records[0]
        self.assertEqual((sticks.prefab, sticks.count, sticks.unit_weight_kg), ("GEAR_Stick", 6, 0.15))

    def test_items_on_the_floor_and_in_containers_carry_them_too(self) -> None:
        records = {record.location_type: record for record in reader.read_save(self.save).records
                   if record.location_type in ("floor", "container")}
        self.assertEqual((records["floor"].prefab, records["floor"].unit_weight_kg, records["floor"].calories),
                         ("GEAR_MRE", 0.5, 122.2))
        self.assertEqual((records["container"].prefab, records["container"].unit_weight_kg,
                          records["container"].liters), ("GEAR_JerrycanRusty", 0.1, 1.148))

    def test_loot_nobody_has_searched_has_no_amounts(self) -> None:
        waiting = [record for record in reader.read_save(self.save, include_unsearched=True).records
                   if record.location_type == "container_unsearched"]
        self.assertEqual([(record.prefab, record.unit_weight_kg, record.liters, record.calories)
                          for record in waiting], [("GEAR_Stick", None, None, None)])

    def test_the_csv_ends_with_the_three_columns(self) -> None:
        code, _, _ = self.run_reader(str(self.save), "--csv")
        self.assertEqual(code, 0)
        rows = rows_of(self.reports / "sandbox15.csv")
        self.assertEqual(rows[0], reader.HEADERS)
        self.assertEqual(rows[0][-3:], ["unit_weight_kg", "liters", "calories"])
        count = reader.HEADERS.index("count")
        self.assertEqual((rows[1][count], rows[1][-3:]), ("6", ["0.15", "", ""]))               # the sticks
        self.assertEqual(rows[2][-3:], ["0.5", "", "122.2"])                                    # the MRE
        self.assertEqual(rows[4][-3:], ["", "2.0", ""])                                         # the water

    def test_the_columns_that_were_there_keep_their_places(self) -> None:
        self.assertEqual(reader.HEADERS[:14], [
            "region", "location", "location_type", "container", "category", "subcategory", "item",
            "count", "condition_pct", "x", "y", "z", "prefab", "source"])


def real_saves() -> Sequence[Path]:
    folder = os.environ.get("TLD_SAVE_DIR")
    return codec.find_saves(Path(folder)) if folder else []


Amounts = Tuple[str, Optional[float], Optional[float], Optional[float]]


def amounts_from_the_codec(save: Path) -> Iterator[Amounts]:
    """(prefab, unit_weight_kg, liters, calories) of every item of a save, from the codec's own decoding.

    It walks the inventory, the items on the ground and the items in containers by itself, so it
    checks both which fields the reader takes and how it reaches them.
    """
    def opened(value: JsonValue) -> JsonValue:
        return value.value if isinstance(value, NestedJson) else value

    def amount(holder: JsonValue, field: str) -> Optional[float]:
        record = opened(holder)
        value = record.get(field) if isinstance(record, dict) else None
        return float(value.literal) if isinstance(value, Number) else None

    def one(item: JsonValue) -> Amounts:
        assert isinstance(item, dict)
        gear = opened(item.get("m_SerializedGear") or item.get("m_SearializedGear"))
        assert isinstance(gear, dict)
        weight = amount(gear, "m_WeightKG")
        volumes = [amount(gear.get(holder), field) for holder, field in (
            ("m_WaterSupplySerialized", "m_VolumeProxy"), ("m_LiquidItemSerialized", "m_LiquidLitersProxy"),
            ("m_KeroseneLampItemSerialized", "m_CurrentFuelLitersProxy"))]
        volume = next((value for value in volumes if value is not None), None)
        left = amount(gear.get("m_FoodItemSerialized"), "m_CaloriesRemainingProxy")
        return (reader.normalize_prefab(str(item["m_PrefabName"])),
                round(weight / 1e9, 3) if weight is not None and weight > 0 else None,
                round(volume / 1e9, 3) if volume is not None and volume >= 0 else None,
                round(left, 1) if left is not None else None)

    entries = codec.decode_save(save.read_bytes()).entries
    for name, document in entries.items():
        if not isinstance(document, dict) or name == "boot":
            continue
        if name == "global":
            listed = opened(document.get("m_Inventory_Serialized"))
            for item in (listed.get("m_SerializedItems") or []) if isinstance(listed, dict) else []:
                yield one(item)
            continue
        on_the_ground = opened(document.get("m_GearManagerSerialized"))
        for item in (on_the_ground.get("m_SerializedItems") or []) if isinstance(on_the_ground, dict) else []:
            yield one(item)
        containers = opened(document.get("m_ContainerManagerSerialized"))
        listed = containers.get("m_SerializedContainers") if isinstance(containers, dict) else None
        for container in listed or []:
            inside = opened(container.get("m_SearializedContainer")) if isinstance(container, dict) else None
            for item in (inside.get("m_SerializedItems") or []) if isinstance(inside, dict) else []:
                yield one(item)


@unittest.skipUnless(real_saves(), "set TLD_SAVE_DIR to the folder that holds your saves")
class RealSaveTests(ReaderCase):
    def test_csv_of_every_real_save_goes_to_the_reports_folder(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        before = sorted(path.name for path in folder.iterdir())
        for save in real_saves():
            with self.subTest(save=save.name):
                expected = len(reader.read_save(save).records)
                code, _, _ = self.run_reader(str(save), "--csv")
                self.assertEqual(code, 0)
                rows = rows_of(self.reports / f"{save.name}.csv")
                self.assertEqual(rows[0], reader.HEADERS)
                self.assertEqual(len(rows) - 1, expected)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), before)

    def test_amounts_match_what_the_codec_decodes_from_every_real_save(self) -> None:
        for save in real_saves():
            with self.subTest(save=save.name):
                records = [record for record in reader.read_save(save).records
                           if record.location_type in ("player_inventory", "floor", "container")]
                read = Counter((record.prefab, record.unit_weight_kg, record.liters, record.calories)
                               for record in records)
                self.assertEqual(read, Counter(amounts_from_the_codec(save)))

    def test_amounts_of_every_real_save_are_in_a_sane_range(self) -> None:
        for save in real_saves():
            for record in reader.read_save(save).records:
                with self.subTest(save=save.name, prefab=record.prefab):
                    if record.unit_weight_kg is not None:
                        self.assertTrue(0 < record.unit_weight_kg <= 100, record.unit_weight_kg)
                    if record.liters is not None:
                        self.assertTrue(0 <= record.liters <= 100, record.liters)
                    if record.calories is not None:
                        self.assertTrue(0 <= record.calories <= 20000, record.calories)

    def test_no_real_save_reports_a_region_named_milton(self) -> None:
        for save in real_saves():
            with self.subTest(save=save.name):
                regions = {record.region for record in reader.read_save(save).records}
                self.assertNotIn("Milton", regions)


if __name__ == "__main__":
    unittest.main()
