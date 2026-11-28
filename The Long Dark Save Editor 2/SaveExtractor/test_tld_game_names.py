r"""Tests for tld_game_names.py, the reader of the item names in the game's own files.

Run from this folder:
    python -m unittest -v test_tld_game_names

Almost all of these tests need neither the game nor the UnityPy package. They build a small
game folder in a temporary place, with a catalog in the game's format, and a stand-in plays
UnityPy and hands out what the bundles hold.

The last class reads a real game folder. It runs only when the environment variable
TLD_GAME_DIR names that folder and UnityPy is installed. It only reads. In PowerShell:

    $env:TLD_GAME_DIR = "C:\Program Files (x86)\Steam\steamapps\common\TheLongDark"
    python -m unittest -v test_tld_game_names
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

import tld_game_names as names
import tld_taxonomy as taxonomy

try:
    import UnityPy
except ImportError:
    UnityPy = None

GAME_DIR = os.environ.get(names.ENV_GAME_DIR, "").strip()
RUNTIME = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}\\StandaloneWindows64\\"


# ---------------------------------------------------------------------------
# Builders: a catalog in the game's format, a game folder, and a stand-in for UnityPy
# ---------------------------------------------------------------------------

def catalog_text(assets: Dict[str, List[str]]) -> str:
    """A catalog as the game writes it. Each asset comes with the bundles it needs, its holder first."""
    bundles = sorted({bundle for needed in assets.values() for bundle in needed})
    ids = [RUNTIME + bundle for bundle in bundles] + list(assets)
    entries: List[Tuple[int, ...]] = [(index, 0, -1, 0, -1, 0, 0) for index in range(len(bundles))]
    buckets: List[List[int]] = []
    for number, needed in enumerate(assets.values()):
        buckets.append([bundles.index(bundle) for bundle in needed])
        entries.append((len(bundles) + number, 1, len(buckets) - 1, 0, -1, 0, 0))
    bucket_data = struct.pack("<i", len(buckets)) + b"".join(
        struct.pack(f"<ii{len(bucket)}i", 0, len(bucket), *bucket) for bucket in buckets)
    entry_data = struct.pack("<i", len(entries)) + b"".join(struct.pack("<7i", *entry) for entry in entries)
    return json.dumps({"m_InternalIds": ids,
                       "m_BucketDataString": base64.b64encode(bucket_data).decode("ascii"),
                       "m_EntryDataString": base64.b64encode(entry_data).decode("ascii")})


class FakeFile:
    """A serialized file inside a bundle: its name, and the other files it points at."""

    def __init__(self, name: str, externals: Sequence[str] = ()) -> None:
        self.name = name
        self.externals = [types.SimpleNamespace(name=external) for external in externals]


class FakeObject:
    def __init__(self, kind: str, path_id: int, assets_file: FakeFile, name: str = "",
                 tree: Optional[Dict[str, Any]] = None, broken: bool = False) -> None:
        self.type = types.SimpleNamespace(name=kind)
        self.path_id = path_id
        self.assets_file = assets_file
        self._name, self._tree, self._broken = name, tree, broken

    def peek_name(self) -> str:
        if self._broken and self.type.name == "GameObject":
            raise ValueError("this object cannot be read")
        return self._name

    def read_typetree(self) -> Optional[Dict[str, Any]]:
        if self._broken:
            raise ValueError("this object cannot be read")
        return self._tree


class FakeUnityPy:
    """Plays the UnityPy module: load() hands out the objects of a bundle by the bundle's file name."""

    def __init__(self, bundles: Dict[str, List[FakeObject]]) -> None:
        self.bundles = bundles
        self.loaded: List[str] = []

    def load(self, path: str) -> types.SimpleNamespace:
        name = Path(path).name
        self.loaded.append(name)
        if name not in self.bundles:
            raise OSError("not a Unity bundle")
        return types.SimpleNamespace(objects=self.bundles[name])


def game_object(assets_file: FakeFile, path_id: int, name: str) -> FakeObject:
    return FakeObject("GameObject", path_id, assets_file, name=name)


def gear_item(assets_file: FakeFile, path_id: int, owner: int, record_file: int, record_id: int) -> FakeObject:
    """The GearItem component of the prefab whose game object has the id owner."""
    tree = {"m_GameObject": {"m_FileID": 0, "m_PathID": owner}, "m_Name": "",
            "m_GearItemData": {"m_FileID": record_file, "m_PathID": record_id}}
    return FakeObject("MonoBehaviour", path_id, assets_file, tree=tree)


def record(assets_file: FakeFile, path_id: int, name: str, key: str, condition: int = -1,
           units: int = 100000000) -> FakeObject:
    tree = {"m_Name": name, "m_LocalizedName": {"m_LocalizationID": key}, "m_ConditionType": condition,
            "m_BaseWeight": {"m_Units": units}}
    return FakeObject("MonoBehaviour", path_id, assets_file, name=name, tree=tree)


def string_table(strings: Dict[str, str]) -> List[FakeObject]:
    tree = {"m_Name": names.STRING_TABLE,
            "m_Entries": [{"m_Key": key, "m_Languages": [text, "second language"]}
                          for key, text in strings.items()]}
    return [FakeObject("MonoBehaviour", 1, FakeFile("CAB-strings"), name=names.STRING_TABLE, tree=tree)]


CONDITION_TREE = {"m_Name": names.CONDITION_TABLE, "m_Entries": [
    {"m_NameForConditionRangeList": [{"m_Suffix": "Fresh", "m_MinConditionForName": 90.0},
                                     {"m_Suffix": "Ruined", "m_MinConditionForName": 0.0}]},
    {"m_NameForConditionRangeList": [{"m_Suffix": "Perfect", "m_MinConditionForName": 100.0},
                                     {"m_Suffix": "Ruined", "m_MinConditionForName": 0.0}]}]}

BASE_ASSETS = {
    "Assets/Prefabs/Gear/Tools/GEAR_Hatchet.prefab": ["prefabs.bundle", "records.bundle", "art.bundle"],
    "Assets/Prefabs/Gear/Tools/GearItemData_Hatchet.asset": ["records.bundle"],
    "Assets/ScriptableObjects/Gear/SCRIPT_ConditionTable.asset": ["prefabs.bundle", "records.bundle"],
    "Assets/Prefabs/Scenery/OBJ_Rock.prefab": ["scenery.bundle"],
    "Assets/ArtAssets/GEAR_Hatchet_Dif.png": ["art.bundle"],
}
DLC_ASSETS = {
    "Assets/Prefabs/Gear/Ammo/GEAR_FilmBoxBW.prefab": ["dlcprefabs.bundle", "dlcrecords.bundle"],
    "Assets/Prefabs/Gear/Ammo/GearItemData_FilmBoxBW.asset": ["dlcrecords.bundle"],
}
STRINGS = {"GAMEPLAY_Hatchet": "Hatchet", "GAMEPLAY_FilmboxBW": "FastFilm-500BW"}


def small_game_bundles() -> Dict[str, List[FakeObject]]:
    """What the bundles of the small game hold: two prefabs, their records, the two tables."""
    prefabs = FakeFile("CAB-prefabs", ["archive:/CAB-records/CAB-records"])
    records = FakeFile("CAB-records")
    dlc_prefabs = FakeFile("CAB-dlcprefabs", ["archive:/CAB-unused/CAB-unused",
                                              "archive:/CAB-dlcrecords/CAB-dlcrecords"])
    dlc_records = FakeFile("CAB-dlcrecords")
    return {
        "prefabs.bundle": [
            game_object(prefabs, 10, "GEAR_Hatchet"),
            gear_item(prefabs, 11, owner=10, record_file=1, record_id=500),
            FakeObject("MonoBehaviour", 12, prefabs, tree={"m_Name": "", "m_SomethingElse": 1}),
            FakeObject("MonoBehaviour", 13, prefabs, broken=True),
            FakeObject("MonoBehaviour", 14, prefabs, name=names.CONDITION_TABLE, tree=CONDITION_TREE),
            FakeObject("Texture2D", 15, prefabs, name="GEAR_Hatchet_Dif"),
        ],
        "records.bundle": [record(records, 500, "GearItemData_Hatchet", "GAMEPLAY_Hatchet", units=500000000)],
        "dlcprefabs.bundle": [
            game_object(dlc_prefabs, 20, "GEAR_FilmBoxBW"),
            gear_item(dlc_prefabs, 21, owner=20, record_file=2, record_id=700),
        ],
        "dlcrecords.bundle": [record(dlc_records, 700, "GearItemData_FilmBoxBW", "GAMEPLAY_FilmBoxBW")],
        "localization": string_table(STRINGS),
    }


def make_game(folder: Path, base: Dict[str, List[str]], dlc: Optional[Dict[str, List[str]]] = None,
              version: Optional[str] = "2.60 189457 S\r\n", strings: bool = True) -> Path:
    """A game folder with catalogs, and empty files where the bundles go. The stand-in says what they hold."""
    game = folder / "TheLongDark"
    bundles = game / names.BASE_BUNDLES
    bundles.mkdir(parents=True)
    (game / names.BASE_CATALOG).write_text(catalog_text(base), encoding="utf-8")
    for bundle in {bundle for needed in base.values() for bundle in needed}:
        (bundles / bundle).write_bytes(b"")
    if strings:
        (game / names.STRINGS_BUNDLE).parent.mkdir(parents=True)
        (game / names.STRINGS_BUNDLE).write_bytes(b"")
    if version is not None:
        (game / names.VERSION_FILE).write_bytes(version.encode("utf-8"))
    if dlc is not None:
        home = game / names.DLC_FOLDER / "TFTFT"
        home.mkdir(parents=True)
        (home / "DLC01.json").write_text(catalog_text(dlc), encoding="utf-8")
        (home / "settings.json").write_text('{"not": "a catalog"}', encoding="utf-8")
        (home / "broken.json").write_text("{", encoding="utf-8")
        for bundle in {bundle for needed in dlc.values() for bundle in needed}:
            (home / bundle).write_bytes(b"")
    return game


@contextlib.contextmanager
def small_game(base: Optional[Dict[str, List[str]]] = None, dlc: Optional[Dict[str, List[str]]] = None,
               bundles: Optional[Dict[str, List[FakeObject]]] = None,
               **options: Any) -> Iterator[Tuple[Path, FakeUnityPy]]:
    """The small game in a temporary folder, with the stand-in in place of UnityPy."""
    fake = FakeUnityPy(small_game_bundles() if bundles is None else bundles)
    with tempfile.TemporaryDirectory() as folder:
        game = make_game(Path(folder), BASE_ASSETS if base is None else base,
                         DLC_ASSETS if dlc is None else dlc, **options)
        with mock.patch.object(names, "load_unitypy", return_value=fake):
            yield game, fake


def run(arguments: List[str]) -> Tuple[int, str, str]:
    """Exit value, standard output and standard error of one run of the tool."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = names.main(arguments)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

class CatalogTests(unittest.TestCase):
    def test_an_asset_is_held_by_the_first_bundle_it_names(self) -> None:
        catalog = names.parse_catalog(catalog_text(BASE_ASSETS), "catalog.json")
        holders = names.asset_holders(catalog, "catalog.json")
        self.assertEqual(holders, {
            "Assets/Prefabs/Gear/Tools/GEAR_Hatchet.prefab": "prefabs.bundle",
            "Assets/Prefabs/Gear/Tools/GearItemData_Hatchet.asset": "records.bundle",
            "Assets/ScriptableObjects/Gear/SCRIPT_ConditionTable.asset": "prefabs.bundle",
            "Assets/Prefabs/Scenery/OBJ_Rock.prefab": "scenery.bundle",
            "Assets/ArtAssets/GEAR_Hatchet_Dif.png": "art.bundle",
        })

    def test_a_bundle_is_not_listed_as_an_asset(self) -> None:
        holders = names.asset_holders(names.parse_catalog(catalog_text(BASE_ASSETS), "c"), "c")
        self.assertFalse([asset for asset in holders if asset.endswith(".bundle")])

    def test_the_file_name_of_a_bundle_with_either_slash(self) -> None:
        self.assertEqual(names.bundle_file(RUNTIME + "abc.bundle"), "abc.bundle")
        self.assertEqual(names.bundle_file("tld_dlc/TFTFT/abc.bundle"), "abc.bundle")
        self.assertEqual(names.bundle_file("abc.bundle"), "abc.bundle")

    def test_text_that_is_not_a_catalog_is_refused_with_the_name_of_the_file(self) -> None:
        good = json.loads(catalog_text(BASE_ASSETS))
        cases = {
            "not JSON": "{",
            "a list": "[1, 2]",
            "no ids": json.dumps({key: value for key, value in good.items() if key != "m_InternalIds"}),
            "no buckets": json.dumps({key: value for key, value in good.items()
                                      if key != "m_BucketDataString"}),
            "ids that are numbers": json.dumps(dict(good, m_InternalIds=[1, 2])),
            "not base64": json.dumps(dict(good, m_EntryDataString="***")),
            "a table that is not text": json.dumps(dict(good, m_EntryDataString=5)),
        }
        for what, text in cases.items():
            with self.subTest(what=what):
                with self.assertRaises(names.GameFilesError) as caught:
                    names.parse_catalog(text, "DLC09.json")
                self.assertIn("DLC09.json", str(caught.exception))

    def test_a_catalog_that_ends_too_early_is_refused(self) -> None:
        good = json.loads(catalog_text(BASE_ASSETS))
        for field in ("m_BucketDataString", "m_EntryDataString"):
            with self.subTest(field=field):
                cut = base64.b64encode(base64.b64decode(good[field])[:-3]).decode("ascii")
                with self.assertRaises(names.GameFilesError) as caught:
                    names.parse_catalog(json.dumps(dict(good, **{field: cut})), "catalog.json")
                self.assertIn("ends inside", str(caught.exception))

    def test_a_bucket_with_a_length_below_zero_is_refused(self) -> None:
        good = json.loads(catalog_text(BASE_ASSETS))
        bad = base64.b64encode(struct.pack("<iii", 1, 0, -4)).decode("ascii")
        with self.assertRaises(names.GameFilesError):
            names.parse_catalog(json.dumps(dict(good, m_BucketDataString=bad)), "catalog.json")

    def test_an_entry_that_points_outside_the_lists_is_refused(self) -> None:
        good = json.loads(catalog_text(BASE_ASSETS))
        for what, entry in (("id", (999, 1, 0, 0, -1, 0, 0)), ("bucket", (0, 1, 999, 0, -1, 0, 0)),
                            ("id below zero", (-1, 1, 0, 0, -1, 0, 0))):
            with self.subTest(what=what):
                data = struct.pack("<i", 1) + struct.pack("<7i", *entry)
                text = json.dumps(dict(good, m_InternalIds=["Assets/a.prefab"],
                                       m_EntryDataString=base64.b64encode(data).decode("ascii")))
                with self.assertRaises(names.GameFilesError) as caught:
                    names.asset_holders(names.parse_catalog(text, "catalog.json"), "catalog.json")
                self.assertIn("outside", str(caught.exception))

    def test_a_bucket_that_names_an_entry_below_zero_is_refused(self) -> None:
        good = json.loads(catalog_text({"Assets/GEAR_A.prefab": ["a.bundle"]}))
        bucket = base64.b64encode(struct.pack("<iiii", 1, 0, 1, -1)).decode("ascii")
        text = json.dumps(dict(good, m_BucketDataString=bucket))
        with self.assertRaises(names.GameFilesError) as caught:
            names.asset_holders(names.parse_catalog(text, "catalog.json"), "catalog.json")
        self.assertIn("outside", str(caught.exception))


class WantedBundlesTests(unittest.TestCase):
    def test_only_the_bundles_of_gear_prefabs_records_and_the_condition_table_are_wanted(self) -> None:
        holders = names.asset_holders(names.parse_catalog(catalog_text(BASE_ASSETS), "c"), "c")
        self.assertEqual(names.wanted_bundles(holders), {"prefabs": ["prefabs.bundle"],
                                                         "records": ["records.bundle"],
                                                         "conditions": ["prefabs.bundle"]})

    def test_a_prefab_counts_whatever_the_case_of_gear(self) -> None:
        wanted = names.wanted_bundles({"Assets/x/Gear_Travois_Dummy.prefab": "a.bundle",
                                       "Assets/x/GEAR_Stick.PREFAB": "b.bundle",
                                       "Assets\\x\\gear_stone.prefab": "c.bundle"})
        self.assertEqual(wanted["prefabs"], ["a.bundle", "b.bundle", "c.bundle"])

    def test_what_only_looks_like_one_of_the_three_is_left_out(self) -> None:
        wanted = names.wanted_bundles({"Assets/x/GEAR_Stick.fbx": "a.bundle",
                                       "Assets/x/OBJ_GEAR_Stick.prefab": "b.bundle",
                                       "Assets/x/GearItemData_Stick.prefab": "c.bundle",
                                       "Assets/x/MyGearItemData_Stick.asset": "d.bundle",
                                       "Assets/x/SCRIPT_ConditionTable_Old.asset": "e.bundle"})
        self.assertEqual(wanted, {"prefabs": [], "records": [], "conditions": []})

    def test_each_bundle_is_named_once_and_in_order(self) -> None:
        wanted = names.wanted_bundles({"Assets/GEAR_B.prefab": "z.bundle", "Assets/GEAR_A.prefab": "a.bundle",
                                       "Assets/GEAR_C.prefab": "z.bundle"})
        self.assertEqual(wanted["prefabs"], ["a.bundle", "z.bundle"])


# ---------------------------------------------------------------------------
# Joining prefab, record and text
# ---------------------------------------------------------------------------

def a_record(data: str, key: str, condition: int = -1, units: int = 0) -> Dict[str, Any]:
    return {"data": data, "name_key": key, "condition_type": condition, "weight_units": units}


class JoinTests(unittest.TestCase):
    RECORDS = {("cab-a", 1): a_record("GearItemData_Stick", "GAMEPLAY_Stick", units=150000000),
               ("cab-a", 2): a_record("GearItemData_BeefJerky", "GAMEPLAY_BeefJerky", condition=1),
               ("cab-a", 3): a_record("GearItemData_CannedCorn", "GAMEPLAY_CannedCorn", condition=2),
               ("cab-a", 4): a_record("GearItemData_Nameless", ""),
               ("cab-a", 5): a_record("GearItemData_BunkerClue", "GAMEPLAY_BunkerClue")}
    STRINGS = {"GAMEPLAY_Stick": "Stick", "GAMEPLAY_beefjerky": "Beef  Jerky ", "GAMEPLAY_CannedCorn": "Corn"}

    def join(self, components: List[Tuple[str, Tuple[str, int]]],
             strings: Optional[Dict[str, str]] = None) -> names.NameTable:
        return names.join(components, self.RECORDS, self.STRINGS if strings is None else strings)

    def test_a_prefab_gets_the_text_of_its_records_key(self) -> None:
        table = self.join([("GEAR_Stick", ("cab-a", 1))])
        self.assertEqual(table.items, {"GEAR_Stick": {"name": "Stick", "name_key": "GAMEPLAY_Stick",
                                                      "data": "GearItemData_Stick", "condition_type": -1,
                                                      "weight_units": 150000000}})
        self.assertEqual(table.names(), {"GEAR_Stick": "Stick"})

    def test_the_key_is_matched_without_regard_to_case_and_the_text_is_cleaned(self) -> None:
        table = self.join([("GEAR_BeefJerky", ("cab-a", 2))])
        self.assertEqual(table.names(), {"GEAR_BeefJerky": "Beef Jerky"})
        self.assertEqual(table.items["GEAR_BeefJerky"]["name_key"], "GAMEPLAY_BeefJerky")

    def test_a_copy_number_and_a_space_at_the_end_of_a_prefab_name_are_dropped(self) -> None:
        table = self.join([("GEAR_Stick (1)", ("cab-a", 1)), ("GEAR_BeefJerky ", ("cab-a", 2))])
        self.assertEqual(sorted(table.items), ["GEAR_BeefJerky", "GEAR_Stick"])

    def test_a_key_without_text_and_a_record_without_a_key_give_no_name(self) -> None:
        table = self.join([("GEAR_BunkerClue1a", ("cab-a", 5)), ("GEAR_Nameless", ("cab-a", 4))])
        self.assertEqual(table.names(), {})
        self.assertEqual(table.no_text, {"GEAR_BunkerClue1a": "GAMEPLAY_BunkerClue", "GEAR_Nameless": ""})
        self.assertEqual(sorted(table.items), ["GEAR_BunkerClue1a", "GEAR_Nameless"])

    def test_table_keys_that_differ_in_case_only_and_agree_give_the_name(self) -> None:
        strings = {"GAMEPLAY_Stick": "Stick", "GAMEPLAY_STICK": "Stick "}
        self.assertEqual(self.join([("GEAR_Stick", ("cab-a", 1))], strings).names(), {"GEAR_Stick": "Stick"})

    def test_table_keys_that_differ_in_case_only_and_disagree_give_no_name(self) -> None:
        strings = {"GAMEPLAY_Stick": "Stick", "GAMEPLAY_STICK": "STICK"}
        table = self.join([("GEAR_Stick", ("cab-a", 1))], strings)
        self.assertEqual(table.names(), {})
        self.assertEqual(table.ambiguous, {"GEAR_Stick": ["GAMEPLAY_STICK", "GAMEPLAY_Stick"]})
        self.assertEqual(table.no_text, {})

    def test_a_component_whose_record_is_not_found_is_listed(self) -> None:
        table = self.join([("GEAR_Lost", ("cab-zz", 9)), ("GEAR_Stick", ("cab-a", 1))])
        self.assertEqual(table.no_record, ["GEAR_Lost"])
        self.assertEqual(sorted(table.items), ["GEAR_Stick"])

    def test_a_prefab_found_in_one_copy_is_not_listed_as_lost_for_another(self) -> None:
        table = self.join([("GEAR_Stick", ("cab-zz", 9)), ("GEAR_Stick", ("cab-a", 1))])
        self.assertEqual(table.no_record, [])
        self.assertEqual(table.names(), {"GEAR_Stick": "Stick"})

    def test_the_first_copy_of_a_prefab_counts_and_a_copy_that_disagrees_is_listed(self) -> None:
        table = self.join([("GEAR_CannedCorn", ("cab-a", 3)), ("GEAR_CannedCorn", ("cab-a", 3)),
                           ("GEAR_CannedCorn", ("cab-a", 1)), ("GEAR_CannedCorn", ("cab-a", 1))])
        self.assertEqual(table.names(), {"GEAR_CannedCorn": "Corn"})
        self.assertEqual(table.two_records,
                         {"GEAR_CannedCorn": ["GearItemData_CannedCorn", "GearItemData_Stick"]})

    def test_two_prefabs_may_share_one_record(self) -> None:
        table = self.join([("GEAR_CannedCorn", ("cab-a", 3)), ("GEAR_CannedBeansB", ("cab-a", 3))])
        self.assertEqual(table.names(), {"GEAR_CannedCorn": "Corn", "GEAR_CannedBeansB": "Corn"})
        self.assertEqual(table.two_records, {})


class StringTableTests(unittest.TestCase):
    def test_the_first_language_of_each_entry_is_taken(self) -> None:
        tree = {"m_Entries": [{"m_Key": "A", "m_Languages": ["one", "eins"]},
                              {"m_Key": "B", "m_Languages": [""]}]}
        self.assertEqual(names.strings_of(tree), {"A": "one", "B": ""})

    def test_entries_that_are_not_key_and_text_are_passed_over(self) -> None:
        tree = {"m_Entries": [{"m_Key": "A"}, {"m_Languages": ["x"]}, {"m_Key": 5, "m_Languages": ["x"]},
                              {"m_Key": "B", "m_Languages": []}, {"m_Key": "C", "m_Languages": [7]},
                              "text", None,
                              {"m_Key": "D", "m_Languages": ["kept"]}]}
        self.assertEqual(names.strings_of(tree), {"D": "kept"})

    def test_a_table_without_entries_is_empty(self) -> None:
        self.assertEqual(names.strings_of({}), {})
        self.assertEqual(names.strings_of({"m_Entries": None}), {})


class CompareTests(unittest.TestCase):
    def test_the_three_kinds_of_difference(self) -> None:
        game = {"GEAR_A": "Same", "GEAR_B": "New text", "GEAR_C": "Only in the game"}
        table = {"GEAR_A": "Same", "GEAR_B": "Old text", "GEAR_D": "Only in the table"}
        self.assertEqual(names.compare(game, table), {
            "new": ["GEAR_C: 'Only in the game'"],
            "different": ["GEAR_B: 'Old text' -> 'New text'"],
            "gone": ["GEAR_D: 'Only in the table'"],
        })

    def test_equal_tables_give_no_lines(self) -> None:
        self.assertEqual(names.compare({"GEAR_A": "A"}, {"GEAR_A": "A"}),
                         {"new": [], "different": [], "gone": []})

    def test_a_character_outside_ascii_is_written_as_an_escape(self) -> None:
        lines = names.compare({"GEAR_A": "Spelunker\u2019s Lantern"}, {})
        self.assertEqual(lines["new"], ["GEAR_A: 'Spelunker\\u2019s Lantern'"])
        self.assertTrue(lines["new"][0].isascii())

    def test_names_of_prefabs_the_item_table_does_not_know_are_set_apart(self) -> None:
        known, unknown = names.known_prefabs({"GEAR_Hatchet": "Hatchet", "GEAR_NotInAnyTable": "Thing"})
        self.assertEqual(known, {"GEAR_Hatchet": "Hatchet"})
        self.assertEqual(unknown, ["GEAR_NotInAnyTable"])


# ---------------------------------------------------------------------------
# The game folder
# ---------------------------------------------------------------------------

class GameFolderTests(unittest.TestCase):
    def test_the_base_game_comes_first_and_then_each_dlc_catalog(self) -> None:
        with small_game() as (game, _fake):
            parts = names.find_parts(game)
        self.assertEqual([part.name for part in parts], ["base", "DLC01"])
        self.assertEqual(parts[0].bundles, game / names.BASE_BUNDLES)
        self.assertEqual(parts[1].bundles, game / names.DLC_FOLDER / "TFTFT")

    def test_json_files_under_tld_dlc_that_are_no_catalog_are_passed_over(self) -> None:
        with small_game() as (game, _fake):
            found = sorted(path.name for path in (game / names.DLC_FOLDER / "TFTFT").glob("*.json"))
            parts = names.find_parts(game)
        self.assertEqual(found, ["DLC01.json", "broken.json", "settings.json"])
        self.assertEqual([part.catalog.name for part in parts], ["catalog.json", "DLC01.json"])

    def test_a_game_without_dlc_has_one_part(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            game = make_game(Path(folder), BASE_ASSETS)
            self.assertEqual([part.name for part in names.find_parts(game)], ["base"])

    def test_what_is_not_a_game_folder_is_refused_with_advice(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "nothing-here"
            with self.assertRaises(names.GameFilesError) as caught:
                names.find_parts(missing)
            self.assertIn("is not a folder", str(caught.exception))
            with self.assertRaises(names.GameFilesError) as caught:
                names.find_parts(Path(folder))
            self.assertIn("does not look like the game's folder", str(caught.exception))
            self.assertIn("tld.exe", str(caught.exception))

    def test_a_game_folder_without_the_catalog_file_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "tld_Data").mkdir()
            with self.assertRaises(names.GameFilesError) as caught:
                names.find_parts(Path(folder))
            self.assertIn("catalog.json", str(caught.exception))
            self.assertIn("may have changed", str(caught.exception))

    def test_the_version_is_the_text_of_the_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            game = make_game(Path(folder), BASE_ASSETS)
            self.assertEqual(names.game_version(game), "2.60 189457 S")
        with tempfile.TemporaryDirectory() as folder:
            game = make_game(Path(folder), BASE_ASSETS, version=None)
            self.assertEqual(names.game_version(game), "")

    def test_the_folder_comes_from_the_option_first_and_then_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {names.ENV_GAME_DIR: "  from-environment  "}):
            self.assertEqual(names.game_dir_of(Path("from-option")), Path("from-option"))
            self.assertEqual(names.game_dir_of(None), Path("from-environment"))
        with mock.patch.dict(os.environ, {names.ENV_GAME_DIR: "   "}):
            with self.assertRaises(names.GameFilesError) as caught:
                names.game_dir_of(None)
        self.assertIn("--game-dir", str(caught.exception))
        self.assertIn(names.ENV_GAME_DIR, str(caught.exception))


class ReadGameTests(unittest.TestCase):
    def test_the_small_game_gives_both_names(self) -> None:
        with small_game() as (game, _fake):
            table, findings, parts = names.read_game(game, progress=False)
        self.assertEqual(parts, ["base", "DLC01"])
        self.assertEqual(table.names(), {"GEAR_Hatchet": "Hatchet", "GEAR_FilmBoxBW": "FastFilm-500BW"})
        self.assertEqual(table.items["GEAR_Hatchet"], {"name": "Hatchet", "name_key": "GAMEPLAY_Hatchet",
                                                       "data": "GearItemData_Hatchet", "condition_type": -1,
                                                       "weight_units": 500000000})
        self.assertEqual((table.no_record, table.no_text, table.ambiguous, table.two_records), ([], {}, {}, {}))
        self.assertEqual(findings.conditions, [[("Fresh", 90.0), ("Ruined", 0.0)],
                                               [("Perfect", 100.0), ("Ruined", 0.0)]])

    def test_only_the_bundles_that_hold_gear_are_opened_and_each_once(self) -> None:
        with small_game() as (game, fake):
            names.read_game(game, progress=False)
        self.assertEqual(fake.loaded, ["prefabs.bundle", "records.bundle", "dlcprefabs.bundle",
                                       "dlcrecords.bundle", "localization"])

    def test_an_object_that_cannot_be_read_is_counted_and_passed_over(self) -> None:
        with small_game() as (game, _fake):
            _table, findings, _parts = names.read_game(game, progress=False)
        self.assertEqual(findings.unreadable, 1)

    def test_a_game_object_that_cannot_be_read_is_counted_too(self) -> None:
        bundles = small_game_bundles()
        bundles["prefabs.bundle"].append(FakeObject("GameObject", 99, bundles["prefabs.bundle"][0].assets_file,
                                                    broken=True))
        with small_game(bundles=bundles) as (game, _fake):
            table, findings, _parts = names.read_game(game, progress=False)
        self.assertEqual(findings.unreadable, 2)
        self.assertEqual(sorted(table.names()), ["GEAR_FilmBoxBW", "GEAR_Hatchet"])

    def test_a_record_in_the_same_file_as_its_prefab_is_found(self) -> None:
        one = FakeFile("CAB-one")
        bundles = {"prefabs.bundle": [game_object(one, 10, "GEAR_Hatchet"),
                                      gear_item(one, 11, owner=10, record_file=0, record_id=500),
                                      record(one, 500, "GearItemData_Hatchet", "GAMEPLAY_Hatchet")],
                   "records.bundle": [], "localization": string_table(STRINGS)}
        with small_game(dlc={}, bundles=bundles) as (game, _fake):
            table, _findings, _parts = names.read_game(game, progress=False)
        self.assertEqual(table.names(), {"GEAR_Hatchet": "Hatchet"})

    def test_a_component_that_names_no_record_or_a_file_that_is_not_there_is_left_out(self) -> None:
        one = FakeFile("CAB-one", ["archive:/CAB-records/CAB-records"])
        records = FakeFile("CAB-records")
        bundles = {"prefabs.bundle": [game_object(one, 10, "GEAR_Hatchet"), game_object(one, 20, "GEAR_Stick"),
                                      game_object(one, 30, "GEAR_Stone"),
                                      gear_item(one, 11, owner=10, record_file=1, record_id=500),
                                      gear_item(one, 21, owner=20, record_file=0, record_id=0),
                                      gear_item(one, 31, owner=30, record_file=7, record_id=500),
                                      gear_item(one, 41, owner=404, record_file=1, record_id=500)],
                   "records.bundle": [record(records, 500, "GearItemData_Hatchet", "GAMEPLAY_Hatchet")],
                   "localization": string_table(STRINGS)}
        with small_game(dlc={}, bundles=bundles) as (game, _fake):
            table, _findings, _parts = names.read_game(game, progress=False)
        self.assertEqual(sorted(table.items), ["GEAR_Hatchet"])

    def test_progress_goes_to_standard_error_one_line_per_bundle(self) -> None:
        err = io.StringIO()
        with small_game() as (game, _fake), contextlib.redirect_stderr(err):
            names.read_game(game)
        self.assertEqual(err.getvalue().splitlines(), ["  reading base: prefabs.bundle",
                                                       "  reading base: records.bundle",
                                                       "  reading DLC01: dlcprefabs.bundle",
                                                       "  reading DLC01: dlcrecords.bundle"])

    def test_a_bundle_the_catalog_names_and_the_folder_lacks_stops_the_run(self) -> None:
        with small_game() as (game, _fake):
            (game / names.BASE_BUNDLES / "records.bundle").unlink()
            with self.assertRaises(names.GameFilesError) as caught:
                names.read_game(game, progress=False)
        self.assertIn("records.bundle", str(caught.exception))
        self.assertIn("fully installed", str(caught.exception))

    def test_a_dlc_bundle_may_sit_in_the_base_folder(self) -> None:
        with small_game() as (game, _fake):
            (game / names.DLC_FOLDER / "TFTFT" / "dlcrecords.bundle").rename(
                game / names.BASE_BUNDLES / "dlcrecords.bundle")
            table, _findings, _parts = names.read_game(game, progress=False)
        self.assertEqual(sorted(table.names()), ["GEAR_FilmBoxBW", "GEAR_Hatchet"])

    def test_a_file_that_is_no_bundle_stops_the_run_with_its_name(self) -> None:
        bundles = small_game_bundles()
        del bundles["records.bundle"]
        with small_game(bundles=bundles) as (game, _fake):
            with self.assertRaises(names.GameFilesError) as caught:
                names.read_game(game, progress=False)
        self.assertIn("records.bundle could not be opened", str(caught.exception))

    def test_bundles_that_give_no_gear_stop_the_run(self) -> None:
        empty = {name: [] for name in small_game_bundles()}
        with small_game(bundles=empty) as (game, _fake):
            with self.assertRaises(names.GameFilesError) as caught:
                names.read_game(game, progress=False)
        self.assertIn("gave 0 gear prefabs and 0 item records", str(caught.exception))

    def test_a_missing_text_bundle_or_table_stops_the_run(self) -> None:
        with small_game(strings=False) as (game, _fake):
            with self.assertRaises(names.GameFilesError) as caught:
                names.read_game(game, progress=False)
        self.assertIn("text bundle is not there", str(caught.exception))
        bundles = small_game_bundles()
        bundles["localization"] = [FakeObject("MonoBehaviour", 1, FakeFile("CAB-strings"), name="French",
                                              tree={})]
        with small_game(bundles=bundles) as (game, _fake):
            with self.assertRaises(names.GameFilesError) as caught:
                names.read_game(game, progress=False)
        self.assertIn("no table named English", str(caught.exception))

    def test_without_the_package_the_run_stops_with_the_line_that_installs_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            game = make_game(Path(folder), BASE_ASSETS)
            with mock.patch.dict(sys.modules, {"UnityPy": None}):
                with self.assertRaises(names.GameFilesError) as caught:
                    names.read_game(game, progress=False)
        self.assertIn(names.INSTALL_HINT, str(caught.exception))


# ---------------------------------------------------------------------------
# The two commands
# ---------------------------------------------------------------------------

BOTH = {"GEAR_Hatchet": "Hatchet", "GEAR_FilmBoxBW": "FastFilm-500BW"}


class CheckCommandTests(unittest.TestCase):
    def test_equal_names_end_with_zero(self) -> None:
        with small_game() as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", BOTH):
            code, out, err = run(["check", "--game-dir", str(game)])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "Game version: 2.60 189457 S. Parts read: base, DLC01.",
            "Prefabs with an item record: 2. With an English name: 2.",
            "Objects UnityPy could not read, and this tool passed over: 1",
            "All 2 names are the ones in GAME_NAMES in tld_taxonomy.py.",
        ])
        self.assertIn("reading base: prefabs.bundle", err)

    def test_differences_are_listed_by_kind_and_end_with_one(self) -> None:
        table = {"GEAR_Hatchet": "Axe", "GEAR_Stick": "Stick"}
        with small_game() as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", table):
            code, out, _err = run(["check", "--game-dir", str(game)])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[2:], [
            "In the game and not in GAME_NAMES: 1",
            "  GEAR_FilmBoxBW: 'FastFilm-500BW'",
            "Another name in the game: 1",
            "  GEAR_Hatchet: 'Axe' -> 'Hatchet'",
            "In GAME_NAMES and not in the game: 1",
            "  GEAR_Stick: 'Stick'",
            "Objects UnityPy could not read, and this tool passed over: 1",
            "3 names differ from GAME_NAMES in tld_taxonomy.py.",
        ])

    def test_another_game_version_is_pointed_out(self) -> None:
        with small_game(version="2.61 190000 S\n") as (game, _fake), \
                mock.patch.object(taxonomy, "GAME_NAMES", BOTH):
            code, out, _err = run(["check", "--game-dir", str(game)])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], [
            "Game version: 2.61 190000 S. Parts read: base, DLC01.",
            f"GAME_NAMES in tld_taxonomy.py was read from game version {taxonomy.GAME_NAMES_VERSION}, "
            "so differences below are to be expected.",
        ])

    def test_a_game_without_a_version_file_says_not_stated(self) -> None:
        with small_game(version=None) as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", BOTH):
            _code, out, _err = run(["check", "--game-dir", str(game)])
        self.assertTrue(out.startswith("Game version: not stated. Parts read: base, DLC01.\n"))

    def test_what_the_item_table_cannot_place_is_listed_and_does_not_count_as_a_difference(self) -> None:
        bundles = small_game_bundles()
        prefabs = bundles["prefabs.bundle"][0].assets_file
        bundles["prefabs.bundle"] += [game_object(prefabs, 60, "GEAR_NotInAnyTable"),
                                      gear_item(prefabs, 61, owner=60, record_file=1, record_id=500),
                                      game_object(prefabs, 70, "GEAR_Stick"),
                                      gear_item(prefabs, 71, owner=70, record_file=1, record_id=404),
                                      game_object(prefabs, 80, "GEAR_Hatchet"),
                                      gear_item(prefabs, 81, owner=80, record_file=1, record_id=501)]
        bundles["records.bundle"].append(record(bundles["records.bundle"][0].assets_file, 501,
                                                "GearItemData_Other", "GAMEPLAY_Other"))
        with small_game(bundles=bundles) as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", BOTH):
            code, out, _err = run(["check", "--game-dir", str(game)])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[2:8], [
            "Prefabs the item table does not know: 1",
            "  GEAR_NotInAnyTable",
            "Prefabs whose record was not found: 1",
            "  GEAR_Stick",
            "Prefabs whose copies name different records: 1",
            "  GEAR_Hatchet: GearItemData_Hatchet, GearItemData_Other",
        ])

    def test_a_key_with_two_texts_is_listed_and_its_name_is_gone(self) -> None:
        bundles = small_game_bundles()
        bundles["localization"] = string_table(dict(STRINGS, GAMEPLAY_HATCHET="HATCHET"))
        with small_game(bundles=bundles) as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", BOTH):
            code, out, _err = run(["check", "--game-dir", str(game)])
        self.assertEqual(code, 1)
        self.assertIn("In GAME_NAMES and not in the game: 1\n  GEAR_Hatchet: 'Hatchet'\n", out)
        self.assertIn("Prefabs whose key matches table keys with different texts: 1\n"
                      "  GEAR_Hatchet: GAMEPLAY_HATCHET, GAMEPLAY_Hatchet\n", out)

    def test_a_folder_that_is_not_the_game_ends_with_two_and_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, out, err = run(["check", "--game-dir", folder])
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("Stopped: "))
        self.assertEqual(len(err.splitlines()), 1)

    def test_no_folder_at_all_ends_with_two(self) -> None:
        with mock.patch.dict(os.environ, {names.ENV_GAME_DIR: ""}):
            code, out, err = run(["check"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--game-dir FOLDER", err)

    def test_the_folder_can_come_from_the_environment(self) -> None:
        with small_game() as (game, _fake), mock.patch.object(taxonomy, "GAME_NAMES", BOTH), \
                mock.patch.dict(os.environ, {names.ENV_GAME_DIR: str(game)}):
            code, _out, _err = run(["check"])
        self.assertEqual(code, 0)


class ExportCommandTests(unittest.TestCase):
    def test_the_file_holds_what_was_read(self) -> None:
        with small_game() as (game, _fake):
            target = game.parent / "names.json"
            code, out, _err = run(["export", str(target), "--game-dir", str(game)])
            text = target.read_text(encoding="ascii")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"Wrote 2 items, 2 of them with an English name, to {target}\n")
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), {
            "game_version": "2.60 189457 S",
            "parts": ["base", "DLC01"],
            "items": {
                "GEAR_FilmBoxBW": {"name": "FastFilm-500BW", "name_key": "GAMEPLAY_FilmBoxBW",
                                   "data": "GearItemData_FilmBoxBW", "condition_type": -1,
                                   "weight_units": 100000000},
                "GEAR_Hatchet": {"name": "Hatchet", "name_key": "GAMEPLAY_Hatchet",
                                 "data": "GearItemData_Hatchet", "condition_type": -1,
                                 "weight_units": 500000000},
            },
            "condition_steps": [[{"step": "Fresh", "from": 90.0}, {"step": "Ruined", "from": 0.0}],
                                [{"step": "Perfect", "from": 100.0}, {"step": "Ruined", "from": 0.0}]],
            "no_english_text": {},
            "ambiguous_keys": {},
            "no_record": [],
            "two_records": {},
            "unreadable_objects": 1,
        })

    def test_a_name_outside_ascii_is_written_as_an_escape(self) -> None:
        bundles = small_game_bundles()
        bundles["localization"] = string_table(dict(STRINGS, GAMEPLAY_Hatchet="Spelunker\u2019s Hatchet"))
        with small_game(bundles=bundles) as (game, _fake):
            target = game.parent / "names.json"
            run(["export", str(target), "--game-dir", str(game)])
            raw = target.read_bytes()
        self.assertIn(b"Spelunker\\u2019s Hatchet", raw)
        self.assertEqual(json.loads(raw)["items"]["GEAR_Hatchet"]["name"], "Spelunker\u2019s Hatchet")

    def test_nothing_is_written_into_the_game_folder(self) -> None:
        with small_game() as (game, fake):
            before = sorted(str(path) for path in game.rglob("*"))
            for target in (game / "names.json", game / "tld_Data" / "names.json"):
                with self.subTest(target=target.name):
                    code, out, err = run(["export", str(target), "--game-dir", str(game)])
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("inside the game folder", err)
            self.assertEqual(sorted(str(path) for path in game.rglob("*")), before)
        self.assertEqual(fake.loaded, [])

    def test_a_folder_as_the_target_is_refused(self) -> None:
        with small_game() as (game, fake):
            code, _out, err = run(["export", str(game.parent), "--game-dir", str(game)])
        self.assertEqual(code, 2)
        self.assertIn("is a folder", err)
        self.assertEqual(fake.loaded, [])

    def test_a_target_that_cannot_be_written_ends_with_two(self) -> None:
        with small_game() as (game, _fake):
            target = game.parent / "no-such-folder" / "names.json"
            code, _out, err = run(["export", str(target), "--game-dir", str(game)])
        self.assertEqual(code, 2)
        self.assertIn("could not be written", err)


# ---------------------------------------------------------------------------
# A real game folder
# ---------------------------------------------------------------------------

@unittest.skipUnless(GAME_DIR, f"set {names.ENV_GAME_DIR} to the game's folder to read a real game")
@unittest.skipIf(UnityPy is None, f"needs the UnityPy package: {names.INSTALL_HINT}")
class RealGameTests(unittest.TestCase):
    table: names.NameTable
    findings: names.Findings
    parts: List[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.table, cls.findings, cls.parts = names.read_game(Path(GAME_DIR), progress=False)

    def test_the_game_gives_hundreds_of_names_and_all_are_clean(self) -> None:
        found = self.table.names()
        self.assertGreater(len(found), 600)
        self.assertEqual(self.parts[0], "base")
        for prefab, name in found.items():
            with self.subTest(prefab=prefab):
                self.assertEqual(name, names.clean(name))
                self.assertTrue(name.isprintable())
                self.assertEqual(prefab, taxonomy.normalize_prefab(prefab.strip()))

    def test_every_gear_item_finds_its_record(self) -> None:
        self.assertEqual(self.table.no_record, [])
        self.assertEqual(self.findings.unreadable, 0)

    def test_no_name_key_matches_table_keys_with_different_texts(self) -> None:
        self.assertEqual(self.table.ambiguous, {})

    def test_copies_of_a_prefab_name_the_same_record(self) -> None:
        self.assertEqual(self.table.two_records, {})

    def test_every_condition_group_starts_high_and_ends_at_zero(self) -> None:
        self.assertGreaterEqual(len(self.findings.conditions), 6)
        groups = {record["condition_type"] for record in self.table.items.values()}
        self.assertLessEqual(max(group for group in groups if isinstance(group, int)),
                             len(self.findings.conditions) - 1)
        for number, steps in enumerate(self.findings.conditions):
            with self.subTest(group=number):
                lows = [low for _step, low in steps]
                self.assertEqual(lows, sorted(lows, reverse=True))
                self.assertEqual(lows[-1], 0.0)
                self.assertTrue(all(step for step, _low in steps))

    def test_game_names_is_what_the_version_it_was_read_from_gives(self) -> None:
        version = names.game_version(Path(GAME_DIR))
        if not version.startswith(taxonomy.GAME_NAMES_VERSION):
            self.skipTest(f"GAME_NAMES was read from game version {taxonomy.GAME_NAMES_VERSION}, and this "
                          f"game is {version or 'of a version it does not state'}. "
                          "Run: python tld_game_names.py check")
        known, unknown = names.known_prefabs(self.table.names())
        self.assertEqual(unknown, [])
        self.assertEqual(names.compare(known, taxonomy.GAME_NAMES), {"new": [], "different": [], "gone": []})


if __name__ == "__main__":
    unittest.main()
