r"""Tests for where tld_save_reader.py writes its CSV.

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

import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
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


def real_saves() -> Sequence[Path]:
    folder = os.environ.get("TLD_SAVE_DIR")
    return codec.find_saves(Path(folder)) if folder else []


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


if __name__ == "__main__":
    unittest.main()
