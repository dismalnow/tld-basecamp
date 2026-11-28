r"""Tests for the display names in tld_taxonomy.py.

Run from this folder:
    python -m unittest -v

One test compares the names taken from the C# editor with its resource file,
Properties\Resources.resx, one folder up. It is skipped when this folder has been copied
somewhere without the rest of the repository. One more test compares the wiki names with
the wiki helper's table. It needs the requests package, as the wiki helper does, and is
skipped without it.

The names from the game's own files, GAME_NAMES, cannot be read again here: that takes the
game folder and a reader for its packed files. These tests pin what was read.
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

# In the order display_name() trusts them.
NAME_TABLES: Dict[str, Dict[str, str]] = {
    "game": taxonomy.GAME_NAMES,
    "editor": taxonomy.EDITOR_NAMES,
    "mod": taxonomy.MOD_NAMES,
    "wiki": taxonomy.WIKI_NAMES,
}

# Mod items whose pack gives no English name. All five are spawn placeholders or leftovers.
MOD_PREFABS_WITHOUT_A_NAME = {"GEAR_CatFoodPackage", "GEAR_RandomBabyFood", "GEAR_RandomBox",
                              "GEAR_RandomCannedFood", "GEAR_RandomPackage"}

# Prefabs that the game's files and the C# editor name differently. The game's name is shown.
# The editor tells the Polaroids and the four notes apart by place, and the game does not.
# The asset of the cattail plant holds the name key of the Technical Backpack.
GAME_AND_EDITOR_DIFFER = {
    "GEAR_CanyonClimbersCaveNote", "GEAR_CanyonDeadClimberNote", "GEAR_CanyonFishingHutJournal",
    "GEAR_CanyonMinersNote", "GEAR_CattailPlant", "GEAR_PostCard_AC_CentralSpire",
    "GEAR_PostCard_AC_TopShelf", "GEAR_PostCard_BI_EchoOne-RadioTower",
    "GEAR_PostCard_CR_AbandonedLookout", "GEAR_PostCard_FM_MuskegOverlook",
    "GEAR_PostCard_FM_ShortwaveTower", "GEAR_PostCard_ML_ForestryLookout",
    "GEAR_PostCard_ML_LakeOverlook", "GEAR_PostCard_MT_RadioTower", "GEAR_PostCard_PV_SignalHill",
    "GEAR_PostCard_RV_Pensive", "GEAR_PostCard_TM_AndresPeak", "GEAR_PostCard_TM_TailSection",
}

# The game's names that hold a character outside ASCII: an apostrophe or a letter.
GAME_NAMES_OUTSIDE_ASCII = {
    "GEAR_Bow_Woodwrights", "GEAR_BunkerPapers4", "GEAR_KeroseneLamp_Spelunkers", "GEAR_Rifle_Curators",
    "GEAR_Rudiger_Watch", "GEAR_Tale3RudigerNote1", "GEAR_Tale3RudigerNote2", "GEAR_Tale3RudigerNote3",
}

# Prefabs of the item table that no source names. The game's files have no name asset for
# them, or a name key without English text. The mod ones are the five above.
PREFABS_WITHOUT_A_NAME = MOD_PREFABS_WITHOUT_A_NAME | {
    "GEAR_BunkerClue1a", "GEAR_BunkerClue1b", "GEAR_BunkerClue1c", "GEAR_BunkerClue1d", "GEAR_Cereal_B",
    "GEAR_DeerSkinCoat", "GEAR_DeerSkinGloves", "GEAR_DeerSkinHat", "GEAR_DriedMangos",
    "GEAR_ElevatorParts", "GEAR_LeadPipes", "GEAR_MiltonParkNotice", "GEAR_NULL",
    "GEAR_NewsprintBootStuffing", "GEAR_NewsprintInsulation", "GEAR_Pickled_Eggs", "GEAR_Pickled_Onions",
    "GEAR_RockCache_Prefab", "GEAR_ScarfTorn", "GEAR_TalesTestNote1", "Gear_Travois_Dummy",
}


def clean(name: str) -> str:
    """A name with runs of spaces made one space and the ends trimmed."""
    return re.sub(r"\s+", " ", name).strip()


class NameTableTests(unittest.TestCase):
    def test_every_named_prefab_is_in_the_item_table(self) -> None:
        for source, table in NAME_TABLES.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(set(table) - set(taxonomy.TAXONOMY_LOOKUP)), [])

    def test_the_editor_the_mods_and_the_wiki_name_different_prefabs(self) -> None:
        names = [prefab for source, table in NAME_TABLES.items() if source != "game" for prefab in table]
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


class GameNameTests(unittest.TestCase):
    """The names read from the game's own files, v2.60."""

    def test_how_many_prefabs_the_game_names(self) -> None:
        self.assertEqual(len(taxonomy.GAME_NAMES), 695)

    def test_the_game_names_no_mod_item(self) -> None:
        self.assertEqual(sorted(set(taxonomy.GAME_NAMES) & set(taxonomy.MOD_PREFABS)), [])

    def test_names_the_owner_read_in_the_game(self) -> None:
        # On 2026-11-27 the inventory showed these with a word for their condition in front,
        # such as Gourmet Acorn Bannock and New Beef Jerky. The table holds the name without it.
        for prefab, name in (("GEAR_CookedBannock", "Bannock"), ("GEAR_UncookedBannock", "Bannock"),
                             ("GEAR_CookedBannockAcorn", "Acorn Bannock"),
                             ("GEAR_UncookedBannockAcorn", "Acorn Bannock"),
                             ("GEAR_CookedPancakeAcorn", "Acorn Pancakes"),
                             ("GEAR_UncookedPancakeAcorn", "Acorn Pancakes"),
                             ("GEAR_BeefJerky", "Beef Jerky"), ("GEAR_CottonHoodie", "Hoodie"),
                             ("GEAR_RifleAmmoBox", "Rifle Ammunition")):
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.GAME_NAMES[prefab], name)

    def test_a_name_key_is_matched_without_regard_to_case(self) -> None:
        # The assets say GAMEPLAY_FilmBoxColour, and the string table says GAMEPLAY_FilmboxColour.
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_FilmBoxColour"], "FastFilm-500")
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_FilmBoxBW"], "FastFilm-500BW")

    def test_stray_spaces_in_the_games_text_are_gone(self) -> None:
        # The string table has "Prepared Birch Bark " and "Rabbit  (Raw)".
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_BirchbarkPrepared"], "Prepared Birch Bark")
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_RawMeatRabbit"], "Rabbit (Raw)")

    def test_a_prefab_gets_the_name_of_its_own_asset(self) -> None:
        # The asset of the manufactured broken arrow names the prefab of the plain one. The
        # table follows the prefab, which names its asset, and not the asset's own link.
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_BrokenArrow"], "Broken Arrow")
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_BrokenArrowManufactured"], "Broken Manufactured Arrow")

    def test_the_names_outside_ascii(self) -> None:
        outside = {prefab for prefab, name in taxonomy.GAME_NAMES.items() if not name.isascii()}
        self.assertEqual(outside, GAME_NAMES_OUTSIDE_ASCII)
        characters = {character for prefab in outside for character in taxonomy.GAME_NAMES[prefab]
                      if not character.isascii()}
        self.assertEqual(characters, {"\u2019", "\xfc"})
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_KeroseneLamp_Spelunkers"], "Spelunker\u2019s Lantern")
        self.assertEqual(taxonomy.GAME_NAMES["GEAR_Rudiger_Watch"], "R\xfcdiger's Watch")

    def test_the_game_and_the_editor_agree_except_for_the_known_eighteen(self) -> None:
        differ = {prefab for prefab, name in taxonomy.GAME_NAMES.items()
                  if prefab in taxonomy.EDITOR_NAMES and taxonomy.EDITOR_NAMES[prefab] != name}
        self.assertEqual(differ, GAME_AND_EDITOR_DIFFER)

    def test_the_game_says_what_the_wiki_says(self) -> None:
        for prefab, name in taxonomy.WIKI_NAMES.items():
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.GAME_NAMES.get(prefab), name)


class DisplayNameTests(unittest.TestCase):
    def test_a_name_from_each_source(self) -> None:
        for prefab, name, source in (("GEAR_BasicBoots", "Trail Boots", "game"),
                                     ("GEAR_KeroseneLampB", "Storm Lantern", "game"),
                                     ("GEAR_KeroseneLamp_Spelunkers", "Spelunker\u2019s Lantern", "game"),
                                     ("GEAR_AccelerantMedium", "Accelerant", "editor"),
                                     ("GEAR_AuroraEnergyDrink", "Aurora Energy Drink", "mod")):
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.display_name(prefab), name)
                self.assertEqual(taxonomy.display_name_source(prefab), source)

    def test_the_first_source_that_names_a_prefab_decides(self) -> None:
        for prefab in taxonomy.TAXONOMY_LOOKUP:
            holders = [source for source, table in NAME_TABLES.items() if prefab in table]
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.display_name_source(prefab), holders[0] if holders else "")
                self.assertEqual(taxonomy.display_name(prefab),
                                 NAME_TABLES[holders[0]][prefab] if holders else "")

    def test_the_games_name_stands_where_the_editors_differs(self) -> None:
        self.assertEqual(taxonomy.EDITOR_NAMES["GEAR_PostCard_ML_ForestryLookout"],
                         "Polaroid Mystery Lake Forestry Lookout")
        self.assertEqual(taxonomy.display_name("GEAR_PostCard_ML_ForestryLookout"), "Polaroid")
        self.assertEqual(taxonomy.EDITOR_NAMES["GEAR_CattailPlant"], "Cat Tail Plant")
        self.assertEqual(taxonomy.display_name("GEAR_CattailPlant"), "Technical Backpack")

    def test_how_many_prefabs_have_a_name(self) -> None:
        named = [prefab for prefab in taxonomy.TAXONOMY_LOOKUP if taxonomy.display_name(prefab)]
        self.assertEqual((len(named), len(taxonomy.TAXONOMY_LOOKUP)), (1014, 1040))

    def test_the_prefabs_no_source_names(self) -> None:
        unnamed = {prefab for prefab in taxonomy.TAXONOMY_LOOKUP if not taxonomy.display_name(prefab)}
        self.assertEqual(unnamed, PREFABS_WITHOUT_A_NAME)

    def test_a_prefab_no_source_names_gives_an_empty_name(self) -> None:
        for prefab in ("GEAR_DriedMangos", "GEAR_NotInAnyTable"):
            with self.subTest(prefab=prefab):
                self.assertEqual(taxonomy.display_name(prefab), "")
                self.assertEqual(taxonomy.display_name_source(prefab), "")

    def test_the_copy_number_unity_adds_is_ignored(self) -> None:
        self.assertEqual(taxonomy.display_name("GEAR_Stick (4)"), "Stick")
        self.assertEqual(taxonomy.display_name_source("GEAR_Stick (4)"), "game")

    def test_the_games_name_stands_where_the_wiki_title_differs(self) -> None:
        # The wiki helper pairs "Signal pistol" with this prefab. The game calls it Distress Pistol.
        self.assertEqual(taxonomy.display_name("GEAR_FlareGun"), "Distress Pistol")
        self.assertEqual(taxonomy.display_name_source("GEAR_FlareGun"), "game")
        self.assertNotIn("GEAR_FlareGun", taxonomy.WIKI_NAMES)


if __name__ == "__main__":
    unittest.main()
