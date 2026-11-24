r"""Tests for tld_save_apply.py.

Run from this folder:
    python -m unittest -v

The tests build their own small saves in temporary folders. They never look for the game:
each test supplies the list of running programs itself. One more test works on copies of
real saves in a temporary folder, and only reads the real ones. It is skipped unless
TLD_SAVE_DIR names their folder:
    $env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
    python -m unittest -v
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from unittest import mock

import tld_save_apply as apply
import tld_save_codec as codec
import tld_save_edit as edit
from tld_save_apply import ApplyError, Backup
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number

SAVED_AT = "2026-11-24 04:49:13"
SAVED_LATER = "2026-11-24 05:30:00"
NOW = datetime(2026, 11, 24, 5, 15, 0)
NO_GAME = ("System Idle Process", "explorer.exe", "steam.exe", "python.exe")
COUNT_PATH = ("m_Dict > global > m_Inventory_Serialized > m_SerializedItems[0] > m_SerializedGear > "
              "m_StackableItemSerialized > m_UnitsProxy")


def make_save(name: str = "sandbox15", saved_at: str = SAVED_AT, matches: str = "23",
              hours: str = "3.5") -> bytes:
    """A small save with the fields the game writes outside the entry table."""
    gear: Dict[str, JsonValue] = {
        "m_NormalizedCondition": Number("1.0"),
        "m_StackableItemSerialized": NestedJson({"m_UnitsProxy": Number(matches)}),
    }
    inventory: Dict[str, JsonValue] = {
        "m_SerializedItems": [{"m_PrefabName": "GEAR_WoodMatches", "m_SerializedGear": NestedJson(gear)}]}
    scene_items: List[JsonValue] = [
        {"m_PrefabName": "GEAR_Wood", "m_Position": [Number("1.5"), Number("-2.25E-05")]} for _ in range(20)]
    slot: Dict[str, JsonValue] = {
        "m_InternalName": name,
        "m_DisplayName": "throwaway",
        "m_Timestamp": saved_at,
        "m_GameMode": "SANDBOX",
        "m_GameId": Number("15"),
        "m_Version": Number("100"),
        "m_Changelist": Number("189457"),
        "m_Dict": {
            "boot": {"m_SceneName": "LakeRegion"},
            "global": {"m_Inventory_Serialized": NestedJson(inventory), "m_HoursPlayed": Number(hours)},
            "LakeRegion": {"m_Items": scene_items},
        },
    }
    return codec.encode_save(DecodedSave(slot))


def with_matches(save: bytes, count: int) -> bytes:
    """The save after tld_save_edit.py has changed its one stack."""
    return edit.set_stack_count(save, "GEAR_WoodMatches", count).data


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d-%H%M%S")


class FolderCase(unittest.TestCase):
    """A save folder with one live save, an edited copy of it outside, and no game running."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.saves = self.root / "TheLongDark" / "Survival"
        self.saves.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        self.backups = self.root / "kept"          # not created here: making it is the tool's job
        self.original = make_save()
        self.live = self.saves / "sandbox15"
        self.live.write_bytes(self.original)
        self.edited_data = with_matches(self.original, 40)
        self.edited = self.work / "sandbox15.edited"
        self.edited.write_bytes(self.edited_data)
        self.running: List[str] = list(NO_GAME)
        patcher = mock.patch.object(apply, "running_process_names", side_effect=lambda: list(self.running))
        self.listing = patcher.start()
        self.addCleanup(patcher.stop)
        pause = mock.patch.object(apply.time, "sleep")       # no real waiting between swap attempts
        self.pause = pause.start()
        self.addCleanup(pause.stop)

    def names_in(self, folder: Path) -> List[str]:
        return sorted(path.name for path in folder.iterdir())

    def backup_at(self, moment: datetime, save: str = "sandbox15") -> Path:
        return self.backups / f"{save}.backup-{stamp(moment)}"

    def assert_nothing_changed(self) -> None:
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])
        self.assertFalse(self.backups.exists(), "no backup folder should have been made")

    def apply_now(self, moment: datetime = NOW) -> Path:
        """Put the edited file in place. Returns the backup that was made."""
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=moment)
        apply.carry_out(plan)
        assert plan.backup is not None
        return plan.backup


class GameCheckTests(unittest.TestCase):
    def test_the_game_is_recognized_by_its_program_name(self) -> None:
        names = ("tld.exe", "TLD.EXE", "tld", "tld.x86_64", r"C:\Games\TheLongDark\tld.exe", "/opt/game/tld")
        for name in names:
            with self.subTest(name):
                self.assertTrue(apply.game_is_running(["explorer.exe", name, "steam.exe"]))

    def test_other_programs_are_not_the_game(self) -> None:
        names = ["tldr.exe", "notld.exe", "tld-helper.exe", "tld_save_apply.py", "steam.exe", "python.exe", ""]
        self.assertFalse(apply.game_is_running(names))
        self.assertFalse(apply.game_is_running([]))

    def test_on_windows_the_program_list_comes_from_tasklist(self) -> None:
        listed = (b'"System Idle Process","0","Services","0","8 K"\r\n'
                  b'"steam.exe","9120","Console","1","212,344 K"\r\n'
                  b'"tld.exe","4242","Console","1","2,345,678 K"\r\n')
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=listed, stderr=b"")
        with mock.patch.object(apply.sys, "platform", "win32"), \
                mock.patch.object(apply.subprocess, "run", return_value=done) as run:
            names = apply.running_process_names()
        self.assertEqual(names, ["System Idle Process", "steam.exe", "tld.exe"])
        self.assertEqual(run.call_args[0][0], ["tasklist", "/FO", "CSV", "/NH"])
        self.assertTrue(apply.game_is_running(names))

    def test_elsewhere_the_program_list_comes_from_ps(self) -> None:
        listed = b"systemd\nbash\ntld.x86_64\n"
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=listed, stderr=b"")
        with mock.patch.object(apply.sys, "platform", "linux"), \
                mock.patch.object(apply.subprocess, "run", return_value=done) as run:
            names = apply.running_process_names()
        self.assertEqual(names, ["systemd", "bash", "tld.x86_64"])
        self.assertEqual(run.call_args[0][0], ["ps", "-A", "-o", "comm="])

    def test_a_program_list_that_cannot_be_had_is_an_error(self) -> None:
        failures = (FileNotFoundError("no such program"),
                    subprocess.CalledProcessError(1, "tasklist"),
                    subprocess.TimeoutExpired("tasklist", 60))
        for failure in failures:
            with self.subTest(type(failure).__name__):
                with mock.patch.object(apply.subprocess, "run", side_effect=failure):
                    with self.assertRaisesRegex(ApplyError, "could not check whether the game is running"):
                        apply.running_process_names()

    def test_an_empty_program_list_is_an_error(self) -> None:
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"\r\n", stderr=b"")
        with mock.patch.object(apply.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(ApplyError, "listed no programs"):
                apply.running_process_names()


class ApplyTests(FolderCase):
    def test_the_edited_save_is_put_in_place_after_a_backup(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assertEqual(plan.changes, ((COUNT_PATH, "23", "40"),))
        self.assertEqual(plan.backup, self.backup_at(NOW))
        self.assertEqual((plan.old_saved_at, plan.new_saved_at), (SAVED_AT, SAVED_AT))
        apply.carry_out(plan)
        self.assertEqual(self.live.read_bytes(), self.edited_data)
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"], "nothing else may be left there")
        self.assertEqual(self.names_in(self.backups), [f"sandbox15.backup-{stamp(NOW)}"])
        self.assertEqual(self.edited.read_bytes(), self.edited_data)

    def test_working_out_the_plan_changes_nothing(self) -> None:
        apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_while_the_game_is_running(self) -> None:
        self.running.append("tld.exe")
        with self.assertRaisesRegex(ApplyError, "the game is running"):
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_the_program_list_cannot_be_had(self) -> None:
        self.listing.side_effect = ApplyError("could not check whether the game is running: tasklist failed")
        with self.assertRaisesRegex(ApplyError, "could not check"):
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_the_game_has_saved_since_the_edit_was_made(self) -> None:
        later = make_save(saved_at=SAVED_LATER, hours="4.25")
        self.live.write_bytes(later)
        with self.assertRaises(ApplyError) as caught:
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        message = str(caught.exception)
        self.assertIn(SAVED_LATER, message)
        self.assertIn(SAVED_AT, message)
        self.assertIn("would undo everything the game saved since", message)
        self.assertEqual(self.live.read_bytes(), later)
        self.assertFalse(self.backups.exists())

    def test_refused_for_an_edit_of_another_save(self) -> None:
        self.edited.write_bytes(with_matches(make_save(name="sandbox13"), 40))
        with self.assertRaisesRegex(ApplyError, "is a copy of sandbox13, not of sandbox15"):
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_a_field_outside_the_entries_differs(self) -> None:
        save = codec.decode_save(self.edited_data)
        save.slot["m_DisplayName"] = "renamed"
        self.edited.write_bytes(codec.encode_save(save))
        with self.assertRaisesRegex(ApplyError, "These fields differ: m_DisplayName"):
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_the_edited_file_holds_what_the_save_holds(self) -> None:
        self.edited.write_bytes(self.original)
        with self.assertRaisesRegex(ApplyError, "Nothing to put in place"):
            apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_a_file_does_not_pass_the_rebuild_check(self) -> None:
        broken = self.original[:-1] + bytes([self.original[-1] ^ 0xFF])
        for damaged in ("the edited file", "the live save"):
            with self.subTest(damaged):
                self.live.write_bytes(self.original)
                self.edited.write_bytes(self.edited_data)
                target = self.edited if damaged == "the edited file" else self.live
                target.write_bytes(broken)
                with self.assertRaisesRegex(ApplyError, f"{target.name} does not pass the exact rebuild check"):
                    apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
                self.assertEqual(self.names_in(self.saves), ["sandbox15"])
                self.assertFalse(self.backups.exists())

    def test_refused_when_the_target_is_not_named_like_a_save(self) -> None:
        other = self.saves / "notes.txt"
        other.write_bytes(self.original)
        with self.assertRaisesRegex(ApplyError, "notes.txt is not named like a save file"):
            apply.plan_apply(other, self.edited, self.backups, now=NOW)
        self.assertEqual(other.read_bytes(), self.original)
        self.assertFalse(self.backups.exists())

    def test_refused_when_the_edited_file_is_the_live_save_itself(self) -> None:
        with self.assertRaisesRegex(ApplyError, "is the live save itself"):
            apply.plan_apply(self.live, self.saves / "." / "sandbox15", self.backups, now=NOW)
        self.assert_nothing_changed()

    def test_refused_when_the_backup_folder_is_inside_the_save_folder(self) -> None:
        for inside in (self.saves, self.saves / "backups", self.saves / "a" / "b"):
            with self.subTest(str(inside.relative_to(self.root))):
                with self.assertRaisesRegex(ApplyError, "inside the game's save folder"):
                    apply.plan_apply(self.live, self.edited, inside, now=NOW)
                self.assertEqual(self.live.read_bytes(), self.original)
                self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_a_backup_is_never_written_over_an_existing_file(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        self.backups.mkdir()
        self.backup_at(NOW).write_bytes(b"made by something else in the meantime")
        with self.assertRaisesRegex(ApplyError, "appeared in .* while this was running.*Nothing was changed"):
            apply.carry_out(plan)
        self.assertEqual(self.backup_at(NOW).read_bytes(), b"made by something else in the meantime")
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_two_backups_in_one_second_get_different_names(self) -> None:
        self.apply_now(NOW)
        second = with_matches(self.edited_data, 55)
        self.edited.write_bytes(second)
        self.assertEqual(self.apply_now(NOW).name, f"sandbox15.backup-{stamp(NOW)}-2")
        third = with_matches(second, 60)
        self.edited.write_bytes(third)
        self.assertEqual(self.apply_now(NOW).name, f"sandbox15.backup-{stamp(NOW)}-3")
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)
        self.assertEqual((self.backups / f"sandbox15.backup-{stamp(NOW)}-2").read_bytes(), self.edited_data)
        self.assertEqual((self.backups / f"sandbox15.backup-{stamp(NOW)}-3").read_bytes(), second)
        self.assertEqual(self.live.read_bytes(), third)
        self.assertEqual([backup.path.name for backup in apply.list_backups("sandbox15", self.backups)],
                         [f"sandbox15.backup-{stamp(NOW)}-3", f"sandbox15.backup-{stamp(NOW)}-2",
                          f"sandbox15.backup-{stamp(NOW)}"])

    def test_each_edit_gets_its_own_backup(self) -> None:
        self.apply_now(NOW)
        second = with_matches(self.edited_data, 55)
        self.edited.write_bytes(second)
        later = NOW + timedelta(seconds=90)
        self.apply_now(later)
        self.assertEqual(self.live.read_bytes(), second)
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)
        self.assertEqual(self.backup_at(later).read_bytes(), self.edited_data)

    def test_the_backup_name_comes_from_the_clock_when_no_time_is_given(self) -> None:
        before = datetime.now().replace(microsecond=0)
        plan = apply.plan_apply(self.live, self.edited, self.backups)
        after = datetime.now()
        assert plan.backup is not None
        named = datetime.strptime(plan.backup.name, "sandbox15.backup-%Y%m%d-%H%M%S")
        self.assertTrue(before <= named <= after, plan.backup.name)
        self.assertEqual(plan.backup.parent, self.backups)


class CarryOutFailureTests(FolderCase):
    def test_a_backup_that_cannot_be_written_stops_everything(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        with mock.patch.object(edit.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ApplyError, "the backup was not made, so nothing was changed"):
                apply.carry_out(plan)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])
        self.assertEqual(self.names_in(self.backups), [], "a backup that failed must not be left behind")

    def test_a_backup_folder_that_cannot_be_made_stops_everything(self) -> None:
        (self.root / "kept").write_bytes(b"a file where the folder should go")
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        with self.assertRaisesRegex(ApplyError, "Nothing was changed|nothing was changed"):
            apply.carry_out(plan)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_a_save_that_changes_during_the_run_is_left_alone(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        newer = make_save(saved_at=SAVED_LATER, hours="4.25")
        self.live.write_bytes(newer)
        with self.assertRaisesRegex(ApplyError, "changed while this was running, so it was left alone"):
            apply.carry_out(plan)
        self.assertEqual(self.live.read_bytes(), newer)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)

    def test_a_swap_that_is_refused_for_a_moment_is_tried_again(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        real_replace = os.replace
        refusals = [PermissionError("the file is in use"), PermissionError("the file is in use")]

        def replace(source: str, target: str) -> None:
            if refusals:
                raise refusals.pop()
            real_replace(source, target)

        with mock.patch.object(apply.os, "replace", side_effect=replace) as replace_call:
            apply.carry_out(plan)
        self.assertEqual(replace_call.call_count, 3)
        self.assertEqual(self.pause.call_count, 2)
        self.assertEqual(self.live.read_bytes(), self.edited_data)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_a_swap_that_fails_for_another_reason_is_not_tried_again(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        with mock.patch.object(apply.os, "replace", side_effect=OSError("the disk is gone")) as replace_call:
            with self.assertRaisesRegex(ApplyError, "could not replace sandbox15: the disk is gone"):
                apply.carry_out(plan)
        self.assertEqual(replace_call.call_count, 1)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_a_swap_that_fails_leaves_the_save_as_it_was(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        with mock.patch.object(apply.os, "replace",
                               side_effect=PermissionError("the file is in use")) as replace_call:
            with self.assertRaises(ApplyError) as caught:
                apply.carry_out(plan)
        self.assertEqual(replace_call.call_count, apply.SWAP_ATTEMPTS)
        self.assertEqual(self.pause.call_count, apply.SWAP_ATTEMPTS - 1)
        message = str(caught.exception)
        self.assertIn("could not replace sandbox15", message)
        self.assertIn("It holds what it held before", message)
        self.assertIn(str(self.backup_at(NOW)), message)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"], "the temporary file must be removed")
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)

    def test_a_save_that_reads_back_wrong_is_reported_with_the_way_back(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        real_read = apply._read_file
        reads: List[Path] = []

        def read(path: Path, what: str) -> bytes:
            reads.append(path)
            data = real_read(path, what)
            return data if len(reads) == 1 else data[:-1] + b"?"

        with mock.patch.object(apply, "_read_file", side_effect=read):
            with self.assertRaises(ApplyError) as caught:
                apply.carry_out(plan)
        message = str(caught.exception)
        self.assertIn("did not read back as written", message)
        self.assertIn(f'restore "{self.live}" --from "{self.backup_at(NOW)}" --backup-dir "{self.backups}"',
                      message)
        self.assertEqual(self.backup_at(NOW).read_bytes(), self.original)

    def test_the_backup_is_made_before_the_save_is_touched(self) -> None:
        plan = apply.plan_apply(self.live, self.edited, self.backups, now=NOW)
        seen: List[Tuple[bytes, bool]] = []
        real_replace = os.replace

        def replace(source: str, target: str) -> None:
            seen.append((self.live.read_bytes(), self.backup_at(NOW).is_file()))
            real_replace(source, target)

        with mock.patch.object(apply.os, "replace", side_effect=replace):
            apply.carry_out(plan)
        self.assertEqual(seen, [(self.original, True)])
        self.assertEqual(self.live.read_bytes(), self.edited_data)


class RestoreTests(FolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.kept = self.apply_now(NOW)          # the live save now holds the edit; this is the backup
        self.later = NOW + timedelta(minutes=20)

    def restore(self, backup: Path, moment: datetime) -> apply.Plan:
        plan = apply.plan_restore(self.live, backup, self.backups, now=moment)
        apply.carry_out(plan)
        return plan

    def test_the_backup_is_put_back_and_what_was_there_is_kept(self) -> None:
        plan = self.restore(self.kept, self.later)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.backup_at(self.later).read_bytes(), self.edited_data)
        self.assertEqual(self.kept.read_bytes(), self.original)
        self.assertEqual(plan.changes, ())
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])
        self.assertEqual(len(self.names_in(self.backups)), 2)

    def test_a_restore_can_be_undone(self) -> None:
        self.restore(self.kept, self.later)
        self.restore(self.backup_at(self.later), self.later + timedelta(minutes=1))
        self.assertEqual(self.live.read_bytes(), self.edited_data)

    def test_it_works_after_the_game_has_saved_again(self) -> None:
        newer = make_save(saved_at=SAVED_LATER, matches="38", hours="4.25")
        self.live.write_bytes(newer)
        plan = self.restore(self.kept, self.later)
        self.assertEqual((plan.old_saved_at, plan.new_saved_at), (SAVED_LATER, SAVED_AT))
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.backup_at(self.later).read_bytes(), newer)

    def test_it_works_when_the_live_save_is_missing(self) -> None:
        self.live.unlink()
        plan = self.restore(self.kept, self.later)
        self.assertIsNone(plan.backup)
        self.assertIsNone(plan.old_data)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.names_in(self.backups), [self.kept.name])

    def test_it_works_when_the_live_save_is_damaged(self) -> None:
        self.live.write_bytes(b"not a save at all")
        plan = self.restore(self.kept, self.later)
        self.assertIsNone(plan.old_saved_at)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.backup_at(self.later).read_bytes(), b"not a save at all")

    def test_refused_for_a_backup_of_another_save(self) -> None:
        other = self.backups / "sandbox13.backup-20261101-120000"
        other.write_bytes(make_save(name="sandbox13"))
        with self.assertRaisesRegex(ApplyError, "is a copy of sandbox13, not of sandbox15"):
            apply.plan_restore(self.live, other, self.backups, now=self.later)
        self.assertEqual(self.live.read_bytes(), self.edited_data)

    def test_refused_for_a_backup_that_does_not_pass_the_rebuild_check(self) -> None:
        self.kept.write_bytes(self.original[:-1] + bytes([self.original[-1] ^ 0xFF]))
        with self.assertRaisesRegex(ApplyError, "does not pass the exact rebuild check"):
            apply.plan_restore(self.live, self.kept, self.backups, now=self.later)
        self.assertEqual(self.live.read_bytes(), self.edited_data)

    def test_refused_when_the_save_already_holds_the_backup(self) -> None:
        self.restore(self.kept, self.later)
        with self.assertRaisesRegex(ApplyError, "Nothing to put back"):
            apply.plan_restore(self.live, self.kept, self.backups, now=self.later + timedelta(minutes=1))
        self.assertEqual(self.live.read_bytes(), self.original)

    def test_refused_while_the_game_is_running(self) -> None:
        self.running.append("tld.exe")
        with self.assertRaisesRegex(ApplyError, "the game is running"):
            apply.plan_restore(self.live, self.kept, self.backups, now=self.later)
        self.assertEqual(self.live.read_bytes(), self.edited_data)
        self.assertEqual(self.names_in(self.backups), [self.kept.name])

    def test_refused_when_the_save_folder_does_not_exist(self) -> None:
        with self.assertRaisesRegex(ApplyError, "the save folder .* does not exist"):
            apply.plan_restore(self.root / "nowhere" / "sandbox15", self.kept, self.backups, now=self.later)
        self.assertFalse((self.root / "nowhere").exists())

    def test_refused_when_the_target_is_not_named_like_a_save(self) -> None:
        with self.assertRaisesRegex(ApplyError, "is not named like a save file"):
            apply.plan_restore(self.saves / "sandbox15.old", self.kept, self.backups, now=self.later)
        self.assertEqual(self.names_in(self.saves), ["sandbox15"])

    def test_a_save_that_appears_during_the_run_is_left_alone(self) -> None:
        self.live.unlink()
        plan = apply.plan_restore(self.live, self.kept, self.backups, now=self.later)
        self.live.write_bytes(b"written by something else")
        with self.assertRaisesRegex(ApplyError, "appeared while this was running, so it was left alone"):
            apply.carry_out(plan)
        self.assertEqual(self.live.read_bytes(), b"written by something else")


class BackupListTests(FolderCase):
    def test_backups_of_one_save_are_listed_newest_first(self) -> None:
        self.backups.mkdir()
        older = make_save(saved_at="2026-11-21 02:50:08")
        (self.backups / "sandbox15.backup-20261121-024632").write_bytes(older)
        (self.backups / "sandbox15.backup-20261124-051500").write_bytes(self.original)
        (self.backups / "sandbox1.backup-20261125-000000").write_bytes(make_save(name="sandbox1"))
        (self.backups / "sandbox15.edited").write_bytes(self.edited_data)
        (self.backups / "sandbox15.backup-20261126-000000").mkdir()
        self.assertEqual(apply.list_backups("sandbox15", self.backups), [
            Backup(self.backups / "sandbox15.backup-20261124-051500", len(self.original), SAVED_AT),
            Backup(self.backups / "sandbox15.backup-20261121-024632", len(older), "2026-11-21 02:50:08"),
        ])
        self.assertEqual([backup.path.name for backup in apply.list_backups("sandbox1", self.backups)],
                         ["sandbox1.backup-20261125-000000"])

    def test_backups_made_in_the_same_second_are_ordered_by_their_counter(self) -> None:
        self.backups.mkdir()
        names = ["sandbox15.backup-20261124-051500", "sandbox15.backup-20261124-051500-2",
                 "sandbox15.backup-20261124-051500-10", "sandbox15.backup-20261124-051459",
                 "sandbox15.backup-before-the-long-trip"]
        for name in names:
            (self.backups / name).write_bytes(self.original)
        self.assertEqual([backup.path.name for backup in apply.list_backups("sandbox15", self.backups)],
                         ["sandbox15.backup-before-the-long-trip", "sandbox15.backup-20261124-051500-10",
                          "sandbox15.backup-20261124-051500-2", "sandbox15.backup-20261124-051500",
                          "sandbox15.backup-20261124-051459"])

    def test_a_file_that_is_not_a_save_is_listed_without_a_time(self) -> None:
        self.backups.mkdir()
        (self.backups / "sandbox15.backup-20261124-051500").write_bytes(b"not a save at all")
        self.assertEqual(apply.list_backups("sandbox15", self.backups),
                         [Backup(self.backups / "sandbox15.backup-20261124-051500", 17, None)])

    def test_a_folder_that_does_not_exist_has_no_backups(self) -> None:
        self.assertEqual(apply.list_backups("sandbox15", self.backups), [])
        self.assertFalse(self.backups.exists())


class CommandTests(FolderCase):
    def run_command(self, *arguments: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = apply.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def apply_arguments(self, *more: str) -> Sequence[str]:
        return ("apply", str(self.live), "--from", str(self.edited), "--backup-dir", str(self.backups)) + more

    def only_backup(self) -> Path:
        found = list(self.backups.iterdir())
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_apply_says_what_it_did_and_how_to_undo_it(self) -> None:
        code, out, err = self.run_command(*self.apply_arguments())
        self.assertEqual((code, err), (0, ""))
        backup = self.only_backup()
        self.assertRegex(backup.name, r"^sandbox15\.backup-[0-9]{8}-[0-9]{6}$")
        self.assertEqual(self.live.read_bytes(), self.edited_data)
        self.assertEqual(backup.read_bytes(), self.original)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:       sandbox15, {len(self.original):,} bytes, "
                                   f"last saved by the game at {SAVED_AT}")
        self.assertEqual(lines[1], f"Edited:     {self.edited}, {len(self.edited_data):,} bytes")
        self.assertEqual(lines[2], "Changes:    1 value differs")
        self.assertEqual(lines[3], f"              {COUNT_PATH}: 23 -> 40")
        self.assertEqual(lines[4:7], ["Checked:    the game is not running",
                                      "Checked:    both files pass the exact rebuild check",
                                      "Checked:    the edited file was made from the save as it is now"])
        self.assertEqual(lines[7], f"Backup:     {backup}, read back and identical")
        self.assertEqual(lines[8], "Replaced:   sandbox15 now holds the edited save, read back and identical")
        self.assertEqual(lines[9], f'To undo:    python tld_save_apply.py restore "{self.live}" '
                                   f'--from "{backup}" --backup-dir "{self.backups}"')
        self.assertEqual(len(lines), 10)

    def test_restore_says_what_it_did_and_how_to_undo_it(self) -> None:
        self.run_command(*self.apply_arguments())
        first = self.only_backup()
        code, out, err = self.run_command("restore", str(self.live), "--from", str(first),
                                          "--backup-dir", str(self.backups))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.live.read_bytes(), self.original)
        second = [path for path in self.backups.iterdir() if path != first]
        self.assertEqual([path.read_bytes() for path in second], [self.edited_data])
        lines = out.splitlines()
        self.assertEqual(lines[0], f"Save:       sandbox15, {len(self.edited_data):,} bytes, "
                                   f"last saved by the game at {SAVED_AT}")
        self.assertEqual(lines[1], f"Restoring:  {first}, {len(self.original):,} bytes, "
                                   f"the save as of {SAVED_AT}")
        self.assertEqual(lines[2:4], ["Checked:    the game is not running",
                                      "Checked:    that file passes the exact rebuild check and is a copy "
                                      "of sandbox15"])
        self.assertEqual(lines[4], f"Backup:     {second[0]}, read back and identical")
        self.assertEqual(lines[5], "Replaced:   sandbox15 now holds the restored save, read back and identical")
        self.assertEqual(lines[6], f'To undo:    python tld_save_apply.py restore "{self.live}" '
                                   f'--from "{second[0]}" --backup-dir "{self.backups}"')
        self.assertEqual(len(lines), 7)

    def test_restore_of_a_missing_save_makes_no_backup(self) -> None:
        self.run_command(*self.apply_arguments())
        kept = self.only_backup()
        self.live.unlink()
        code, out, err = self.run_command("restore", str(self.live), "--from", str(kept),
                                          "--backup-dir", str(self.backups))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(out.splitlines()[0], "Save:       sandbox15 does not exist now")
        self.assertNotIn("Backup:", out)
        self.assertNotIn("To undo:", out)
        self.assertEqual(self.names_in(self.backups), [kept.name])

    def test_a_dry_run_checks_everything_and_changes_nothing(self) -> None:
        code, out, err = self.run_command(*self.apply_arguments("--dry-run"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Changes:    1 value differs", out)
        self.assertIn("Dry run:    nothing was changed. Without --dry-run, sandbox15 would be copied to", out)
        self.assertNotIn("Replaced:", out)
        self.assert_nothing_changed()

    def test_a_dry_run_still_refuses_what_apply_would_refuse(self) -> None:
        self.running.append("tld.exe")
        code, out, err = self.run_command(*self.apply_arguments("--dry-run"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Refused: the game is running", err)
        self.assert_nothing_changed()

    def test_a_refusal_is_printed_and_changes_nothing(self) -> None:
        self.running.append("tld.exe")
        code, out, err = self.run_command(*self.apply_arguments())
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines(), ["Refused: the game is running. Close it, then run this again",
                                            "Nothing was changed."])
        self.assert_nothing_changed()

    def test_a_swap_that_fails_is_reported_as_stopped(self) -> None:
        with mock.patch.object(apply.os, "replace", side_effect=PermissionError("the file is in use")):
            code, out, err = self.run_command(*self.apply_arguments())
        self.assertEqual(code, 1)
        self.assertIn("Stopped: could not replace sandbox15", err)
        self.assertNotIn("Replaced:", out)
        self.assertEqual(self.live.read_bytes(), self.original)
        self.assertEqual(self.only_backup().read_bytes(), self.original)

    def test_many_changes_are_cut_short(self) -> None:
        save = codec.decode_save(self.original)
        items = save.entries["LakeRegion"]["m_Items"]
        for position in range(14):
            items[position]["m_PrefabName"] = "GEAR_Stick"
        self.edited.write_bytes(codec.encode_save(save))
        code, out, err = self.run_command(*self.apply_arguments("--dry-run"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Changes:    14 values differ\n", out)
        self.assertEqual(out.count(" -> "), 10)
        self.assertIn("              and 4 more\n", out)

    def test_a_missing_file_is_a_usage_error(self) -> None:
        gone = str(self.saves / "sandbox99")
        for arguments in (("apply", gone, "--from", str(self.edited)),
                          ("apply", str(self.live), "--from", str(self.work / "nothing")),
                          ("restore", str(self.live), "--from", str(self.work / "nothing"))):
            with self.subTest(" ".join(arguments[:1]) + " " + Path(arguments[-1]).name):
                code, out, err = self.run_command(*arguments, "--backup-dir", str(self.backups))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("Not found", err)
        self.assert_nothing_changed()

    def test_missing_options_are_a_usage_error(self) -> None:
        for arguments in ([], ["apply"], ["apply", str(self.live)], ["restore", str(self.live)],
                          ["apply", str(self.live), "--from"], ["backups"]):
            with self.subTest(" ".join(arguments[:1]) or "no arguments"):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        apply.main(arguments)
                self.assertEqual(caught.exception.code, 2)
        self.assert_nothing_changed()

    def test_backups_lists_them_and_says_how_to_put_one_back(self) -> None:
        self.run_command(*self.apply_arguments())
        kept = self.only_backup()
        code, out, err = self.run_command("backups", str(self.live), "--backup-dir", str(self.backups))
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["backup", "bytes", "the", "save", "as", "of"])
        self.assertEqual(lines[1].split(), [kept.name, f"{len(self.original):,}"] + SAVED_AT.split())
        self.assertEqual(lines[2], f"1 backup of sandbox15 in {self.backups}, newest first.")
        self.assertEqual(lines[3], f'To put one back: python tld_save_apply.py restore "{self.live}" '
                                   f'--from "{self.backups / "BACKUP_NAME"}" --backup-dir "{self.backups}"')

    def test_backups_says_so_when_there_are_none(self) -> None:
        code, out, err = self.run_command("backups", "sandbox15", "--backup-dir", str(self.backups))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"No backups of sandbox15 in {self.backups}.\n")
        self.assertFalse(self.backups.exists())

    def test_backups_refuses_a_name_that_is_not_a_save(self) -> None:
        code, out, err = self.run_command("backups", "notes.txt", "--backup-dir", str(self.backups))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is not named like a save file", err)

    def test_without_a_backup_folder_option_backups_go_to_the_home_folder(self) -> None:
        home = self.root / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            code, out, err = self.run_command("apply", str(self.live), "--from", str(self.edited))
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.names_in(home), ["TLD-save-backups"])
            kept = list((home / "TLD-save-backups").iterdir())
            self.assertEqual([path.read_bytes() for path in kept], [self.original])
            self.assertEqual(self.live.read_bytes(), self.edited_data)
            undo = f'To undo:    python tld_save_apply.py restore "{self.live}" --from "{kept[0]}"'
            self.assertEqual(out.splitlines()[-1], undo)
            code, out, err = self.run_command("backups", "sandbox15")
            self.assertEqual((code, err), (0, ""))
            self.assertIn(kept[0].name, out)

    def test_without_a_home_folder_the_backup_folder_must_be_named(self) -> None:
        with mock.patch.object(apply.Path, "home", side_effect=RuntimeError("no home folder")):
            code, out, err = self.run_command("apply", str(self.live), "--from", str(self.edited))
            self.assertEqual((code, out), (2, ""))
            self.assertIn("Say where backups go with --backup-dir", err)
            self.assert_nothing_changed()
            code, out, err = self.run_command(*self.apply_arguments())
            self.assertEqual((code, err), (0, ""))
            self.assertIn(f'--backup-dir "{self.backups}"', out.splitlines()[-1])


@unittest.skipUnless(os.environ.get("TLD_SAVE_DIR"),
                     "set TLD_SAVE_DIR to a folder of real saves to run this")
class RealSaveTests(unittest.TestCase):
    def test_every_real_save_can_be_applied_and_restored_on_a_copy(self) -> None:
        """Works on copies in a temporary folder. The real saves are only read."""
        folder = Path(os.environ["TLD_SAVE_DIR"])
        saves = codec.find_saves(folder)
        self.assertTrue(saves, f"no save files found in {folder}")
        listed_before = sorted((path.name, path.stat().st_size, path.stat().st_mtime_ns)
                               for path in folder.iterdir() if path.is_file())
        with mock.patch.object(apply, "running_process_names", return_value=list(NO_GAME)):
            for path in saves:
                with self.subTest(save=path.name), tempfile.TemporaryDirectory() as temporary:
                    original = path.read_bytes()
                    stacks = edit.list_stacks(codec.decode_save(original))
                    if not stacks:
                        continue
                    target = stacks[0]
                    edited_data = edit.set_stack_count(original, target.prefab, target.count + 1,
                                                       entry=target.entry).data
                    root = Path(temporary)
                    live = root / "Survival" / path.name
                    live.parent.mkdir()
                    live.write_bytes(original)
                    edited = root / f"{path.name}.edited"
                    edited.write_bytes(edited_data)
                    kept = root / "kept"

                    plan = apply.plan_apply(live, edited, kept, now=NOW)
                    self.assertEqual(len(plan.changes), 1)
                    apply.carry_out(plan)
                    assert plan.backup is not None
                    self.assertEqual(live.read_bytes(), edited_data)
                    self.assertEqual(plan.backup.read_bytes(), original)

                    back = apply.plan_restore(live, plan.backup, kept, now=NOW + timedelta(minutes=5))
                    apply.carry_out(back)
                    assert back.backup is not None
                    self.assertEqual(live.read_bytes(), original)
                    self.assertEqual(back.backup.read_bytes(), edited_data)
                    self.assertEqual(sorted(item.name for item in live.parent.iterdir()), [path.name])
                    self.assertEqual(path.read_bytes(), original, "the real save must not change")
        listed_after = sorted((path.name, path.stat().st_size, path.stat().st_mtime_ns)
                              for path in folder.iterdir() if path.is_file())
        self.assertEqual(listed_after, listed_before, "the real save folder must not change")


if __name__ == "__main__":
    unittest.main()
