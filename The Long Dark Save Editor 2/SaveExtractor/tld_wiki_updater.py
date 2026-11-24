#!/usr/bin/env python3
"""
tld_wiki_updater.py: fetch current item lists from the TLD Fandom wiki and report
wiki items that the taxonomy does not cover.

Run this from your local machine (requires internet access to fandom.com).
It does not modify tld_taxonomy.py. It prints a report so you can decide how to
classify what is missing.

tld_taxonomy.py is looked for next to this script. Use --taxonomy to point at
another copy. The run stops if it cannot be loaded.

Usage:
    python tld_wiki_updater.py                      # report only
    python tld_wiki_updater.py --save sandbox1      # also check a save file
    python tld_wiki_updater.py --patch              # write missing_prefabs.json
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

WIKI_API = "https://thelongdark.fandom.com/api.php"
HEADERS  = {"User-Agent": "TLD-taxonomy-updater/1.0 (local tool)"}

# Wiki categories to enumerate. Checked against the wiki's category list on
# 2026-11-16: "Other" and "Research_Books" do not exist there and returned nothing.
WIKI_CATEGORIES = [
    "Clothing", "Tools", "Materials", "Food_And_Drink",
    "First_Aid", "Fire_Starting", "Story_items", "Collectibles",
]

# Known mapping from wiki display name to GEAR_ prefab name.
# Extend this as new items are discovered. A prefab's internal name often differs
# from its display name (GEAR_BasicBoots is "Trail Boots"), so check new entries
# against Properties/Resources.resx or a real save before adding them.
WIKI_NAME_TO_PREFAB: dict[str, str] = {
    # Food - Meat
    "Bear Meat (Cooked)":          "GEAR_CookedMeatBear",
    "Bear Meat (Raw)":             "GEAR_RawMeatBear",
    "Cougar Meat (Cooked)":        "GEAR_CookedMeatCougar",
    "Cougar Meat (Raw)":           "GEAR_RawMeatCougar",
    "Moose Meat (Cooked)":         "GEAR_CookedMeatMoose",
    "Moose Meat (Raw)":            "GEAR_RawMeatMoose",
    "Rabbit Meat (Cooked)":        "GEAR_CookedMeatRabbit",
    "Rabbit Meat (Raw)":           "GEAR_RawMeatRabbit",
    "Venison (Cooked)":            "GEAR_CookedMeatDeer",
    "Venison (Raw)":               "GEAR_RawMeatDeer",
    "Wolf Meat (Cooked)":          "GEAR_CookedMeatWolf",
    "Wolf Meat (Raw)":             "GEAR_RawMeatWolf",
    # Food - Fish
    "Coho Salmon (Cooked)":        "GEAR_CookedCohoSalmon",
    "Coho Salmon (Raw)":           "GEAR_RawCohoSalmon",
    "Lake Whitefish (Cooked)":     "GEAR_CookedLakeWhiteFish",
    "Lake Whitefish (Raw)":        "GEAR_RawLakeWhiteFish",
    "Rainbow Trout (Cooked)":      "GEAR_CookedRainbowTrout",
    "Rainbow Trout (Raw)":         "GEAR_RawRainbowTrout",
    "Smallmouth Bass (Cooked)":    "GEAR_CookedSmallMouthBass",
    "Smallmouth Bass (Raw)":       "GEAR_RawSmallMouthBass",
    "Burbot (Cooked)":             "GEAR_CookedBurbot",
    "Burbot (Raw)":                "GEAR_RawBurbot",
    "Goldeye (Cooked)":            "GEAR_CookedGoldeye",
    "Goldeye (Raw)":               "GEAR_RawGoldeye",
    "Red Irish Lord (Cooked)":     "GEAR_CookedRedIrishLord",
    "Red Irish Lord (Raw)":        "GEAR_RawRedIrishLord",
    "Rockfish (Cooked)":           "GEAR_CookedRockfish",
    "Rockfish (Raw)":              "GEAR_RawRockfish",
    # Food - Packaged
    "Beef Jerky":                  "GEAR_BeefJerky",
    "Candy Bar":                   "GEAR_CandyBar",
    "Condensed Milk":              "GEAR_CondensedMilk",
    "Dog Food":                    "GEAR_DogFood",
    "Energy Bar":                  "GEAR_EnergyBar",
    "Granola Bar":                 "GEAR_GranolaBar",
    "Maple Syrup":                 "GEAR_MapleSyrup",
    "Military-Grade MRE":          "GEAR_MRE",
    "Peanut Butter":               "GEAR_PeanutButter",
    "Pinnacle Peaches":            "GEAR_PinnacleCanPeaches",
    "Pork and Beans":              "GEAR_CannedBeans",
    "Salty Crackers":              "GEAR_Crackers",
    "Tin of Sardines":             "GEAR_CannedSardines",
    "Tomato Soup":                 "GEAR_TomatoSoupCan",
    # Drinks
    "Coffee":                      "GEAR_CoffeeTin",
    "Herbal Tea":                  "GEAR_GreenTeaPackage",
    "Orange Soda":                 "GEAR_SodaOrange",
    "Potable Water":               "GEAR_WaterSupplyPotable",
    "Stacy's Grape Soda":          "GEAR_SodaGrape",
    "Summit Soda":                 "GEAR_Soda",
    # First Aid
    "Antibiotics":                 "GEAR_BottleAntibiotics",
    "Antiseptic":                  "GEAR_BottleHydrogenPeroxide",
    "Bandage":                     "GEAR_HeavyBandage",
    "Emergency stim":              "GEAR_EmergencyStim",
    "Painkillers":                 "GEAR_BottlePainKillers",
    # Materials
    "Birch bark":                  "GEAR_BarkTinder",
    "Birch sapling":               "GEAR_BirchSapling",
    "Black bear hide":             "GEAR_BearHide",
    "Cloth":                       "GEAR_Cloth",
    "Coal":                        "GEAR_Coal",
    "Crow Feather":                "GEAR_CrowFeather",
    "Cured Birch Sapling":         "GEAR_BirchSaplingDried",
    "Cured Gut":                   "GEAR_GutDried",
    "Cured Leather":               "GEAR_LeatherDried",
    "Cured Maple Sapling":         "GEAR_MapleSaplingDried",
    "Cured Moose Hide":            "GEAR_MooseHideDried",
    "Cured Rabbit Pelt":           "GEAR_RabbitPeltDried",
    "Cured Wolf Pelt":             "GEAR_WolfPeltDried",
    "Fresh Gut":                   "GEAR_Gut",
    "Fresh Leather":               "GEAR_Leather",
    "Fresh Moose Hide":            "GEAR_MooseHide",
    "Fresh Rabbit Pelt":           "GEAR_RabbitPelt",
    "Fresh Wolf Pelt":             "GEAR_WolfPelt",
    "Reclaimed Wood":              "GEAR_ReclaimedWoodB",
    "Scrap Metal":                 "GEAR_ScrapMetal",
    "Stick":                       "GEAR_Stick",
    "Stone":                       "GEAR_Stone",
    # Tools
    "Can Opener":                  "GEAR_CanOpener",
    "Firestriker":                 "GEAR_Firestriker",
    "Fishing tackle":              "GEAR_HookAndLine",
    "Hacksaw":                     "GEAR_Hacksaw",
    "Hatchet":                     "GEAR_Hatchet",
    "Heavy Hammer":                "GEAR_Hammer",
    "Hook":                        "GEAR_Hook",
    "Hunting Knife":               "GEAR_Knife",
    "Hunting Rifle":               "GEAR_Rifle",
    "Improvised Hatchet":          "GEAR_HatchetImprovised",
    "Improvised Knife":            "GEAR_KnifeImprovised",
    "Kerosene":                    "GEAR_LampFuel",
    "Lantern":                     "GEAR_KeroseneLampB",
    "Lantern Fuel":                "GEAR_LampFuel",
    "Line":                        "GEAR_Line",
    "Magnifying Lens":             "GEAR_MagnifyingLens",
    "Mountaineering Rope":         "GEAR_Rope",
    "Newsprint":                   "GEAR_Newsprint",
    "Newsprint Roll":              "GEAR_NewsprintRoll",
    "Prybar":                      "GEAR_Prybar",
    "Quality tools":               "GEAR_HighQualityTools",
    "Rifle Cleaning Kit":          "GEAR_RifleCleaningKit",
    "Sewing Kit":                  "GEAR_SewingKit",
    "Signal pistol":               "GEAR_FlareGun",
    "Simple Tools":                "GEAR_SimpleTools",
    "Snare":                       "GEAR_Snare",
    "Storm Lantern":               "GEAR_KeroseneLampB",
    "Survival Bow":                "GEAR_Bow",
    "Torch":                       "GEAR_Torch",
    "Travois":                     "GEAR_Travois",
    "Whetstone":                   "GEAR_SharpeningStone",
    # Clothing
    "Backpack":                    "GEAR_TechnicalBackpack",
    "Balaclava":                   "GEAR_Balaclava",
    "Baseball Cap":                "GEAR_BaseballCap",
    "Basic Boots":                 "GEAR_BasicBoots",
    "Basic Gloves":                "GEAR_BasicGloves",
    "Basic Winter Coat":           "GEAR_BasicWinterCoat",
    "Basic Wool Hat":              "GEAR_BasicWoolHat",
    "Basic Wool Scarf":            "GEAR_BasicWoolScarf",
    "Bear Skin Bedroll":           "GEAR_BearSkinBedRoll",
    "Bear Skin Coat":              "GEAR_BearSkinCoat",
    "Bedroll":                     "GEAR_BedRoll",
    "Cargo Pants":                 "GEAR_CargoPants",
    "Climbing Socks":              "GEAR_ClimbingSocks",
    "Combat Boots":                "GEAR_CombatBoots",
    "Combat Pants":                "GEAR_CombatPants",
    "Cotton Long Underwear":       "GEAR_LongUnderwear",
    "Cotton Scarf":                "GEAR_CottonScarf",
    "Cotton Socks":                "GEAR_CottonSocks",
    "Cotton Toque":                "GEAR_BasicWoolHat",
    "Cowichan Sweater":            "GEAR_CowichanSweater",
    "Deerskin Boots":              "GEAR_DeerSkinBoots",
    "Deerskin Pants":              "GEAR_DeerSkinPants",
    "Down Ski Jacket":             "GEAR_DownSkiJacket",
    "Down Vest":                   "GEAR_DownVest",
    "Dress Shirt":                 "GEAR_CottonShirt",
    "Driving Gloves":              "GEAR_BasicGloves",
    "Expedition Parka":            "GEAR_PremiumWinterCoat",
    "Fisherman's Sweater":         "GEAR_FishermanSweater",
    "Fleece Cowl":                 "GEAR_WoolWrapCap",
    "Fleece Mittens":              "GEAR_FleeceMittens",
    "Fleece Sweater":              "GEAR_FleeceSweater",
    "Gauntlets":                   "GEAR_Gauntlets",
    "Heavy Wool Sweater":          "GEAR_HeavyWoolSweater",
    "Hoodie":                      "GEAR_CottonHoodie",
    "Insulated Boots":             "GEAR_InsulatedBoots",
    "Insulated Vest":              "GEAR_InsulatedVest",
    "Jeans":                       "GEAR_Jeans",
    "Leather Shoes":               "GEAR_LeatherShoes",
    "Light Shell":                 "GEAR_SkiJacket",
    "Long Wool Scarf":             "GEAR_WoolWrap",
    "Mackinaw Jacket":             "GEAR_MackinawJacket",
    "Mariner's Pea Coat":          "GEAR_QualityWinterCoat",
    "Military Coat":               "GEAR_MilitaryParka",
    "Mittens":                     "GEAR_Mittens",
    "Moose-hide Cloak":            "GEAR_MooseHideCloak",
    "Moose-hide Satchel":          "GEAR_MooseHideBag",
    "Mukluks":                     "GEAR_MuklukBoots",
    "Old Fashioned Parka":         "GEAR_HeavyParka",
    "Plaid Shirt":                 "GEAR_PlaidShirt",
    "Premium Winter Coat":         "GEAR_PremiumWinterCoat",
    "Quality Winter Coat":         "GEAR_QualityWinterCoat",
    "Rabbitskin Mitts":            "GEAR_RabbitSkinMittens",
    "Running Shoes":               "GEAR_BasicShoes",
    "Simple Parka":                "GEAR_LightParka",
    "Ski Boots":                   "GEAR_SkiBoots",
    "Ski Gloves":                  "GEAR_SkiGloves",
    "Ski Jacket":                  "GEAR_DownSkiJacket",
    "Snow Pants":                  "GEAR_InsulatedPants",
    "Sport Vest":                  "GEAR_InsulatedVest",
    "Sports Socks":                "GEAR_CottonSocks",
    "Sweatshirt":                  "GEAR_FleeceSweater",
    "T-Shirt":                     "GEAR_TeeShirt",
    "Tin of Coffee":               "GEAR_CoffeeTin",
    "Trail Boots":                 "GEAR_BasicBoots",
    "Urban Parka":                 "GEAR_DownParka",
    "Windbreaker":                 "GEAR_BasicWinterCoat",
    "Wolf Pelt":                   "GEAR_WolfPelt",
    "Wool Ear Wrap":               "GEAR_EarMuffs",
    "Wool Long Scarf":             "GEAR_WoolWrap",
    "Wool Mittens":                "GEAR_Mittens",
    "Wool Scarf":                  "GEAR_BasicWoolScarf",
    "Wool Shirt":                  "GEAR_WoolShirt",
    "Wool Socks":                  "GEAR_WoolSocks",
    "Wool Sweater":                "GEAR_WoolSweater",
    "Work Boots":                  "GEAR_WorkBoots",
    "Work Gloves":                 "GEAR_WorkGloves",
    "Work Pants":                  "GEAR_WorkPants",
}

# Items to skip (not actual gear items, or redirect pages)
SKIP_TITLES = {
    "Items", "Light Sources", "Car", "Cargo Container", "Metal Container",
    "Plastic Container", "Safe", "Forge", "Rural Store", "Locket",
    "Polaroid Photo", "Climber's Journal Pages", "Cat Tail Head",
    "Green Birch Sapling", "Cardboard Matches",  # old name for Pack Matches
}


def fetch_category_members(category: str, session: "requests.Session") -> list[str]:
    pages: list[str] = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmlimit": 500,
        "cmtype": "page",
        "format": "json",
    }
    while True:
        r = session.get(WIKI_API, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        members = data.get("query", {}).get("categorymembers", [])
        pages.extend(m["title"] for m in members)
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        params["cmcontinue"] = cont
        time.sleep(0.25)
    return pages


def find_taxonomy(explicit: "Path | None") -> Path:
    """Path of tld_taxonomy.py: the --taxonomy argument, or the copy next to this script."""
    script_dir = Path(__file__).resolve().parent
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = [script_dir / "tld_taxonomy.py"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    sys.exit("tld_taxonomy.py not found. Looked in:\n  "
             + "\n  ".join(str(candidate) for candidate in candidates)
             + "\nPass --taxonomy PATH to point at it.")


def load_taxonomy_prefabs(taxonomy_path: Path) -> set[str]:
    """Import tld_taxonomy.py from its path and return the prefab names it knows."""
    spec = importlib.util.spec_from_file_location("tld_taxonomy", taxonomy_path)
    if spec is None or spec.loader is None:
        sys.exit(f"Cannot load {taxonomy_path}")
    module = importlib.util.module_from_spec(spec)
    # Registered under its real name so tld_save_reader imports this same module.
    sys.modules["tld_taxonomy"] = module
    spec.loader.exec_module(module)
    prefabs = set(getattr(module, "TAXONOMY_LOOKUP", {}))
    if not prefabs:
        sys.exit(f"{taxonomy_path} has no TAXONOMY_LOOKUP entries. "
                 f"Stopping, because every wiki item would be reported as new.")
    return prefabs


def load_save_prefabs(save_path: Path, tools_dir: Path) -> set[str]:
    """All GEAR_ prefab names in a save, read with tld_save_reader.py from tools_dir."""
    sys.path.insert(0, str(tools_dir))
    try:
        from tld_save_reader import read_save
    except ImportError as error:
        sys.exit(f"--save needs tld_save_reader.py next to tld_taxonomy.py in {tools_dir}: {error}")
    try:
        contents = read_save(save_path, include_unsearched=True)
    except (OSError, ValueError) as error:
        sys.exit(f"Could not read save file {save_path}: {error}")
    return {record.prefab for record in contents.records}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch TLD wiki item list and report taxonomy gaps."
    )
    parser.add_argument("--save", type=Path, help="Path to a save file to cross-reference")
    parser.add_argument("--patch", action="store_true",
                        help="Write missing items to missing_prefabs.json")
    parser.add_argument("--taxonomy", type=Path,
                        help="Path to tld_taxonomy.py (default: next to this script)")
    args = parser.parse_args()

    # Wiki titles can hold characters the Windows console code page lacks. Without this,
    # redirecting the output to a file stops the run with a UnicodeEncodeError.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    script_dir = Path(__file__).resolve().parent
    taxonomy_path = find_taxonomy(args.taxonomy)
    known_prefabs = load_taxonomy_prefabs(taxonomy_path)
    print(f"Loaded {len(known_prefabs)} prefabs from {taxonomy_path}")

    broken_mappings = {title: prefab for title, prefab in WIKI_NAME_TO_PREFAB.items()
                       if prefab not in known_prefabs}
    if broken_mappings:
        print(f"\nWARNING: {len(broken_mappings)} WIKI_NAME_TO_PREFAB entries point at a prefab "
              f"the taxonomy does not have. Each is a wrong name or a new item:")
        for title, prefab in sorted(broken_mappings.items()):
            print(f"   {title!r} -> {prefab}")

    save_prefabs: set[str] = set()
    if args.save:
        if not args.save.exists():
            sys.exit(f"Save file not found: {args.save}")
        save_prefabs = load_save_prefabs(args.save, taxonomy_path.parent)
        print(f"Loaded {len(save_prefabs)} prefabs from save file.")
        unknown_in_save = save_prefabs - known_prefabs
        if unknown_in_save:
            print(f"\nWARNING: {len(unknown_in_save)} prefabs in save NOT in taxonomy:")
            for p in sorted(unknown_in_save):
                print(f"   {p}")
        else:
            print("OK: all save prefabs are in the taxonomy.")

    print(f"\nFetching wiki categories from {WIKI_API} ...")
    session = requests.Session()
    all_wiki_items: dict[str, list[str]] = {}
    failed_categories: list[str] = []
    for cat in WIKI_CATEGORIES:
        try:
            items = fetch_category_members(cat, session)
            all_wiki_items[cat] = [
                t for t in items
                if t not in SKIP_TITLES and not t.startswith("File:")
            ]
            print(f"  {cat}: {len(all_wiki_items[cat])} items")
            if not items:
                print(f"  WARNING: {cat} returned no pages. The category name may be wrong.")
        except (requests.RequestException, ValueError) as e:
            print(f"  {cat}: ERROR: {e}")
            failed_categories.append(cat)

    # Resolve wiki names to GEAR_ prefabs
    new_prefabs: dict[str, str] = {}  # prefab -> category
    unmappable: list[str] = []
    for cat, items in all_wiki_items.items():
        for title in items:
            prefab = WIKI_NAME_TO_PREFAB.get(title)
            if prefab and prefab not in known_prefabs:
                new_prefabs[prefab] = cat
            elif not prefab and title not in SKIP_TITLES:
                unmappable.append(f"{title}  (wiki cat: {cat})")

    print(f"\n=== RESULTS ===")
    if failed_categories:
        print(f"\nERROR: {len(failed_categories)} of {len(WIKI_CATEGORIES)} wiki categories could not be "
              f"fetched ({', '.join(failed_categories)}), so the results below are incomplete.")
    if new_prefabs:
        print(f"\nNEW: {len(new_prefabs)} prefabs found on wiki, not in taxonomy:")
        for p, cat in sorted(new_prefabs.items()):
            print(f"   {p:<40} <- {cat}")
    else:
        print("\nOK: no new prefabs detected.")

    if unmappable:
        print(f"\nUNMAPPED: {len(unmappable)} wiki items with no known GEAR_ mapping:")
        for item in sorted(set(unmappable)):
            print(f"   {item}")

    if args.patch and failed_categories:
        print("\nmissing_prefabs.json was left as it is because the wiki fetch was incomplete.")
    elif args.patch:
        patch_path = script_dir / "missing_prefabs.json"
        with patch_path.open("w", encoding="utf-8") as f:
            json.dump({
                "new_prefabs": {p: {"wiki_category": c} for p, c in new_prefabs.items()},
                "unmapped_wiki_items": sorted(set(unmappable)),
            }, f, indent=2)
        print(f"\nPatch written to {patch_path}")
        if new_prefabs:
            print("Add these prefabs to TAXONOMY_LOOKUP in tld_taxonomy.py to classify them.")

    if failed_categories:
        sys.exit(1)


if __name__ == "__main__":
    main()
