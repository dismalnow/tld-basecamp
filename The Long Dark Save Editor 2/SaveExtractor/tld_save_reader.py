#!/usr/bin/env python3
"""
tld_save_reader.py: extract items from a The Long Dark save file (v2.44+).
No third-party dependencies required. Pure Python stdlib only.

Usage:
    python tld_save_reader.py [save_file] [--csv] [--out FILE] [--scene SCENE_NAME]
                              [--summary] [--include-unsearched]

--csv writes <save name>.csv to the folder TLD-save-reports in your home folder.
--out FILE writes the CSV to FILE.

Item categories and the source column (base game, DLC, mod) come from
tld_taxonomy.py, which must sit next to this file.
The save file is only read, never written.
"""

import argparse
import base64
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tld_taxonomy import TAXONOMY_LOOKUP, normalize_prefab, prefab_source


# ---------------------------------------------------------------------------
# Pure-Python LZF decompressor
# ---------------------------------------------------------------------------

def _lzf_decompress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        ctrl = data[i]; i += 1
        if ctrl < 32:
            length = ctrl + 1
            out += data[i:i + length]; i += length
        else:
            length = (ctrl >> 5) + 2
            if length == 9: length += data[i]; i += 1
            ref = ((ctrl & 0x1f) << 8) + data[i] + 1; i += 1
            pos = len(out) - ref
            if pos < 0:
                raise ValueError(f"Invalid LZF back-reference at byte {i}")
            for _ in range(length):
                out.append(out[pos]); pos += 1
    return bytes(out)


def _decompress(data: bytes) -> bytes:
    for skip in (0, 1):
        try:
            return _lzf_decompress(data[skip:])
        except (ValueError, IndexError):
            pass
    raise ValueError("LZF decompression failed")


# ---------------------------------------------------------------------------
# Location enrichment
# ---------------------------------------------------------------------------

# Human-readable names for indoor scene keys.
# Keys are matched as prefixes (before any _ + GUID suffix).
# ---------------------------------------------------------------------------
# TLD Location Database — derived from save file coordinate analysis
# Coverage status:
#   Full (from saves):   Mystery Lake, Coastal Highway, Pleasant Valley, Mountain Town
#   Partial (from saves): Ravine, Highway Transition
#   Stub (no save data): Timberwolf Mtn, Desolation Point, Forlorn Muskeg,
#                        Ash Canyon, Blackrock, Bleak Inlet
# ---------------------------------------------------------------------------

SCENE_DISPLAY_NAMES = {
    # Mystery Lake
    "CampOffice":                "Camp Office",
    "LakeCabinA":                "Lake Cabin A",
    "LakeCabinB":                "Lake Cabin B",
    "LakeCabinC":                "Lake Cabin C",
    "LakeCabinD":                "Lake Cabin D",
    "LakeCabinE":                "Lake Cabin E",
    "LakeCabinF":                "Lake Cabin F",
    "TrailerA":                  "Camp Office Trailer A",
    "TrailerB":                  "Camp Office Trailer B",
    "TrailerC":                  "Camp Office Trailer C",
    "TrailerD":                  "Camp Office Trailer D",
    "DamTrailerA":               "Dam Trailer A",
    "DamTrailerB":               "Dam Trailer B",
    "Dam":                       "Carter Hydro Dam",
    "DamTransitionZone":         "Dam Transition Zone",
    "DamRiverTransitionZoneB":   "Dam River Transition",
    "DamCaveTransitionZone":     "Dam Cave Transition",
    "SafeHouseA":                "Bunker",
    "PrepperCacheFEmpty":        "Prepper Cache F",
    "PrepperCacheF":             "Prepper Cache F",
    "PrepperCacheA":             "Prepper Cache A",
    "PrepperCacheB":             "Prepper Cache B",
    "PrepperCacheC":             "Prepper Cache C",
    "PrepperCacheD":             "Prepper Cache D",
    "PrepperCacheE":             "Prepper Cache E",
    # Coastal Highway
    "QuonsetGasStation":         "Quonset Gas Station",
    "RadioControlHut":           "Radio Control Hut",
    "LighthouseA":               "Lighthouse",
    "CoastalHouseA":             "Coastal House A",
    "CoastalHouseB":             "Coastal House B",
    "CoastalHouseC":             "Coastal House C",
    "CoastalHouseD":             "Coastal House D",
    "CoastalHouseE":             "Coastal House E",
    "CoastalHouseF":             "Coastal House F",
    "CoastalHouseH":             "Coastal House H",
    "FishingCabinA":             "Fishing Cabin A",
    "FishingCabinB":             "Fishing Cabin B",
    "FishingCabinC":             "Fishing Cabin C",
    "FishingCabinD":             "Fishing Cabin D",
    "MineTransitionZone":        "Mine Entrance",
    "HighwayMineTransitionZone": "Highway Mine Transition",
    "HouseBasementC":            "House Basement",
    # Pleasant Valley
    "FarmHouseA":                "Farmstead",
    "FarmHouseABasement":        "Farmstead Basement",
    "BarnHouseA":                "Barn",
    "RuralStoreA":               "Rural Store",
    # Timberwolf Mountain
    "MountainCaveA":             "Mountain Cave A",
    # Desolation Point
    "WhalingShipA":              "The Riken",
    "WhalingWarehouseA":         "Hibernia Processing",
    "WhalingMine":               "Desolation Point Mine",
    "TrailerSShape":             "S-Shaped Trailer",
    # Transition zones
    "RavineTransitionZone":      "The Ravine",
    "HighwayTransitionZone":     "Highway Transition",
}

# Region shown for each scene. The longest matching prefix of the scene key wins.
# Outdoor zone names follow Properties/Resources.resx (RegionsWithMap_*) in this
# repository. MountainTownRegion is "Mountain Town", the game's own label. Milton is
# the town inside that region, and it keeps its name in the landmarks further down.
# Interior entries come from the original table and from what real saves show.
# region_for_scene() keeps one only when that region's zone is in the same save.
SCENE_TO_REGION = {
    # Outdoor zones
    "LakeRegion":            "Mystery Lake",
    "CoastalRegion":         "Coastal Highway",
    "RuralRegion":           "Pleasant Valley",
    "MarshRegion":           "Forlorn Muskeg",
    "WhalingStation":        "Desolation Point",
    "CrashMountain":         "Timberwolf Mountain",
    "MountainTown":          "Mountain Town",
    "TracksRegion":          "Broken Railroad",
    "RiverValleyRegion":     "Hushed River Valley",
    "CanneryRegion":         "Bleak Inlet",
    "AshCanyonRegion":       "Ash Canyon",
    "BlackrockRegion":       "Blackrock",
    "Ravine":                "Ravine",
    "HighwayTransitionZone": "Crumbling Highway",
    # Mystery Lake
    "CampOffice":            "Mystery Lake",
    "LakeCabin":             "Mystery Lake",
    "Trailer":               "Mystery Lake",
    "DamTrailer":            "Mystery Lake",
    "Dam":                   "Mystery Lake",
    "SafeHouseA":            "Mystery Lake",
    "PrepperCache":          "Mystery Lake",
    # Coastal Highway
    "QuonsetGasStation":     "Coastal Highway",
    "CoastalHouse":          "Coastal Highway",
    "FishingCabin":          "Coastal Highway",
    "HouseBasementC":        "Coastal Highway",
    # Pleasant Valley
    "FarmHouseA":            "Pleasant Valley",
    "BarnHouseA":            "Pleasant Valley",
    "RuralStoreA":           "Pleasant Valley",
    # Timberwolf Mountain
    "MountainCaveA":         "Timberwolf Mountain",
    "BlackrockCaveA":        "Timberwolf Mountain",
    # Mountain Town (the town of Milton)
    "BankA":                 "Mountain Town",
    "ChurchB":               "Mountain Town",
    "ConvenienceStoreA":     "Mountain Town",
    "FarmHouseB":            "Mountain Town",
    "GreyMothersHouseA":     "Mountain Town",
    "MiltonHouse":           "Mountain Town",
    "PostOfficeA":           "Mountain Town",
    # Broken Railroad
    "MaintenanceShedA":      "Broken Railroad",
    # Desolation Point
    "LighthouseA":           "Desolation Point",
    "WhalingShipA":          "Desolation Point",
    "WhalingWarehouseA":     "Desolation Point",
    "WhalingMine":           "Desolation Point",
    "TrailerSShape":         "Desolation Point",
}

# Each entry: (landmark_name, center_x, center_z, min_y, max_y, radius)
# min_y / max_y = -1 means ignore elevation
# radius = max XZ distance from center to claim this label
REGION_LANDMARKS = {
    "LakeRegion": [
        ("Camp Office (Exterior)",       1020,   438,   -1,   -1,  200),
        ("Fishing Hut 1",                1307,   287,   -1,   -1,  120),
        ("Fishing Hut 2",                1383,   193,   -1,   -1,  120),
        ("Fishing Hut 3",                1528,   224,   -1,   -1,  120),
        ("Fishing Hut 4",                1580,   422,   -1,   -1,  120),
        ("Trapper's Homestead",          1640,   852,   -1,   -1,  200),
        ("Signal Hill (Summit)",          790,   957,  180,  999,  150),
        ("Plane Crash / Signal Hill",     804,   944,  120,  180,  150),
        ("Signal Hill (Approach)",        820,   935,   -1,  120,  200),
        ("Alan's Cave",                  1260,   653,   80,  999,  150),
        ("Unnamed Pond",                   57,  1037,   -1,   -1,  250),
        ("Deadfall Area",                1252,  1600,   -1,   -1,  250),
        ("Railway Tracks (NE)",          1629,  1272,   -1,   -1,  200),
        ("Ravine Approach (South)",       949,  -224,   -1,   -1,  200),
        ("Northern Forest",               215,  1363,   -1,   -1,  250),
        ("Frozen Creek (West)",           290,   422,   -1,   -1,  250),
        ("Logging Camp",                  870,   229,   -1,   -1,  200),
        ("Mystery Lake (Open Ice)",      1050,   400,   -1,   25,  600),
    ],
    "CoastalRegion": [
        ("Fishing Cabin (Jackrabbit Is.)",  -690,   401,   -1,   30,  120),
        ("Fishing Cabin (Inlet)",           -411,   595,   -1,   30,  120),
        ("Fishing Cabin (Mid-Lake)",        -570,   673,   -1,   30,  120),
        ("Fishing Cabin (East Shore)",        63,   628,   -1,   30,  120),
        ("Fishing Cabin (SE Shore)",          80,   309,   -1,   30,  120),
        ("Fishing Cabin (Central)",          368,   695,   -1,   30,  120),
        ("Fishing Cabin (SW)",               390,   494,   -1,   30,  120),
        ("Quonset Gas Station (Area)",      -749,   657,   -1,   -1,  150),
        ("Coastal Townsite",                -226,   302,   -1,   -1,  200),
        ("Rabbit Grove",                    -189,   545,   -1,   -1,  200),
        ("Cannery / Ship Area",             -503,   793,   -1,   -1,  200),
        ("Lookout (West Cliffs)",           -995,  1178,  150,  999,  200),
        ("Crumbling Highway (West)",       -1191,   848,   80,  999,  200),
        ("Remote Pass (NW)",              -1120,   309,  100,  999,  200),
        ("Highway (Roadside Cars)",          753,   643,   -1,   40,  250),
        ("Frozen River Corridor (East)",     655,  -154,   -1,   -1,  200),
        ("Coastal Highway (Open Ice)",      -100,   500,   -1,   28,  999),
    ],
    "RuralRegion": [
        ("Crashed Plane Site",              480,  1700,  140,  999,  350),
        ("Skeeter's Ridge",                 476,   593,   40,  999,  150),
        ("Farmstead Area",                 1111,   693,   -1,   -1,  200),
        ("Southern Road",                  1541,   112,   -1,   -1,  200),
        ("Upper Valley (Roadside)",        2350,  2270,   -1,   70,  300),
        ("Coastal Cave Pass (Upper)",      2450,   480,  180,  999,  200),
        ("Fishing Cabin (PV Lake)",        2507,  1208,  100,  999,  150),
        ("Northern Farmland",               370,  2624,   -1,   -1,  250),
        ("Upper Pass (NW)",                 361,  2266,  160,  999,  200),
        ("Pleasant Valley (Open Fields)",  1400,  1200,   -1,   70,  800),
    ],
    "MountainTownRegion": [
        ("Milton (Lower Town)",             444,   716,   -1,   20,  250),
        ("Farmhouse (Milton)",              593,  1555,  200,  999,  150),
        ("Police Station",                  964,  1234,  290,  999,  150),
        ("Northern Cache (Milton)",         628,  2048,  270,  999,  200),
        ("Upper Road (Milton)",            1100,  1740,  250,  999,  300),
        ("Mountaineer's Route",            1350,  1620,  340,  999,  200),
        ("Summit Approach (Milton)",       1880,  2065,  380,  999,  300),
        ("High Pass",                      2000,  1445,  370,  999,  200),
    ],
    "RavineTransitionZone": [
        ("Ravine (Upper Cliffs)",          -800,  -130,  120,  999,  200),
        ("Ravine (Valley Floor)",          -710,  -192,   -1,   25,  150),
        ("Ravine (Entry, ML Side)",       -1070,    24,  130,  999,  200),
    ],
    "HighwayTransitionZone": [
        ("Highway Transition (Cars)",       430,   380,   60,  999,  200),
        ("Highway Transition (Far End)",    580,   505,   60,  999,  200),
    ],
    # Stub regions — single large-radius catch-all until save data is available
    "CrashMountainRegion": [
        ("Timberwolf Mountain",            1000,  1000,   -1,   -1, 9999),
    ],
    "WhalingStationRegion": [
        ("Desolation Point",                500,   500,   -1,   -1, 9999),
    ],
    "MarshRegion": [
        ("Forlorn Muskeg",                  500,   500,   -1,   -1, 9999),
    ],
}



def _nearest_landmark(region: str, x: float, z: float, y: float) -> str:
    landmarks = REGION_LANDMARKS.get(region)
    if not landmarks:
        return None
    best_name, best_dist = None, float("inf")
    for name, lx, lz, min_y, max_y, radius in landmarks:
        if min_y != -1 and y < min_y:
            continue
        if max_y != -1 and y > max_y:
            continue
        dist = math.sqrt((x - lx) ** 2 + (z - lz) ** 2)
        if dist <= radius and dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name


def _scene_display_name(raw_key: str) -> str:
    """Convert raw m_Dict key to human-readable scene name."""
    # Strip GUID suffix (e.g. LakeCabinA_5f695d84-79c9...)
    base = raw_key.split("_")[0] if "_" in raw_key else raw_key
    # Try exact match first, then prefix match
    if raw_key in SCENE_DISPLAY_NAMES:
        return SCENE_DISPLAY_NAMES[raw_key]
    if base in SCENE_DISPLAY_NAMES:
        return SCENE_DISPLAY_NAMES[base]
    # Fallback: clean up camelCase / underscores
    name = re.sub(r"_[0-9a-f]{8}-[0-9a-f]{4}.*$", "", raw_key)  # strip GUIDs
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.replace("_", " ").strip()


def _region_for_scene(scene_name: str) -> Optional[str]:
    """Return the region key used in REGION_LANDMARKS, if this scene is an outdoor region."""
    base = scene_name.split("_")[0]
    return scene_name if scene_name in REGION_LANDMARKS else (base if base in REGION_LANDMARKS else None)


def build_location(scene_name: str, x: Optional[float], y: Optional[float], z: Optional[float]) -> str:
    """
    Return a human-readable location string:
    - Indoor scene  → "Camp Office, Mystery Lake"
    - Outdoor scene → "Near Camp Office (exterior), Mystery Lake" or "Mystery Lake (Wilderness)"
    - Player        → "Player Inventory"
    """
    if scene_name == "player_inventory":
        return "Player Inventory"

    display = _scene_display_name(scene_name)

    # Determine if this is an outdoor region that needs coordinate lookup
    region = _region_for_scene(scene_name)
    if region and x is not None and z is not None:
        landmark = _nearest_landmark(region, x, z or 0, y or 0)
        if landmark:
            return f"Near {landmark}"
        # Beyond landmark radius — generic region name
        return display

    return display


def _static_region(scene_name: str) -> str:
    """Region from SCENE_TO_REGION, using the longest prefix that matches."""
    best = ""
    for prefix in SCENE_TO_REGION:
        if scene_name.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return SCENE_TO_REGION.get(best, "")


def _is_outdoor_zone(scene_name: str) -> bool:
    """True for region and transition-zone scenes, False for interiors.

    Checked against every scene name in the v2.60 game catalogs.
    """
    base = scene_name.split("_")[0]
    return base.endswith("Region") or "Zone" in base or "TrackSection" in base


def region_for_scene(scene_name: str, zone_names: list[str]) -> str:
    """Region label for a scene, checked against the outdoor zones present in the save.

    Generic interiors such as TrailerC are reused in several regions, so the table's
    answer is kept only when that region's zone is in the same save. When every zone
    in the save belongs to one region, an interior can only be in that region.
    """
    label = _static_region(scene_name)
    if _is_outdoor_zone(scene_name) or not zone_names:
        return label
    zone_labels = {_static_region(zone) for zone in zone_names}
    if label and label in zone_labels:
        return label
    if len(zone_labels) == 1:
        return zone_labels.pop()
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_scene_blob(b64_str: str) -> dict:
    raw = base64.b64decode(b64_str + "==")
    return json.loads(_decompress(raw))


def _parse_json_field(val) -> dict:
    if isinstance(val, str):
        return json.loads(val) if val.strip() else {}
    return val or {}


def _json_value(val: Any) -> Any:
    """Decode a field that may hold JSON text. Unlike _parse_json_field it also allows lists."""
    if isinstance(val, str):
        return json.loads(val) if val.strip() else None
    return val


def _condition(gear: dict) -> Optional[float]:
    # m_CurrentHPProxy is raw hit points (a wolfskin coat has 500), so it is not a
    # substitute when the normalized value is missing.
    v = gear.get("m_NormalizedCondition")
    return round(v * 100, 1) if v is not None else None


def _count(gear: dict) -> int:
    stack = gear.get("m_StackableItemSerialized")
    if not stack:
        return 1
    if isinstance(stack, str):
        stack = json.loads(stack)
    return int(stack.get("m_UnitsProxy", 1))


def _position(gear: dict) -> tuple:
    pos = gear.get("m_Position")
    if pos and len(pos) == 3:
        return round(pos[0], 2), round(pos[1], 2), round(pos[2], 2)
    return None, None, None


def _pretty_name(prefab: str) -> str:
    name = prefab
    for prefix in ("GEAR_", "CONTAINER_", "CORPSE_", "CON_"):
        name = name.removeprefix(prefix)
    name = name.replace("_", " ")
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.strip()


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ItemRecord:
    scene: str                   # raw scene key
    region: str                  # e.g. "Mystery Lake"
    location: str                # human-readable location
    location_type: str           # "player_inventory" | "floor" | "scripted" |
                                 # "container" | "container_unsearched"
    container_prefab: str
    category: str                # from tld_taxonomy.py, "Unknown" if not listed
    subcategory: str
    item: str
    prefab: str                  # GEAR_ name with any Unity " (N)" suffix removed
    source: str                  # "base", "DLC01", "DLC02", "mod:<pack>", "legacy", "unknown"
    count: int
    condition_pct: Optional[float]
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

SKIP_SCENES = {"boot", "info", "sandbox_global", "screenshot"}


def _make_record(scene: str, region: str, location: str, location_type: str,
                 container_prefab: str, raw_prefab: str, gear: dict,
                 x: Optional[float], y: Optional[float], z: Optional[float]) -> "ItemRecord":
    prefab = normalize_prefab(raw_prefab)
    category, subcategory, _leaf = TAXONOMY_LOOKUP.get(prefab, ("Unknown", "", ""))
    return ItemRecord(
        scene=scene,
        region=region,
        location=location,
        location_type=location_type,
        container_prefab=container_prefab,
        category=category,
        subcategory=subcategory,
        item=_pretty_name(prefab),
        prefab=prefab,
        source=prefab_source(prefab),
        count=_count(gear),
        condition_pct=_condition(gear),
        x=x, y=y, z=z,
    )


def extract_player_inventory(global_scene: dict) -> list:
    records = []
    inv = _parse_json_field(global_scene.get("m_Inventory_Serialized", {}))
    for item in inv.get("m_SerializedItems", []):
        gear = _parse_json_field(item.get("m_SerializedGear", "{}"))
        records.append(_make_record(
            "player_inventory", "", "Player Inventory", "player_inventory", "",
            item["m_PrefabName"], gear, None, None, None,
        ))
    return records


def _scripted_items(scene: dict) -> list:
    """Items the game has placed but not made interactive yet (visor notes, keys).

    They live in m_GearItemInteractabilityControlSerialized, not in the gear manager.
    """
    items = []
    for control in _json_value(scene.get("m_GearItemInteractabilityControlSerialized")) or []:
        control = _json_value(control) or {}
        save_list = _json_value(control.get("m_GearSaveList")) or {}
        items.extend(save_list.get("m_SerializedItems") or [])
    return items


def extract_scene(scene_name: str, scene: dict, region: Optional[str] = None,
                  include_unsearched: bool = False) -> list:
    records = []
    if region is None:
        region = _static_region(scene_name)

    # Floor / loose gear, then scripted world items
    gear_mgr = _parse_json_field(scene.get("m_GearManagerSerialized", {}))
    world_items = [("floor", item) for item in gear_mgr.get("m_SerializedItems", [])]
    world_items += [("scripted", item) for item in _scripted_items(scene)]
    for location_type, item in world_items:
        gear = _parse_json_field(item.get("m_SearializedGear", "{}"))
        x, y, z = _position(gear)
        location = build_location(scene_name, x, y, z)
        records.append(_make_record(
            scene_name, region, location, location_type, "",
            item["m_PrefabName"], gear, x, y, z,
        ))

    # Containers
    cont_mgr = _parse_json_field(scene.get("m_ContainerManagerSerialized", {}))
    for container in cont_mgr.get("m_SerializedContainers", []):
        cont_prefab = _pretty_name(container.get("m_PrefabName", "Unknown"))
        cont_data = _parse_json_field(container.get("m_SearializedContainer", "{}"))
        pos_src = cont_data.get("m_Position") or container.get("m_Position") or [None, None, None]
        cx = round(pos_src[0], 2) if pos_src[0] is not None else None
        cy = round(pos_src[1], 2) if len(pos_src) > 1 and pos_src[1] is not None else None
        cz = round(pos_src[2], 2) if len(pos_src) > 2 and pos_src[2] is not None else None
        location = build_location(scene_name, cx, cy, cz)

        for item in cont_data.get("m_SerializedItems", []):
            gear_raw = item.get("m_SearializedGear") or item.get("m_SerializedGear") or "{}"
            gear = _parse_json_field(gear_raw)
            records.append(_make_record(
                scene_name, region, location, "container", cont_prefab,
                item["m_PrefabName"], gear, cx, cy, cz,
            ))

        if include_unsearched:
            # A container nobody has searched yet stores only the names of the gear
            # it will create, so there is no count or condition to report.
            for raw_prefab in cont_data.get("m_GearToInstantiateSerialized") or []:
                records.append(_make_record(
                    scene_name, region, location, "container_unsearched", cont_prefab,
                    raw_prefab, {}, cx, cy, cz,
                ))

    return records


@dataclass
class SaveContents:
    records: list                # ItemRecord, in save order
    header: dict                 # the save's top-level fields, without m_Dict
    failed_scenes: list          # scene keys that could not be decoded


def read_save(path: Path, include_unsearched: bool = False) -> SaveContents:
    raw_file = path.read_bytes()
    outer = json.loads(_decompress(raw_file))
    if not isinstance(outer, dict) or not isinstance(outer.get("m_Dict"), dict):
        raise ValueError("decoded, but there is no m_Dict scene table, so this is not a TLD save")
    scenes = outer["m_Dict"]
    zone_names = [name for name in scenes
                  if name not in SKIP_SCENES and name != "global" and _is_outdoor_zone(name)]
    contents = SaveContents(
        records=[],
        header={key: value for key, value in outer.items() if key != "m_Dict"},
        failed_scenes=[],
    )

    for scene_name, b64_val in scenes.items():
        if scene_name in SKIP_SCENES or not isinstance(b64_val, str):
            continue
        try:
            scene = _decode_scene_blob(b64_val)
        except ValueError as e:
            print(f"  [warn] could not decode scene '{scene_name}': {e}", file=sys.stderr)
            contents.failed_scenes.append(scene_name)
            continue
        if not isinstance(scene, dict):
            continue

        if scene_name == "global":
            contents.records.extend(extract_player_inventory(scene))
        else:
            region = region_for_scene(scene_name, zone_names)
            contents.records.extend(extract_scene(scene_name, scene, region, include_unsearched))

    return contents


def load_save(path: Path) -> list:
    return read_save(path).records


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

HEADERS = ["region", "location", "location_type", "container",
           "category", "subcategory", "item",
           "count", "condition_pct", "x", "y", "z", "prefab", "source"]


def print_table(records: list) -> None:
    widths = {h: len(h) for h in HEADERS}
    rows = []
    for r in records:
        row = [
            r.region, r.location, r.location_type, r.container_prefab,
            r.category, r.subcategory, r.item,
            str(r.count),
            f"{r.condition_pct:.1f}%" if r.condition_pct is not None else "",
            str(r.x) if r.x is not None else "",
            str(r.y) if r.y is not None else "",
            str(r.z) if r.z is not None else "",
            r.prefab, r.source,
        ]
        rows.append(row)
        for h, v in zip(HEADERS, row):
            widths[h] = max(widths[h], len(v))

    sep = "  "
    header_line = sep.join(h.ljust(widths[h]) for h in HEADERS)
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(sep.join(v.ljust(widths[h]) for h, v in zip(HEADERS, row)))


DEFAULT_REPORT_FOLDER = "TLD-save-reports"


def default_csv_path(save_file: Path) -> Path:
    """Where --csv writes when --out is not given: TLD-save-reports in the home folder.

    The folder is outside the game's save folder on purpose. Steam Cloud syncs that folder,
    and a report next to a save is clutter there. The CSV is named after the whole file
    name, so a save and a backup of it do not share a report.
    """
    return Path.home() / DEFAULT_REPORT_FOLDER / f"{save_file.name}.csv"


def write_csv(records: list, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for r in records:
            writer.writerow([
                r.region, r.location, r.location_type, r.container_prefab,
                r.category, r.subcategory, r.item,
                r.count,
                round(r.condition_pct, 1) if r.condition_pct is not None else "",
                r.x if r.x is not None else "",
                r.y if r.y is not None else "",
                r.z if r.z is not None else "",
                r.prefab, r.source,
            ])
    print(f"Wrote {len(records)} rows to {path}")


def print_summary(records: list) -> None:
    total_units = sum(r.count for r in records)
    print(f"\n{'='*65}")
    print(f" SAVE SUMMARY")
    print(f"{'='*65}")
    print(f" Total stacks: {len(records):>6}    Total units: {total_units:>6}")
    print(f"{'='*65}\n")

    by_location: dict = defaultdict(list)
    for r in records:
        key = f"{r.region}  /  {r.location}" if r.region else r.location
        by_location[key].append(r)

    print("BY LOCATION:")
    print(f"  {'Location':<60} {'Stacks':>6}  {'Units':>6}")
    print(f"  {'-'*60} {'------':>6}  {'------':>6}")
    for loc in sorted(by_location):
        recs = by_location[loc]
        print(f"  {loc:<60} {len(recs):>6}  {sum(r.count for r in recs):>6}")

    item_counts: Counter = Counter()
    for r in records:
        item_counts[r.item] += r.count

    print("\nTOP 30 ITEMS (total units across all locations):")
    print(f"  {'Item':<45} {'Count':>7}")
    print(f"  {'-'*45} {'-------':>7}")
    for item, count in item_counts.most_common(30):
        print(f"  {item:<45} {count:>7}")


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def _interactive_args():
    print("=" * 65)
    print(" TLD Save Reader — Interactive Mode")
    print("=" * 65)
    default_save_dir = Path.home() / "AppData" / "Local" / "Hinterland" / "TheLongDark" / "Survival"

    while True:
        prompt = f"Save file path [{default_save_dir}]: " if default_save_dir.exists() else "Save file path: "
        raw = input(prompt).strip().strip('"')
        if not raw and default_save_dir.exists():
            files = [f for f in default_save_dir.iterdir()
                     if f.is_file() and f.suffix != ".backup"]
            if files:
                from datetime import datetime
                files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
                print("\nFound save files (most recent first):")
                for i, f in enumerate(files, 1):
                    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    print(f"  [{i}] {f.name:<30}  {mtime}")
                choice = input("Enter number or full path: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(files):
                    save_path = files[int(choice) - 1]
                else:
                    save_path = Path(choice.strip('"'))
            else:
                print("No save files found in default directory.")
                continue
        else:
            save_path = Path(raw)

        if save_path.exists():
            break
        print(f"  File not found: {save_path}\n")

    print("\nOutput options:")
    print("  [1] Summary only (default)")
    print("  [2] Summary + CSV export")
    print("  [3] Filter to a specific location + CSV")
    mode = input("Choice [1]: ").strip() or "1"

    scene_filter = None
    do_csv = False
    if mode == "2":
        do_csv = True
    elif mode == "3":
        scene_filter = input("Location filter (e.g. CampOffice, LakeRegion, Dam): ").strip()
        do_csv = True

    import types
    return types.SimpleNamespace(
        save_file=save_path, csv=do_csv, out=None,
        scene=scene_filter, summary=(mode == "1"),
        include_unsearched=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract items from a TLD v2.44+ save file. No dependencies required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tld_save_reader.py sandbox1 --summary
  python tld_save_reader.py sandbox1 --csv
  python tld_save_reader.py sandbox1 --out items.csv
  python tld_save_reader.py sandbox1 --scene CampOffice
  python tld_save_reader.py sandbox1 --scene LakeRegion --csv

The CSV goes to TLD-save-reports in your home folder, named after the save
(sandbox1.csv), unless --out names another file.
        """,
    )
    parser.add_argument("save_file", type=Path, nargs="?")
    parser.add_argument("--csv", action="store_true", help="Write full item list to CSV")
    parser.add_argument("--out", type=Path,
                        help="CSV file to write (default: see the note below)")
    parser.add_argument("--scene", help="Filter to scenes containing this string")
    parser.add_argument("--summary", action="store_true", help="Print summary stats only")
    parser.add_argument("--include-unsearched", action="store_true",
                        help="Also list the loot waiting in containers that have not been searched (spoilers)")
    args = parser.parse_args()

    if args.save_file is None:
        args = _interactive_args()

    if not args.save_file.exists():
        sys.exit(f"File not found: {args.save_file}")

    print(f"Loading {args.save_file}...", file=sys.stderr)
    try:
        records = read_save(args.save_file, args.include_unsearched).records
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read {args.save_file}: {e}")
    print(f"Extracted {len(records)} item stacks.", file=sys.stderr)

    unknown = {r.prefab for r in records if r.category == "Unknown"}
    if unknown:
        print(f"{len(unknown)} prefab name(s) are not in tld_taxonomy.py. "
              f"Run tld_prefab_diff.py on this save to list them.", file=sys.stderr)

    if args.scene:
        records = [r for r in records if args.scene.lower() in r.scene.lower()]
        print(f"Filtered to '{args.scene}': {len(records)} records.", file=sys.stderr)

    if args.csv or args.out:
        if args.out:
            out_path = args.out
        else:
            try:
                out_path = default_csv_path(args.save_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
            except (OSError, RuntimeError) as error:
                sys.exit(f"Could not use the default CSV folder: {error}. "
                         f"Use --out FILE to choose a place.")
        try:
            write_csv(records, out_path)
        except OSError as error:
            sys.exit(f"Could not write {out_path}: {error}")
    else:
        print_summary(records)
        if not args.summary:
            print(f"\nRe-run with --csv to export the full item list.")


if __name__ == "__main__":
    main()
