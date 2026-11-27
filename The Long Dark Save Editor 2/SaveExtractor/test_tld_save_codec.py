r"""Tests for tld_save_codec.py.

Run from this folder:
    python -m unittest -v

The tests build their own small saves, so they need no game files. One more test checks
real saves. It is skipped unless TLD_SAVE_DIR names a folder that holds them:
    $env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
    python -m unittest -v
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from typing import Callable, Dict, List, Optional, Tuple

import tld_save_codec as codec
from tld_save_codec import DecodedSave, NestedJson, Number, SaveFormatError

Compressor = Callable[[bytes], bytes]

# Expected bytes come from this repository's own C# compressor (Helpers/CLZF.cs),
# compiled and run on .NET 8 on 2026-11-20. They are fixed values, not computed here.
CSHARP_VECTORS: Tuple[Tuple[str, bytes, str], ...] = (
    ("two bytes", b"ab", "016162"),
    ("no repeats", bytes(range(64)),
     "1f000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
     "1f202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f"),
    ("one byte repeated", b"a" * 40, "016161e01b00016161"),
    ("slot document",
     b'{"m_Dict":{"boot":"AAAA","global":"AAAA"},"m_Version":100,"m_Changelist":189457}',
     "0e7b226d5f44696374223a7b22626f6f2007012241200008222c22676c6f62616cc00f107d2c226d5f"
     "56657273696f6e223a313030400f084368616e67656c69732036063138393435377d"),
    ("repeated records", b'{"m_PrefabName":"GEAR_Stick","m_Count":1},' * 12,
     "1f7b226d5f5072656661624e616d65223a22474541525f537469636b222c226d5f0a436f756e74223a"
     "317d2c7be0ff29e1ba25017d2c"),
    ("long run", b"x" * 300 + b"tail", "017878e0ff00e01900037461696c"),
)


def stored_lzf(data: bytes) -> bytes:
    """A valid LZF stream made of plain runs only, so its bytes differ from the game's compressor."""
    out = bytearray()
    for start in range(0, len(data), 32):
        chunk = data[start:start + 32]
        out.append(len(chunk) - 1)
        out += chunk
    return bytes(out)


def nest(text: str) -> str:
    """Write a JSON document as a JSON text value, the way the game nests documents."""
    return json.dumps(text, ensure_ascii=False)


def gear_text(units: str) -> str:
    stack = '{"m_UnitsProxy":' + units + "}"
    return ('{"m_NormalizedCondition":6.204471E-05,"m_WeightKG":250000000,'
            '"m_StackableItemSerialized":' + nest(stack) + "}")


def global_text(units: str = "3") -> str:
    inventory = ('{"m_SerializedItems":[{"m_PrefabName":"GEAR_Stick","m_SerializedGear":'
                 + nest(gear_text(units)) + "}]}")
    return '{"m_Inventory_Serialized":' + nest(inventory) + ',"m_SecondsSinceVoiceOver":Infinity}'


BOOT_TEXT = '{"m_SceneName":"LakeRegion"}'
SCENE_ITEM = '{"m_PrefabName":"GEAR_Wood","m_Position":[1.5,-2.25E-05,3]}'
SCENE_TEXT = '{"m_Items":[' + ",".join([SCENE_ITEM] * 40) + "]}"


def default_entries(units: str = "3") -> Dict[str, str]:
    return {"boot": BOOT_TEXT, "global": global_text(units), "LakeRegion": SCENE_TEXT}


def encode_entry(text: bytes, compressor: Compressor = codec.lzf_compress) -> str:
    return base64.b64encode(compressor(text)).decode("ascii")


def slot_text(entries: Dict[str, str], compressors: Optional[Dict[str, Compressor]] = None) -> str:
    compressors = compressors or {}
    table = ",".join(
        json.dumps(name) + ':"'
        + encode_entry(text.encode("utf-8"), compressors.get(name, codec.lzf_compress)) + '"'
        for name, text in entries.items()
    )
    return ('{"m_DisplayName":"Test save","m_Timestamp":"2026-11-20 12:00:00","m_Dict":{'
            + table + '},"m_Version":100,"m_Changelist":189457}')


def build_save(entries: Optional[Dict[str, str]] = None,
               compressors: Optional[Dict[str, Compressor]] = None,
               slot_compressor: Compressor = codec.lzf_compress) -> bytes:
    return slot_compressor(slot_text(entries or default_entries(), compressors).encode("utf-8"))


def save_with_slot(slot: str) -> bytes:
    return codec.lzf_compress(slot.encode("utf-8"))


def save_with_boot(encoded: str) -> bytes:
    """A save whose only entry is "boot", holding the given base64 text."""
    return save_with_slot('{"m_Dict":{"boot":"' + encoded + '"}}')


def entry_table(save_bytes: bytes) -> Dict[str, str]:
    return json.loads(codec.lzf_decompress(save_bytes))["m_Dict"]


def first_gear(save: DecodedSave) -> NestedJson:
    inventory = save.entries["global"]["m_Inventory_Serialized"]
    return inventory.value["m_SerializedItems"][0]["m_SerializedGear"]


class LzfTests(unittest.TestCase):
    def test_compress_matches_the_csharp_compressor(self) -> None:
        for name, plain, expected_hex in CSHARP_VECTORS:
            with self.subTest(name):
                self.assertEqual(codec.lzf_compress(plain).hex(), expected_hex)

    def test_decompress_reads_the_csharp_output(self) -> None:
        for name, plain, packed_hex in CSHARP_VECTORS:
            with self.subTest(name):
                self.assertEqual(codec.lzf_decompress(bytes.fromhex(packed_hex)), plain)

    def test_compress_then_decompress_gives_back_the_input(self) -> None:
        generator = random.Random(20261120)
        record = SCENE_ITEM.encode("ascii") + b","
        sizes = [0, 1, 2, 3, 4, 5, 6, 31, 32, 33, 34, 65, 263, 264, 265, 266, 267, 1000,
                 8191, 8192, 8193, 8300, 20000]
        makers = {
            "random bytes": lambda size: bytes(generator.getrandbits(8) for _ in range(size)),
            "four symbols": lambda size: bytes(generator.choice(b"abcd") for _ in range(size)),
            "one symbol": lambda size: b"z" * size,
            "repeated records": lambda size: (record * (size // len(record) + 1))[:size],
        }
        for kind, make in makers.items():
            for size in sizes:
                with self.subTest(kind=kind, size=size):
                    plain = make(size)
                    self.assertEqual(codec.lzf_decompress(codec.lzf_compress(plain)), plain)

    def test_decompress_copies_that_overlap_their_own_output(self) -> None:
        # "ab", then copy 7 bytes starting 2 bytes back: the pair repeats.
        self.assertEqual(codec.lzf_decompress(bytes.fromhex("016162a001")), b"ababababa")
        # "x", then copy 25 bytes starting 1 byte back (the long form with an extra length byte).
        self.assertEqual(codec.lzf_decompress(bytes.fromhex("0078e01000")), b"x" * 26)

    def test_decompress_refuses_damaged_data(self) -> None:
        damaged = {
            "run of plain bytes cut short": "056162",
            "back-reference cut after its first byte": "0061e0",
            "back-reference without its distance byte": "006120",
            "back-reference before the start": "00612005",
            "back-reference with nothing before it": "2000",
        }
        for name, packed_hex in damaged.items():
            with self.subTest(name):
                with self.assertRaises(SaveFormatError):
                    codec.lzf_decompress(bytes.fromhex(packed_hex))


class NumberTests(unittest.TestCase):
    def test_accepts_every_form_the_game_writes(self) -> None:
        for literal in ("0", "-0", "10", "1.5", "-2.25", "1E+08", "6.204471E-05", "1e5", "250000000",
                        "123456789012345678901234567890", "NaN", "Infinity", "-Infinity"):
            with self.subTest(literal):
                self.assertEqual(Number(literal).literal, literal)

    def test_refuses_text_that_is_not_a_json_number(self) -> None:
        for literal in ("", "abc", "01", "1.", ".5", "+1", "1e", "--1", "Inf", " 1", "1 ",
                        "0x10", "1,5"):
            with self.subTest(literal):
                with self.assertRaises(ValueError):
                    Number(literal)

    def test_is_read_only(self) -> None:
        number = Number("1")
        with self.assertRaises(AttributeError):
            number.literal = "2"
        self.assertEqual(number.literal, "1")

    def test_compares_by_what_was_written(self) -> None:
        self.assertEqual(Number("1.0"), Number("1.0"))
        self.assertNotEqual(Number("1.0"), Number("1.00"))
        self.assertNotEqual(Number("1"), "1")
        self.assertEqual(len({Number("1"), Number("1"), Number("1.0")}), 2)

    def test_can_be_copied(self) -> None:
        self.assertEqual(copy.deepcopy(Number("1E+08")), Number("1E+08"))


class ExactJsonTests(unittest.TestCase):
    EXACT_DOCUMENTS = (
        '{"a":6.204471E-05,"b":1E+08,"c":-0,"d":250000000,"e":1.5,"f":-3.40916628E-08}',
        '{"a":Infinity,"b":-Infinity,"c":NaN}',
        '{"a":1e5,"b":1E5,"c":1e+5,"d":0.0,"e":-0.0,"f":123456789012345678901234567890}',
        "[]", "{}", "[[],{}]", '""', "null", "true", "false", "0",
        '{"a":null,"b":true,"c":false,"d":[1,"two",3.0]}',
        r'{"t":"line\nbreak\ttab \"quoted\" back\\slash \u0001 \b\f\r"}',
        '{"name":"Zo\u00eb \u2603 \u65e5\u672c"}',
        '{"path":"C:/Games/TheLongDark","url":"https://example.com/a?b=1&c=2"}',
    )

    def test_documents_are_written_back_identically(self) -> None:
        for text in self.EXACT_DOCUMENTS:
            with self.subTest(text):
                self.assertEqual(codec.render_json(codec.parse_json(text)), text)

    def test_numbers_are_kept_as_written(self) -> None:
        document = codec.parse_json('{"a":1E+08,"b":250000000,"c":Infinity}')
        expected = {"a": Number("1E+08"), "b": Number("250000000"), "c": Number("Infinity")}
        self.assertEqual(document, expected)

    def test_nested_documents_are_opened(self) -> None:
        text = global_text()
        document = codec.parse_json(text)
        inventory = document["m_Inventory_Serialized"]
        self.assertIsInstance(inventory, NestedJson)
        gear = inventory.value["m_SerializedItems"][0]["m_SerializedGear"]
        self.assertIsInstance(gear, NestedJson)
        self.assertEqual(gear.value["m_NormalizedCondition"], Number("6.204471E-05"))
        stack = gear.value["m_StackableItemSerialized"]
        self.assertIsInstance(stack, NestedJson)
        self.assertEqual(stack.value, {"m_UnitsProxy": Number("3")})
        self.assertEqual(codec.render_json(document), text)

    def test_text_that_only_looks_like_json_stays_text(self) -> None:
        for value in ("{not json}", "[1, 2]", "{", "[", "[x]", '{"a": 1}', "{}x"):
            text = '{"a":' + json.dumps(value) + "}"
            with self.subTest(value):
                document = codec.parse_json(text)
                self.assertEqual(document["a"], value)
                self.assertEqual(codec.render_json(document), text)

    def test_a_document_kept_as_text_is_told_apart_from_ordinary_text(self) -> None:
        unread = {
            "space between values": '{"a": 1}',
            "space in a list": "[1, 2]",
            "repeated field name": '{"a":1,"a":2}',
            "escaped letter": r'{"a":"\u00e9"}',
            "line breaks inside": '{\n"a":1\n}',
            "line break at the end": '{"a":1}\n',
            "space in front": ' {"a":1}',
            "tab behind a list": "[1,2]\t",
            "nested too deeply to open": "[" * 5000 + "]" * 5000,
            "nested too deeply to parse": "[" * 200000 + "]" * 200000,
        }
        ordinary = {
            "a document this module opens": '{"a":1}',
            "an empty list": "[]",
            "a note in square brackets": "[Day 3] wolves at the dam [twice]",
            "words in braces": "{not json}",
            "one bracket": "[",
            "cut short": '{"a":1',
            "text behind a document": "{}x",
            "a number": "12",
            "a word": "true",
            "a document in quotes": '"[1, 2]"',
            "white space only": " \n",
            "empty": "",
        }
        for name, value in unread.items():
            with self.subTest(name):
                self.assertTrue(codec.holds_unread_document(value))
                self.assertEqual(codec.parse_json('{"a":' + json.dumps(value) + "}")["a"], value)
        for name, value in ordinary.items():
            with self.subTest(name):
                self.assertFalse(codec.holds_unread_document(value))

    def test_empty_containers_in_text_are_opened_and_written_back(self) -> None:
        text = '{"a":"[]","b":"{}"}'
        document = codec.parse_json(text)
        self.assertEqual(document, {"a": NestedJson([]), "b": NestedJson({})})
        self.assertEqual(codec.render_json(document), text)

    def test_a_copied_document_is_written_back_identically(self) -> None:
        text = global_text()
        self.assertEqual(codec.render_json(copy.deepcopy(codec.parse_json(text))), text)

    def test_documents_that_cannot_be_reproduced_are_refused(self) -> None:
        refused = {
            "space between values": '{"a": 1}',
            "line break at the end": '{"a":1}\n',
            "repeated field name": '{"a":1,"a":2}',
            "escaped slash": r'{"a":"\/"}',
            "escaped letter": r'{"a":"\u00e9"}',
            "escape in capitals": r'{"a":"\u001F"}',
            "cut short": '{"a":1',
            "empty": "",
            "leading zero": '{"a":01}',
            "not json": "hello",
            "nested too deeply": "[" * 5000 + "]" * 5000,
        }
        for name, text in refused.items():
            with self.subTest(name):
                with self.assertRaises(SaveFormatError):
                    codec.parse_json(text)

    def test_render_refuses_values_it_cannot_write_exactly(self) -> None:
        cases = (
            ("python int", {"a": 1}),
            ("python float", {"a": 1.5}),
            ("tuple", ("a",)),
            ("bytes", b"a"),
            ("field name that is not text", {1: "a"}),
        )
        for name, value in cases:
            with self.subTest(name):
                with self.assertRaises(TypeError):
                    codec.render_json(value)


class SaveTests(unittest.TestCase):
    def test_decode_gives_every_document(self) -> None:
        save = codec.decode_save(build_save())
        self.assertEqual(list(save.entries), ["boot", "global", "LakeRegion"])
        self.assertEqual(save.slot["m_DisplayName"], "Test save")
        self.assertEqual(save.slot["m_Version"], Number("100"))
        self.assertEqual(save.entries["boot"], {"m_SceneName": "LakeRegion"})
        self.assertEqual(save.entries["global"]["m_SecondsSinceVoiceOver"], Number("Infinity"))
        stack = first_gear(save).value["m_StackableItemSerialized"]
        self.assertEqual(stack.value["m_UnitsProxy"], Number("3"))
        self.assertEqual(len(save.entries["LakeRegion"]["m_Items"]), 40)

    def test_encode_gives_back_the_same_bytes(self) -> None:
        data = build_save()
        self.assertEqual(codec.encode_save(codec.decode_save(data)), data)

    def test_verify_reports_an_exact_rebuild(self) -> None:
        result = codec.verify_save(build_save())
        self.assertTrue(result.identical, result.detail)
        self.assertEqual(result.entries, 3)
        # The slot document, three entries, and three documents nested as text inside "global".
        self.assertEqual(result.documents, 7)
        self.assertEqual(result.detail, "")

    def test_an_edit_changes_only_the_edited_value(self) -> None:
        original = build_save()
        save = codec.decode_save(original)
        first_gear(save).value["m_StackableItemSerialized"].value["m_UnitsProxy"] = Number("7")
        edited = codec.encode_save(save)

        # Built independently, from text, with the one value changed.
        self.assertEqual(edited, build_save(default_entries(units="7")))

        before, after = entry_table(original), entry_table(edited)
        self.assertEqual(before["boot"], after["boot"])
        self.assertEqual(before["LakeRegion"], after["LakeRegion"])
        self.assertNotEqual(before["global"], after["global"])

    def test_saves_that_cannot_be_decoded_exactly_are_refused(self) -> None:
        padded = next(text for text in (b'{"a":1}', b'{"ab":1}', b'{"abc":1}')
                      if encode_entry(text).endswith("=="))
        canonical = encode_entry(padded)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        loose = canonical[:-3] + alphabet[alphabet.index(canonical[-3]) + 1] + "=="
        self.assertEqual(base64.b64decode(loose), base64.b64decode(canonical))

        refused = {
            "bytes that are not LZF": (b"\xff\xff\xff", "save file"),
            "no entry table": (save_with_slot('{"m_DisplayName":"x"}'), "no m_Dict entry table"),
            "entry table is not a table": (save_with_slot('{"m_Dict":[]}'), "no m_Dict entry table"),
            "entry is a number": (save_with_slot('{"m_Dict":{"boot":5}}'), "expected base64 text"),
            "entry is not base64": (save_with_boot("!!!!"), "not valid base64"),
            "entry in loose base64": (save_with_boot(loose), "base64 is in a form this tool does not write"),
            "entry is not text": (save_with_boot(encode_entry(b"\xff\xfe\xfd")), "not UTF-8 text"),
            "entry with a space in it": (save_with_boot(encode_entry(b'{"a": 1}')),
                                         "cannot be written back identically"),
            "entry cut short": (save_with_boot(encode_entry(b'{"a":1')), "not valid JSON"),
        }
        for name, (data, expected_message) in refused.items():
            with self.subTest(name):
                with self.assertRaises(SaveFormatError) as caught:
                    codec.decode_save(data)
                self.assertIn(expected_message, str(caught.exception))
                result = codec.verify_save(data)
                self.assertFalse(result.identical)
                self.assertIn("cannot be decoded exactly", result.detail)

    def test_verify_notices_an_entry_compressed_another_way(self) -> None:
        scene = SCENE_TEXT.encode("utf-8")
        self.assertNotEqual(stored_lzf(scene), codec.lzf_compress(scene))
        data = build_save(compressors={"LakeRegion": stored_lzf})
        self.assertEqual(len(codec.decode_save(data).entries["LakeRegion"]["m_Items"]), 40)
        result = codec.verify_save(data)
        self.assertFalse(result.identical)
        self.assertIn("compressed bytes differ for: LakeRegion", result.detail)

    def test_verify_notices_a_slot_document_compressed_another_way(self) -> None:
        plain = slot_text(default_entries()).encode("utf-8")
        self.assertNotEqual(stored_lzf(plain), codec.lzf_compress(plain))
        result = codec.verify_save(build_save(slot_compressor=stored_lzf))
        self.assertFalse(result.identical)
        self.assertIn("compressed bytes of the slot document differ", result.detail)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def run_command(self, *arguments: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = codec.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def write(self, name: str, data: bytes) -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_verify_one_file(self) -> None:
        path = self.write("sandbox1", build_save())
        code, out, err = self.run_command("verify", str(path))
        self.assertEqual(code, 0)
        self.assertIn("IDENTICAL", out)
        self.assertIn("1 of 1 saves rebuild byte for byte.", out)
        self.assertEqual(err, "")

    def test_verify_a_folder_checks_only_save_files_in_natural_order(self) -> None:
        for name in ("sandbox10", "sandbox2", "sandbox1", "challenge3"):
            self.write(name, build_save())
        self.write("sandbox1.csv", b"not a save")
        self.write("notes.txt", b"not a save")
        self.write("backups/sandbox1", b"not checked: folders inside are not searched")
        code, out, _ = self.run_command("verify", str(self.folder))
        self.assertEqual(code, 0)
        checked: List[str] = [line.split()[0] for line in out.splitlines() if "IDENTICAL" in line]
        self.assertEqual(checked, ["challenge3", "sandbox1", "sandbox2", "sandbox10"])
        self.assertIn("4 of 4 saves rebuild byte for byte.", out)

    def test_verify_reports_a_file_it_cannot_decode(self) -> None:
        good = self.write("sandbox1", build_save())
        bad = self.write("sandbox2", b"this is not a save file")
        code, out, _ = self.run_command("verify", str(good), str(bad))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertIn("cannot be decoded exactly", out)
        self.assertIn("1 of 2 saves rebuild byte for byte.", out)

    def test_verify_reports_a_save_compressed_another_way(self) -> None:
        path = self.write("sandbox1", build_save(compressors={"LakeRegion": stored_lzf}))
        code, out, _ = self.run_command("verify", str(path))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertIn("compressed bytes differ for: LakeRegion", out)

    def test_a_missing_path_is_a_usage_error(self) -> None:
        code, out, err = self.run_command("verify", str(self.folder / "sandbox99"))
        self.assertEqual(code, 2)
        self.assertIn("Not found", err)
        self.assertEqual(out, "")

    def test_a_folder_without_saves_is_a_usage_error(self) -> None:
        self.write("notes.txt", b"not a save")
        code, _, err = self.run_command("verify", str(self.folder))
        self.assertEqual(code, 2)
        self.assertIn("No save files found", err)

    def test_a_folder_that_cannot_be_listed_is_a_usage_error(self) -> None:
        with mock.patch.object(codec, "find_saves", side_effect=PermissionError("access denied")):
            code, _, err = self.run_command("verify", str(self.folder))
        self.assertEqual(code, 2)
        self.assertIn("Could not list", err)

    def test_no_arguments_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                codec.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_verify_leaves_the_file_and_the_folder_untouched(self) -> None:
        path = self.write("sandbox1", build_save())
        os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))

        def snapshot() -> Tuple[str, int, List[str]]:
            return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
                    sorted(item.name for item in self.folder.iterdir()))

        before = snapshot()
        self.run_command("verify", str(self.folder))
        self.assertEqual(snapshot(), before)


@unittest.skipUnless(os.environ.get("TLD_SAVE_DIR"),
                     "set TLD_SAVE_DIR to a folder of real saves to run this")
class RealSaveTests(unittest.TestCase):
    def test_every_real_save_rebuilds_byte_for_byte(self) -> None:
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        for path in saves:
            with self.subTest(save=path.name):
                result = codec.verify_save(path.read_bytes())
                self.assertTrue(result.identical, result.detail)


if __name__ == "__main__":
    unittest.main()
