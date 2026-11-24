r"""Tests for the display names in tld_taxonomy.py.

Run from this folder:
    python -m unittest -v

One test compares the names taken from the C# editor with its resource file,
Properties\Resources.resx, one folder up. It is skipped when this folder has been copied
somewhere without the rest of the repository. One more test compares the wiki names with
the wiki helper's table. It needs the requests package, as the wiki helper does, and is
skipped without it.
"""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict

import tld_taxonomy as taxonomy

try:
    import requests
except ImportError:
    requests = None

# The wiki helper stops while it is being imported when the package is missing.
if requests is not None:
    import tld_wiki_updater as wiki

HERE = Path(__file__).resolve().parent
EDITOR_RESOURCES = HERE.parent / "Properties" / "Resources.resx"

NAME_TABLES: Dict[str, Dict[str, str]] = {
    "editor": taxonomy.EDITOR_NAMES,
    "mod": taxonomy.MOD_NAMES,
    "wiki": taxonomy.WIKI_NAMES,
}

# Mod items whose pack gives no English name. All five are spawn placeholders or leftovers.
MOD_PREFABS_WITHOUT_A_NAME = {"GEAR_CatFoodPackage", "GEAR_RandomBabyFood", "GEAR_RandomBox",
                              "GEAR_RandomCannedFood", "GEAR_RandomPackage"}


def clean(name: str) -> str:
    """A name with runs of spaces made one space and the ends trimmed."""
    return re.sub(r"\s+", " ", name).strip()


class NameTableTests(unittest.TestCase):
    def test_every_named_prefab_is_in_the_item_table(self) -> None:
        for source, table in NAME_TABLES.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(set(table) - set(taxonomy.TAXONOMY_LOOKUP)), [])

    def test_no_prefab_is_named_by_two_sources(self) -> None:
        names = [prefab for table in NAME_TABLES.values() for prefab in table]
        self.assertEqual(len(names), len(set(names)))

    def test_names_are_clean_text(self) -> None:
        for source, table in NAME_TABLES.items():
            for prefab, name in table.items():
                with self.subTest(source=source, prefab=prefab):
                    self.assertTrue(name)
                    self.assertEqual(name, clean(name))
                    self.assertTrue(name.isprintable())

    def test_mod_names_belong_to_mod_prefabs(self) -> None:
        self.assertEqual(sorted(set(taxonomy.MOD_NAMES) - set(taxonomy.MOD_PREFABS)), [])

    def test_every_mod_prefab_has_a_name_except_the_known_five(self) -> None:
        unnamed = {prefab for prefab in taxonomy.MOD_PREFABS if not taxonomy.display_name(prefab)}
        self.assertEqual(unnamed, MOD_PREFABS_WITHOUT_A_NAME)

    @unittest.skipUnless(EDITOR_RESOURCES.is_file(), "the C# editor's Resources.resx is not one folder up")
    def test_editor_names_are_the_ones_in_the_resource_file(self) -> None:
        stored = {entry.get("name", ""): entry.findtext("value") or ""
                  for entry in ElementTree.parse(EDITOR_RESOURCES).getroot().iter("data")}
        expected = {prefab: clean(name) for prefab, name in stored.items()
                    if prefab.startswith("GEAR_") and prefab in taxonomy.TAXONOMY_LOOKUP}
        self.assertEqual(taxonomy.EDITOR_NAMES, expected)

    @unittest.skipIf(requests is None, "needs the requests package: pip install requests")
    def test_wiki_names_are_pairings_of_the_wiki_helper(self) -> None:
        for prefab, name in taxonomy.WIKI_NAMES.items():
            with self.subTest(prefab=prefab):
                self.assertEqual(wiki.WIKI_NAME_TO_PREFAB.get(name), prefab)


class DisplayNameTests(unittest.TestCase):
    def test_a_name_from_each_source(self) -> None:
        for prefab, name, source in (("GEAR_BasicBoots", "Trail Boots", "editor"),
                                     ("GEAR_KeroseneLampB", "Storm Lantern", "editor"),
                                     ("GEAR_AuroraEnergyDrink", "Aurora Energy Drink", "mod"),
                                     ("GEAR_Travois", "Travois", "wiki")):
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.display_name(prefab), name)
                self.assertEqual(taxonomy.display_name_source(prefab), source)

    def test_a_prefab_no_source_names_gives_an_empty_name(self) -> None:
        for prefab in ("GEAR_KeroseneLamp_Spelunkers", "GEAR_NotInAnyTable"):
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.display_name(prefab), "")
                self.assertEqual(taxonomy.display_name_source(prefab), "")

    def test_the_copy_number_unity_adds_is_ignored(self) -> None:
        self.assertEqual(taxonomy.display_name("GEAR_Stick (4)"), "Stick")
        self.assertEqual(taxonomy.display_name_source("GEAR_Stick (4)"), "editor")

    def test_the_editor_name_stands_where_the_wiki_title_differs(self) -> None:
        # The wiki helper pairs "Signal pistol" with this prefab. The editor calls it Distress Pistol.
        self.assertEqual(taxonomy.display_name("GEAR_FlareGun"), "Distress Pistol")
        self.assertNotIn("GEAR_FlareGun", taxonomy.WIKI_NAMES)


if __name__ == "__main__":
    unittest.main()
