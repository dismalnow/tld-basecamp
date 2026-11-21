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
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from unittest import mock

import tld_save_codec as codec
import tld_save_edit as edit
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number
from tld_save_edit import EditError, Stack

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

    def test_missing_options_are_a_usage_error(self) -> None:
        for arguments in ([], ["set-count", str(self.save)],
                          ["set-count", str(self.save), "--item", "GEAR_Stick", "--out", str(self.out)],
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


if __name__ == "__main__":
    unittest.main()
