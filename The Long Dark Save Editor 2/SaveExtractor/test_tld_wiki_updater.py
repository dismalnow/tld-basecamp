r"""Tests for tld_wiki_updater.py.

Run from this folder:
    python -m unittest -v

No test reaches the wiki. A stand-in answers in place of the web session. The report that
--patch writes goes to a temporary folder, so missing_prefabs.json in this folder stays as
it is.

Like the tool, these tests need the requests package:
    pip install requests
Without it they are skipped. One test still runs then: it checks what the tool says when
the package is missing.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

import tld_save_codec as codec
import tld_taxonomy as taxonomy
from tld_save_codec import DecodedSave, JsonValue, NestedJson, Number

try:
    import requests
except ImportError:
    requests = None

# The tool stops while it is being imported when the package is missing, so it is imported
# only when the package is there.
if requests is not None:
    import tld_wiki_updater as wiki

HERE = Path(__file__).resolve().parent
needs_requests = unittest.skipIf(requests is None, "needs the requests package: pip install requests")

KNOWN_TITLE = "Prybar"                 # a wiki name the tool maps to a prefab of the real item table
UNMAPPED_TITLE = "Glow Stick 3000"     # a wiki name the tool has no mapping for


class Answer:
    """One answer from the web, as far as the tool reads it."""

    def __init__(self, payload: object = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Server Error")

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeWiki:
    """Stands in for the web session. It answers from a table and keeps every request it gets.

    parts maps a category to the parts the wiki answers in, each a list of page titles.
    broken maps a category to what it gets in place of that: an error to raise, or an Answer.
    """

    def __init__(self, parts: Dict[str, List[List[str]]], broken: Optional[Dict[str, object]] = None) -> None:
        self.parts = parts
        self.broken = broken or {}
        self.asked: List[Dict[str, Any]] = []

    def get(self, url: str, params: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Answer:
        # The tool reuses one params dictionary for every part, so each request keeps a copy.
        self.asked.append({"url": url, "params": dict(params), "headers": headers, "timeout": timeout})
        if len(self.asked) > 100:
            raise AssertionError("more than 100 requests: the tool is not moving on to the next part")
        category = params["cmtitle"][len("Category:"):]
        problem = self.broken.get(category)
        if isinstance(problem, Exception):
            raise problem
        if isinstance(problem, Answer):
            return problem
        parts = self.parts.get(category, [[]])
        number = int(params["cmcontinue"][len("part-"):]) if "cmcontinue" in params else 0
        payload: Dict[str, Any] = {
            "batchcomplete": "",
            "query": {"categorymembers": [{"ns": 0, "title": title} for title in parts[number]]},
        }
        if number + 1 < len(parts):
            payload["continue"] = {"cmcontinue": f"part-{number + 1}", "continue": "-||"}
        return Answer(payload)

    def categories_asked(self) -> List[str]:
        return [asked["params"]["cmtitle"] for asked in self.asked]


def write_table(path: Path, prefabs: Sequence[str]) -> Path:
    """An item table file that knows only the given prefabs."""
    lines = ["TAXONOMY_LOOKUP = {"]
    lines += [f"    {prefab!r}: ('Tools', 'Test', '')," for prefab in prefabs]
    lines += ["}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_with(*prefabs: str, unsearched: Sequence[str] = ()) -> bytes:
    """A save with one of each given item in the player's inventory.

    unsearched names the items waiting in a locker that nobody has searched yet. Such a container
    stores only the names of the items it will create.
    """
    gear: Dict[str, JsonValue] = {"m_NormalizedCondition": Number("1.0")}
    items: List[JsonValue] = [
        {"m_PrefabName": prefab, "m_SerializedGear": NestedJson(gear)} for prefab in prefabs]
    inventory: Dict[str, JsonValue] = {"m_SerializedItems": items}
    waiting: List[JsonValue] = list(unsearched)
    locker: Dict[str, JsonValue] = {
        "m_PrefabName": "CONTAINER_Locker",
        "m_SearializedContainer": NestedJson({"m_GearToInstantiateSerialized": waiting}),
    }
    containers: Dict[str, JsonValue] = {"m_SerializedContainers": [locker]}
    slot: Dict[str, JsonValue] = {
        "m_InternalName": "sandbox1",
        "m_Dict": {
            "boot": {"m_SceneName": "LakeRegion"},
            "global": {"m_Inventory_Serialized": NestedJson(inventory)},
            "LakeRegion": {"m_ContainerManagerSerialized": NestedJson(containers)},
        },
    }
    return codec.encode_save(DecodedSave(slot))


def exit_message(stop: Any) -> str:
    """The text a run stopped with. stop is what assertRaises(SystemExit) hands back."""
    return str(stop.exception.code)


class TemporaryFolderCase(unittest.TestCase):
    """A temporary folder, and the two things the tool changes for the whole program put back afterwards."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        # Loading an item table registers it as the tld_taxonomy module, and reading a save adds to sys.path.
        self.addCleanup(sys.modules.__setitem__, "tld_taxonomy", sys.modules["tld_taxonomy"])
        self.start(mock.patch.object(sys, "path", list(sys.path)))

    def start(self, patcher: Any) -> Any:
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started


@needs_requests
class FetchTests(unittest.TestCase):
    """fetch_category_members: one category of the wiki, fetched part by part."""

    def setUp(self) -> None:
        patcher = mock.patch.object(wiki.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_answer_gives_its_titles_in_order(self) -> None:
        web = FakeWiki({"Tools": [["Prybar", "Hatchet"]]})
        self.assertEqual(wiki.fetch_category_members("Tools", web), ["Prybar", "Hatchet"])
        self.assertEqual(len(web.asked), 1)
        self.sleep.assert_not_called()

    def test_the_request_asks_for_the_pages_of_the_category(self) -> None:
        web = FakeWiki({"Food_And_Drink": [["Beef Jerky"]]})
        wiki.fetch_category_members("Food_And_Drink", web)
        self.assertEqual(web.asked, [{
            "url": "https://thelongdark.fandom.com/api.php",
            "params": {"action": "query", "list": "categorymembers", "cmtitle": "Category:Food_And_Drink",
                       "cmlimit": 500, "cmtype": "page", "format": "json"},
            "headers": wiki.HEADERS,
            "timeout": 15,
        }])

    def test_a_long_category_is_fetched_part_by_part(self) -> None:
        web = FakeWiki({"Clothing": [["Balaclava", "Jeans"], ["Mittens"], ["Mukluks", "T-Shirt"]]})
        self.assertEqual(wiki.fetch_category_members("Clothing", web),
                         ["Balaclava", "Jeans", "Mittens", "Mukluks", "T-Shirt"])
        self.assertEqual([asked["params"].get("cmcontinue") for asked in web.asked], [None, "part-1", "part-2"])

    def test_it_pauses_between_parts(self) -> None:
        web = FakeWiki({"Clothing": [["Balaclava"], ["Mittens"], ["Mukluks"]]})
        wiki.fetch_category_members("Clothing", web)
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.25), mock.call(0.25)])

    def test_an_answer_without_a_page_list_gives_no_titles(self) -> None:
        web = FakeWiki({}, broken={"Tools": Answer({"batchcomplete": ""})})
        self.assertEqual(wiki.fetch_category_members("Tools", web), [])

    def test_an_error_status_is_raised(self) -> None:
        web = FakeWiki({}, broken={"Tools": Answer(status=503)})
        with self.assertRaises(requests.HTTPError):
            wiki.fetch_category_members("Tools", web)


@needs_requests
class TaxonomyFileTests(TemporaryFolderCase):
    """find_taxonomy and load_taxonomy_prefabs: which item table is used, and what it must hold."""

    def test_the_default_is_the_table_next_to_the_tool(self) -> None:
        self.assertEqual(wiki.find_taxonomy(None), HERE / "tld_taxonomy.py")

    def test_a_given_file_is_used(self) -> None:
        table = write_table(self.root / "another_table.py", ["GEAR_Hatchet"])
        self.assertEqual(wiki.find_taxonomy(table), table)

    def test_a_missing_file_stops_the_run_and_names_the_place(self) -> None:
        missing = self.root / "tld_taxonomy.py"
        with self.assertRaises(SystemExit) as stop:
            wiki.find_taxonomy(missing)
        self.assertIn("tld_taxonomy.py not found", exit_message(stop))
        self.assertIn(str(missing), exit_message(stop))
        self.assertIn("--taxonomy PATH", exit_message(stop))

    def test_the_real_table_gives_every_prefab(self) -> None:
        self.assertEqual(wiki.load_taxonomy_prefabs(HERE / "tld_taxonomy.py"), set(taxonomy.TAXONOMY_LOOKUP))

    def test_a_small_table_gives_its_prefabs(self) -> None:
        table = write_table(self.root / "tld_taxonomy.py", ["GEAR_Hatchet", "GEAR_Prybar"])
        self.assertEqual(wiki.load_taxonomy_prefabs(table), {"GEAR_Hatchet", "GEAR_Prybar"})

    def test_a_table_with_no_entries_stops_the_run(self) -> None:
        empty = write_table(self.root / "tld_taxonomy.py", [])
        with self.assertRaises(SystemExit) as stop:
            wiki.load_taxonomy_prefabs(empty)
        self.assertIn(f"{empty} has no TAXONOMY_LOOKUP entries", exit_message(stop))

    def test_a_file_without_the_table_stops_the_run(self) -> None:
        other = self.root / "tld_taxonomy.py"
        other.write_text("SOMETHING_ELSE = 1\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as stop:
            wiki.load_taxonomy_prefabs(other)
        self.assertIn(f"{other} has no TAXONOMY_LOOKUP entries", exit_message(stop))

    def test_the_loaded_table_becomes_the_tld_taxonomy_module(self) -> None:
        table = write_table(self.root / "tld_taxonomy.py", ["GEAR_Hatchet"])
        wiki.load_taxonomy_prefabs(table)
        self.assertEqual(sys.modules["tld_taxonomy"].__file__, str(table))


@needs_requests
class SaveFileTests(TemporaryFolderCase):
    """load_save_prefabs: the prefabs of a save, read with the save reader."""

    def test_it_returns_every_prefab_in_the_save(self) -> None:
        save = self.root / "sandbox1"
        save.write_bytes(save_with("GEAR_WoodMatches", "GEAR_Hatchet", "GEAR_NotInAnyTable"))
        self.assertEqual(wiki.load_save_prefabs(save, HERE),
                         {"GEAR_WoodMatches", "GEAR_Hatchet", "GEAR_NotInAnyTable"})

    def test_it_includes_the_items_of_containers_nobody_has_searched(self) -> None:
        save = self.root / "sandbox1"
        save.write_bytes(save_with("GEAR_WoodMatches", unsearched=["GEAR_Hatchet", "GEAR_Prybar"]))
        self.assertEqual(wiki.load_save_prefabs(save, HERE),
                         {"GEAR_WoodMatches", "GEAR_Hatchet", "GEAR_Prybar"})

    def test_a_file_that_is_not_a_save_stops_the_run(self) -> None:
        junk = self.root / "sandbox1"
        junk.write_bytes(b"this is not a save")
        with self.assertRaises(SystemExit) as stop:
            wiki.load_save_prefabs(junk, HERE)
        self.assertIn(f"Could not read save file {junk}", exit_message(stop))

    def test_without_the_save_reader_the_run_stops_and_names_the_folder(self) -> None:
        # None in sys.modules makes the import of the reader fail.
        with mock.patch.dict(sys.modules, {"tld_save_reader": None}):
            with self.assertRaises(SystemExit) as stop:
                wiki.load_save_prefabs(self.root / "sandbox1", self.root)
        self.assertIn(f"--save needs tld_save_reader.py next to tld_taxonomy.py in {self.root}",
                      exit_message(stop))


@needs_requests
class ReportTests(TemporaryFolderCase):
    """The whole run: from the command line to the printed report and the report file."""

    def setUp(self) -> None:
        super().setUp()
        self.real_table = str(HERE / "tld_taxonomy.py")
        self.report = self.root / "missing_prefabs.json"
        # The tool writes its report next to itself. For these tests that is the temporary folder.
        self.start(mock.patch.object(wiki, "__file__", str(self.root / "tld_wiki_updater.py")))
        self.start(mock.patch.object(wiki.time, "sleep"))

    def web(self, titles: Optional[Dict[str, List[str]]] = None,
            broken: Optional[Dict[str, object]] = None) -> FakeWiki:
        """A wiki whose categories each hold one item the real table knows, except the ones given."""
        parts = {category: [[KNOWN_TITLE]] for category in wiki.WIKI_CATEGORIES}
        parts.update({category: [found] for category, found in (titles or {}).items()})
        return FakeWiki(parts, broken)

    def table_without(self, *prefabs: str) -> str:
        """The real item table, less the given prefabs."""
        kept = sorted(set(taxonomy.TAXONOMY_LOOKUP) - set(prefabs))
        return str(write_table(self.root / "reduced_table.py", kept))

    def run_updater(self, web: FakeWiki, *arguments: str) -> Tuple[object, str]:
        """Run the tool's command line against the stand-in wiki. Returns its exit value and what it printed."""
        out = io.StringIO()
        code: object = 0
        with mock.patch.object(wiki.requests, "Session", return_value=web), \
                mock.patch("sys.argv", ["tld_wiki_updater.py", *arguments]), \
                contextlib.redirect_stdout(out):
            try:
                wiki.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_a_wiki_that_matches_the_table_reports_nothing_new(self) -> None:
        code, out = self.run_updater(self.web(), "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn(f"Loaded {len(taxonomy.TAXONOMY_LOOKUP)} prefabs from {self.real_table}", out)
        self.assertIn("OK: no new prefabs detected.", out)
        for word in ("NEW:", "UNMAPPED:", "WARNING", "ERROR"):
            self.assertNotIn(word, out)

    def test_every_category_is_asked_for_once(self) -> None:
        web = self.web()
        _, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(web.categories_asked(), [f"Category:{category}" for category in wiki.WIKI_CATEGORIES])
        for category in wiki.WIKI_CATEGORIES:
            self.assertIn(f"  {category}: 1 items\n", out)

    def test_without_the_taxonomy_option_the_table_next_to_the_tool_is_used(self) -> None:
        table = write_table(self.root / "tld_taxonomy.py", ["GEAR_Prybar"])
        code, out = self.run_updater(self.web())
        self.assertEqual(code, 0)
        self.assertIn(f"Loaded 1 prefabs from {table}", out)

    def test_a_missing_table_stops_the_run_before_the_wiki_is_asked(self) -> None:
        web = self.web()
        code, _ = self.run_updater(web, "--taxonomy", str(self.root / "no_such_table.py"))
        self.assertIn("tld_taxonomy.py not found", str(code))
        self.assertEqual(web.asked, [])

    def test_an_item_without_a_mapping_is_listed_as_unmapped(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, UNMAPPED_TITLE]})
        code, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn("  Tools: 2 items\n", out)
        self.assertIn("UNMAPPED: 1 wiki items with no known GEAR_ mapping:", out)
        self.assertIn(f"   {UNMAPPED_TITLE}  (wiki cat: Tools)\n", out)
        self.assertIn("OK: no new prefabs detected.", out)

    def test_unmapped_items_are_listed_in_alphabetical_order(self) -> None:
        web = self.web({"Tools": ["Zeta Thing", "Alpha Thing"], "Materials": ["Mid Thing"]})
        _, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertIn("UNMAPPED: 3 wiki items with no known GEAR_ mapping:\n"
                      "   Alpha Thing  (wiki cat: Tools)\n"
                      "   Mid Thing  (wiki cat: Materials)\n"
                      "   Zeta Thing  (wiki cat: Tools)\n", out)

    def test_skipped_titles_and_file_pages_are_not_counted(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, "Forge", "File:Prybar.png"]})
        code, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn("Forge", wiki.SKIP_TITLES)
        self.assertIn("  Tools: 1 items\n", out)
        self.assertNotIn("UNMAPPED:", out)

    def test_a_mapped_item_the_table_lacks_is_reported_as_new(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, "Hatchet"]})
        code, out = self.run_updater(web, "--taxonomy", self.table_without("GEAR_Hatchet"))
        self.assertEqual(code, 0)
        self.assertIn(f"Loaded {len(taxonomy.TAXONOMY_LOOKUP) - 1} prefabs from", out)
        self.assertIn("NEW: 1 prefabs found on wiki, not in taxonomy:", out)
        self.assertIn(f"   {'GEAR_Hatchet':<40} <- Tools\n", out)
        self.assertNotIn("OK: no new prefabs detected.", out)

    def test_new_prefabs_are_listed_in_alphabetical_order(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, "Hatchet", "Hacksaw"]})
        _, out = self.run_updater(web, "--taxonomy", self.table_without("GEAR_Hatchet", "GEAR_Hacksaw"))
        self.assertIn("NEW: 2 prefabs found on wiki, not in taxonomy:\n"
                      f"   {'GEAR_Hacksaw':<40} <- Tools\n"
                      f"   {'GEAR_Hatchet':<40} <- Tools\n", out)

    def test_a_mapping_to_a_prefab_the_table_lacks_gets_a_warning(self) -> None:
        titles = sorted(title for title, prefab in wiki.WIKI_NAME_TO_PREFAB.items() if prefab == "GEAR_Hatchet")
        self.assertIn("Hatchet", titles)
        _, out = self.run_updater(self.web(), "--taxonomy", self.table_without("GEAR_Hatchet"))
        self.assertIn(f"WARNING: {len(titles)} WIKI_NAME_TO_PREFAB entries point at a prefab", out)
        self.assertIn("   'Hatchet' -> GEAR_Hatchet\n", out)

    def test_an_empty_category_gets_a_warning(self) -> None:
        code, out = self.run_updater(self.web({"Materials": []}), "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn("  Materials: 0 items\n", out)
        self.assertIn("  WARNING: Materials returned no pages. The category name may be wrong.", out)

    def test_a_category_that_cannot_be_reached_fails_the_run(self) -> None:
        web = self.web(broken={"Materials": requests.ConnectionError("no route to the wiki")})
        code, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(code, 1)
        self.assertIn("  Materials: ERROR: no route to the wiki\n", out)
        self.assertIn(f"ERROR: 1 of {len(wiki.WIKI_CATEGORIES)} wiki categories could not be fetched "
                      f"(Materials), so the results below are incomplete.", out)
        self.assertEqual(len(web.asked), len(wiki.WIKI_CATEGORIES))       # the others are still fetched

    def test_an_error_status_fails_the_run(self) -> None:
        web = self.web(broken={"Tools": Answer(status=503)})
        code, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(code, 1)
        self.assertIn("  Tools: ERROR: 503 Server Error\n", out)

    def test_an_answer_that_is_not_json_fails_the_run(self) -> None:
        web = self.web(broken={"Tools": Answer(ValueError("Expecting value: line 1 column 1 (char 0)"))})
        code, out = self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(code, 1)
        self.assertIn("  Tools: ERROR: Expecting value", out)

    def test_patch_writes_the_report_next_to_the_tool(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, "Hatchet", "Zeta Thing", "Alpha Thing"],
                        "Materials": ["Mid Thing"]})
        code, out = self.run_updater(web, "--patch", "--taxonomy", self.table_without("GEAR_Hatchet"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8")), {
            "new_prefabs": {"GEAR_Hatchet": {"wiki_category": "Tools"}},
            "unmapped_wiki_items": ["Alpha Thing  (wiki cat: Tools)", "Mid Thing  (wiki cat: Materials)",
                                    "Zeta Thing  (wiki cat: Tools)"],
        })
        self.assertIn(f"Patch written to {self.report}", out)
        self.assertIn("Add these prefabs to TAXONOMY_LOOKUP in tld_taxonomy.py to classify them.", out)

    def test_patch_with_nothing_new_writes_an_empty_report(self) -> None:
        code, out = self.run_updater(self.web(), "--patch", "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8")),
                         {"new_prefabs": {}, "unmapped_wiki_items": []})
        self.assertNotIn("Add these prefabs", out)

    def test_patch_leaves_the_report_alone_when_a_category_failed(self) -> None:
        self.report.write_bytes(b"the last complete report")
        web = self.web(broken={"Tools": requests.ConnectionError("no route to the wiki")})
        code, out = self.run_updater(web, "--patch", "--taxonomy", self.real_table)
        self.assertEqual(code, 1)
        self.assertEqual(self.report.read_bytes(), b"the last complete report")
        self.assertIn("missing_prefabs.json was left as it is because the wiki fetch was incomplete.", out)

    def test_no_report_is_written_without_patch(self) -> None:
        web = self.web({"Tools": [KNOWN_TITLE, UNMAPPED_TITLE]})
        self.run_updater(web, "--taxonomy", self.real_table)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [])

    def test_the_report_in_this_folder_is_never_written(self) -> None:
        stored = HERE / "missing_prefabs.json"
        before = stored.read_bytes()
        self.run_updater(self.web({"Tools": [UNMAPPED_TITLE]}), "--patch", "--taxonomy", self.real_table)
        self.assertEqual(stored.read_bytes(), before)
        self.assertTrue(self.report.is_file())

    def test_a_missing_save_stops_the_run_before_the_wiki_is_asked(self) -> None:
        web = self.web()
        missing = self.root / "sandbox1"
        code, _ = self.run_updater(web, "--save", str(missing), "--taxonomy", self.real_table)
        self.assertEqual(code, f"Save file not found: {missing}")
        self.assertEqual(web.asked, [])

    def test_a_save_whose_items_are_all_in_the_table(self) -> None:
        save = self.root / "sandbox1"
        save.write_bytes(save_with("GEAR_WoodMatches", "GEAR_Hatchet"))
        code, out = self.run_updater(self.web(), "--save", str(save), "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn("Loaded 2 prefabs from save file.", out)
        self.assertIn("OK: all save prefabs are in the taxonomy.", out)

    def test_a_save_with_an_item_the_table_lacks(self) -> None:
        save = self.root / "sandbox1"
        save.write_bytes(save_with("GEAR_WoodMatches", "GEAR_NotInAnyTable"))
        code, out = self.run_updater(self.web(), "--save", str(save), "--taxonomy", self.real_table)
        self.assertEqual(code, 0)
        self.assertIn("WARNING: 1 prefabs in save NOT in taxonomy:\n   GEAR_NotInAnyTable\n", out)
        self.assertNotIn("OK: all save prefabs are in the taxonomy.", out)


@needs_requests
class TableTests(unittest.TestCase):
    """The tool's own tables, checked against the item table."""

    def test_every_mapped_prefab_is_in_the_item_table(self) -> None:
        unknown = {title: prefab for title, prefab in wiki.WIKI_NAME_TO_PREFAB.items()
                   if prefab not in taxonomy.TAXONOMY_LOOKUP}
        self.assertEqual(unknown, {})

    def test_no_skipped_title_has_a_mapping(self) -> None:
        self.assertEqual(wiki.SKIP_TITLES & set(wiki.WIKI_NAME_TO_PREFAB), set())

    def test_the_categories_asked_for(self) -> None:
        self.assertEqual(wiki.WIKI_CATEGORIES, ["Clothing", "Tools", "Materials", "Food_And_Drink", "First_Aid",
                                                "Fire_Starting", "Story_items", "Collectibles"])

    def test_the_names_the_tests_rely_on(self) -> None:
        self.assertIn(wiki.WIKI_NAME_TO_PREFAB[KNOWN_TITLE], taxonomy.TAXONOMY_LOOKUP)
        self.assertNotIn(UNMAPPED_TITLE, wiki.WIKI_NAME_TO_PREFAB)
        self.assertNotIn(UNMAPPED_TITLE, wiki.SKIP_TITLES)


class MissingPackageTests(unittest.TestCase):
    """What the tool says when the requests package is not installed. Runs with or without the package."""

    def test_the_tool_names_the_package_to_install(self) -> None:
        # None in sys.modules makes "import requests" fail, whether or not the package is installed.
        program = ("import sys; sys.path.insert(0, sys.argv[1]); sys.modules['requests'] = None; "
                   "import tld_wiki_updater")
        done = subprocess.run([sys.executable, "-c", program, str(HERE)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Missing dependency: pip install requests", done.stderr)
        self.assertEqual(done.stdout, "")


if __name__ == "__main__":
    unittest.main()
