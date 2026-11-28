r"""Read the item names from an installed copy of The Long Dark, and compare them with tld_taxonomy.py.

Usage:
    python tld_game_names.py check [--game-dir FOLDER]
    python tld_game_names.py export OUT_FILE [--game-dir FOLDER]

check    reads the names from the game's files and compares them with GAME_NAMES in
         tld_taxonomy.py. It lists every prefab whose name is new, different or gone.
         The exit value is 0 when nothing differs and 1 when something does.
export   writes what it read to OUT_FILE as JSON: the game version, one record per prefab
         (name, key of the name, item record, condition group, weight), the table of
         condition steps, and what could not be read.

FOLDER is the folder the game is installed in, the one that holds tld.exe. Without
--game-dir it comes from the environment variable TLD_GAME_DIR. This tool only reads there.

This is the one tool in this folder that needs UnityPy, a package that reads Unity's asset
files. Install it with:  python -m pip install UnityPy
No other tool here needs it. Run this tool after a game update, to see which names changed.

How the game stores a name
--------------------------
1. The Addressables catalogs say which bundle file holds which asset: catalog.json for the
   base game, and one .json file for each DLC in a folder of its own under tld_dlc. A .json
   file there that is not a catalog is passed over. The first line of the output names the
   parts that were read.
2. A gear prefab such as GEAR_Hatchet carries a GearItem component. Its field
   m_GearItemData points at an item record, a GearItemData asset.
3. The record holds the key of the name (m_LocalizedName.m_LocalizationID, for example
   GAMEPLAY_Hatchet), the condition group (m_ConditionType) and the weight
   (m_BaseWeight.m_Units, in billionths of a kilogram).
4. The text for the key is in the string table "English" of the bundle
   tld_Data/StreamingAssets/Bundles/Windows/ui/localization.

The record also points back at a prefab (m_PrefabReference). That link is wrong for some
records in the game's own data, so this tool goes from the prefab to the record only.

A key is matched without regard to upper and lower case. Three film boxes write their key
in another case than the table does (GAMEPLAY_FilmBoxBW in the record, GAMEPLAY_FilmboxBW in
the table), and the game's wiki, which is written from the screen, calls them by the table's
text. The table also holds a few pairs of keys that differ in case only. When the key of an
item's name is one of those, the item is listed under "ambiguous" and gets no name.

The word that the game puts in front of a name (New, Fresh, Perfect) comes from the table of
condition steps: the group of the item picks a list of steps, the condition picks the step,
and the title is the text of the key NAMEKEY_Step, such as GAMEPLAY_BeefJerky_Fresh.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import tld_taxonomy as taxonomy

ENV_GAME_DIR = "TLD_GAME_DIR"
BASE_CATALOG = Path("tld_Data", "StreamingAssets", "aa", "catalog.json")
BASE_BUNDLES = Path("tld_Data", "StreamingAssets", "aa", "StandaloneWindows64")
DLC_FOLDER = Path("tld_dlc")
STRINGS_BUNDLE = Path("tld_Data", "StreamingAssets", "Bundles", "Windows", "ui", "localization")
VERSION_FILE = Path("tld_Data", "StreamingAssets", "version.txt")
STRING_TABLE = "English"
RECORD_PREFIX = "GearItemData_"
PREFAB_PREFIX = "gear_"
CONDITION_TABLE = "SCRIPT_ConditionTable"
INSTALL_HINT = "python -m pip install UnityPy"

Location = Tuple[str, int]      # (name of the serialized file inside a bundle, id of the object in it)


class GameFilesError(Exception):
    """The game folder, or a file in it, is not what this tool can read. The text is for the player."""


# ---------------------------------------------------------------------------
# The Addressables catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Catalog:
    ids: List[str]                              # every internal id: asset paths and bundle paths
    entries: List[Tuple[int, ...]]              # seven numbers per entry, the first is an index into ids
    buckets: List[Tuple[int, Tuple[int, ...]]]  # per key: where the key starts, and the entries it names


def _numbers(data: bytes, form: str, offset: int, what: str) -> Tuple[int, ...]:
    try:
        return struct.unpack_from(form, data, offset)
    except struct.error as error:
        raise GameFilesError(f"the catalog ends inside its {what}") from error


def parse_catalog(text: str, source: str) -> Catalog:
    """The parts of a catalog this tool uses. source names the file in messages."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise GameFilesError(f"{source} is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise GameFilesError(f"{source} is not an Addressables catalog")
    try:
        ids = document["m_InternalIds"]
        bucket_data = base64.b64decode(document["m_BucketDataString"], validate=True)
        entry_data = base64.b64decode(document["m_EntryDataString"], validate=True)
    except KeyError as error:
        raise GameFilesError(f"{source} is not an Addressables catalog: it has no {error.args[0]}") from error
    except (binascii.Error, TypeError, ValueError) as error:
        raise GameFilesError(f"{source} holds a table that is not base64: {error}") from error
    if not isinstance(ids, list) or not all(isinstance(name, str) for name in ids):
        raise GameFilesError(f"{source}: m_InternalIds is not a list of texts")
    try:
        count = _numbers(bucket_data, "<i", 0, "list of buckets")[0]
        position = 4
        buckets: List[Tuple[int, Tuple[int, ...]]] = []
        for _ in range(count):
            key_offset, length = _numbers(bucket_data, "<ii", position, "list of buckets")
            position += 8
            if length < 0:
                raise GameFilesError("a bucket has a length below zero")
            buckets.append((key_offset, _numbers(bucket_data, f"<{length}i", position, "list of buckets")))
            position += 4 * length
        entry_count = _numbers(entry_data, "<i", 0, "list of entries")[0]
        entries = [_numbers(entry_data, "<7i", 4 + 28 * index, "list of entries")
                   for index in range(entry_count)]
    except GameFilesError as error:
        raise GameFilesError(f"{source}: {error}") from error
    return Catalog(ids, entries, buckets)


def _item(items: Sequence[Any], index: int) -> Any:
    """items[index], and an IndexError for an index below zero too, which Python would count from the end."""
    if not 0 <= index < len(items):
        raise IndexError(index)
    return items[index]


def bundle_file(internal_id: str) -> str:
    """The file name at the end of a bundle's internal id, whichever slash the catalog uses."""
    return internal_id.replace("\\", "/").rsplit("/", 1)[-1]


def asset_holders(catalog: Catalog, source: str) -> Dict[str, str]:
    """Asset path -> file name of the bundle that holds it.

    An entry names a list of bundles the asset needs. The first of them is the one that holds it.
    """
    holders: Dict[str, str] = {}
    try:
        for internal, _provider, dependency, _hash, _data, _primary, _kind in catalog.entries:
            name = _item(catalog.ids, internal)
            if name.endswith(".bundle") or dependency < 0:
                continue
            locations = _item(catalog.buckets, dependency)[1]
            if locations:
                holder = _item(catalog.entries, locations[0])[0]
                holders[name] = bundle_file(_item(catalog.ids, holder))
    except IndexError as error:
        raise GameFilesError(f"{source}: an entry points outside the catalog's own lists") from error
    return holders


def _stem(asset_path: str) -> str:
    return asset_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def wanted_bundles(holders: Dict[str, str]) -> Dict[str, List[str]]:
    """The bundles to open, by what they are opened for: "prefabs", "records", "conditions"."""
    wanted: Dict[str, List[str]] = {"prefabs": [], "records": [], "conditions": []}
    for asset, bundle in holders.items():
        lowered, stem = asset.lower(), _stem(asset)
        if lowered.endswith(".prefab") and stem.lower().startswith(PREFAB_PREFIX):
            kind = "prefabs"
        elif lowered.endswith(".asset") and stem.startswith(RECORD_PREFIX):
            kind = "records"
        elif lowered.endswith(".asset") and stem == CONDITION_TABLE:
            kind = "conditions"
        else:
            continue
        if bundle not in wanted[kind]:
            wanted[kind].append(bundle)
    return {kind: sorted(bundles) for kind, bundles in wanted.items()}


# ---------------------------------------------------------------------------
# The game folder
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Part:
    name: str           # "base", or the name of a DLC's catalog file, such as DLC01
    catalog: Path
    bundles: Path       # the folder that holds this part's bundle files


def looks_like_catalog(path: Path) -> bool:
    """Whether a JSON file under tld_dlc is an Addressables catalog. Other JSON files there are passed over."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and "m_InternalIds" in document


def find_parts(game_dir: Path) -> List[Part]:
    """The base game, then every DLC that is installed, by name. Raises when FOLDER is not a game folder."""
    if not game_dir.is_dir():
        raise GameFilesError(f"{game_dir} is not a folder. Give the folder that holds tld.exe.")
    base = game_dir / BASE_CATALOG
    if not base.is_file():
        if (game_dir / BASE_CATALOG.parts[0]).is_dir():
            raise GameFilesError(f"{base} is not there. This tool reads the game's catalog in that file, so "
                                 "the game may have changed how it stores the catalog.")
        raise GameFilesError(f"{game_dir} does not look like the game's folder: {BASE_CATALOG} is not in it. "
                             "Give the folder that holds tld.exe.")
    parts = [Part("base", base, game_dir / BASE_BUNDLES)]
    dlc = game_dir / DLC_FOLDER
    if dlc.is_dir():
        found = [Part(catalog.stem, catalog, catalog.parent) for catalog in dlc.glob("*/*.json")
                 if looks_like_catalog(catalog)]
        parts.extend(sorted(found, key=lambda part: (part.name, str(part.catalog))))
    return parts


def game_version(game_dir: Path) -> str:
    """The text of the game's version file, or "" when it is not there."""
    path = game_dir / VERSION_FILE
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def load_unitypy() -> Any:
    """The UnityPy module. The tests put a stand-in here, so the rest of this file asks for no type."""
    try:
        import UnityPy
    except ImportError as error:
        raise GameFilesError("This tool needs the package UnityPy, which reads Unity's asset files, and it "
                             f"is not installed for this Python. Install it with:  {INSTALL_HINT}") from error
    return UnityPy


# ---------------------------------------------------------------------------
# What the bundles hold
# ---------------------------------------------------------------------------

@dataclass
class Findings:
    components: List[Tuple[str, Location]] = field(default_factory=list)   # (prefab name, record it names)
    records: Dict[Location, Dict[str, Any]] = field(default_factory=dict)
    conditions: List[List[Tuple[str, float]]] = field(default_factory=list)
    unreadable: int = 0                                                    # objects UnityPy could not read


def _file_name(assets_file: Any) -> str:
    return bundle_file(str(assets_file.name)).lower()


def _record_location(component_file: Any, reference: Dict[str, Any]) -> Optional[Location]:
    """Where the record is that a GearItem component names, or None when it names none."""
    path_id, file_id = reference.get("m_PathID"), reference.get("m_FileID", 0)
    if not isinstance(path_id, int) or not isinstance(file_id, int) or path_id == 0:
        return None
    if file_id == 0:
        return _file_name(component_file), path_id
    externals = component_file.externals
    if not 0 < file_id <= len(externals):
        return None
    return bundle_file(str(externals[file_id - 1].name)).lower(), path_id


def _record_fields(tree: Dict[str, Any]) -> Dict[str, Any]:
    name = tree.get("m_LocalizedName") or {}
    weight = tree.get("m_BaseWeight") or {}
    return {
        "data": tree.get("m_Name", ""),
        "name_key": name.get("m_LocalizationID", "") if isinstance(name, dict) else "",
        "condition_type": tree.get("m_ConditionType"),
        "weight_units": weight.get("m_Units") if isinstance(weight, dict) else None,
    }


def _condition_steps(tree: Dict[str, Any]) -> List[List[Tuple[str, float]]]:
    groups: List[List[Tuple[str, float]]] = []
    for entry in tree.get("m_Entries") or []:
        steps = entry.get("m_NameForConditionRangeList") or [] if isinstance(entry, dict) else []
        groups.append([(str(step.get("m_Suffix", "")), float(step.get("m_MinConditionForName", 0.0)))
                       for step in steps if isinstance(step, dict)])
    return groups


def scan_bundle(unitypy: Any, path: Path, findings: Findings) -> None:
    """Add what one bundle holds to the findings: GearItem components, item records, the condition table."""
    try:
        environment = unitypy.load(str(path))
    except Exception as error:  # noqa: BLE001 - UnityPy raises many kinds; the player needs the file's name
        raise GameFilesError(f"{path.name} could not be opened as a Unity bundle: {error}") from error
    names: Dict[Location, str] = {}
    behaviours = []
    for item in environment.objects:
        kind = item.type.name
        if kind == "GameObject":
            try:
                names[(_file_name(item.assets_file), item.path_id)] = item.peek_name() or ""
            except Exception:  # noqa: BLE001 - one object UnityPy cannot read must not end the whole survey
                findings.unreadable += 1
        elif kind == "MonoBehaviour":
            behaviours.append(item)
    for item in behaviours:
        try:
            tree = item.read_typetree()
        except Exception:  # noqa: BLE001 - one object UnityPy cannot read must not end the whole survey
            findings.unreadable += 1
            continue
        if not isinstance(tree, dict):
            continue
        own_name = tree.get("m_Name") or ""
        if own_name.startswith(RECORD_PREFIX):
            findings.records[(_file_name(item.assets_file), item.path_id)] = _record_fields(tree)
        elif own_name == CONDITION_TABLE:
            findings.conditions = _condition_steps(tree)
        elif isinstance(tree.get("m_GearItemData"), dict):
            owner = tree.get("m_GameObject") or {}
            prefab = names.get((_file_name(item.assets_file), owner.get("m_PathID", 0)), "")
            location = _record_location(item.assets_file, tree["m_GearItemData"])
            if prefab and location is not None:
                findings.components.append((prefab, location))


def read_strings(unitypy: Any, path: Path) -> Dict[str, str]:
    """The English string table: key as the table writes it -> text."""
    if not path.is_file():
        raise GameFilesError(f"the game's text bundle is not there: {path}")
    try:
        environment = unitypy.load(str(path))
    except Exception as error:  # noqa: BLE001 - UnityPy raises many kinds; the player needs the file's name
        raise GameFilesError(f"{path} could not be opened as a Unity bundle: {error}") from error
    for item in environment.objects:
        if item.type.name == "MonoBehaviour" and item.peek_name() == STRING_TABLE:
            try:
                tree = item.read_typetree()
            except Exception as error:  # noqa: BLE001 - as above
                raise GameFilesError(
                    f"the table {STRING_TABLE} in {path} could not be read: {error}") from error
            return strings_of(tree)
    raise GameFilesError(f"{path} holds no table named {STRING_TABLE}")


def strings_of(tree: Dict[str, Any]) -> Dict[str, str]:
    """Key -> first language of every entry of a string table."""
    strings: Dict[str, str] = {}
    for entry in tree.get("m_Entries") or []:
        if not isinstance(entry, dict):
            continue
        key, languages = entry.get("m_Key"), entry.get("m_Languages")
        if isinstance(key, str) and isinstance(languages, list) and languages and isinstance(languages[0], str):
            strings[key] = languages[0]
    return strings


# ---------------------------------------------------------------------------
# Joining prefab, record and text
# ---------------------------------------------------------------------------

def clean(name: str) -> str:
    """A name with runs of spaces made one space and the ends trimmed."""
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class NameTable:
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # prefab -> name, name_key, data, ...
    no_text: Dict[str, str] = field(default_factory=dict)            # prefab -> key that has no English text
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)    # prefab -> table keys that differ in case
    no_record: List[str] = field(default_factory=list)               # prefabs whose record was not found
    two_records: Dict[str, List[str]] = field(default_factory=dict)  # prefab -> records, when copies disagree

    def names(self) -> Dict[str, str]:
        return {prefab: item["name"] for prefab, item in self.items.items() if item["name"]}


def join(components: Iterable[Tuple[str, Location]], records: Dict[Location, Dict[str, Any]],
         strings: Dict[str, str]) -> NameTable:
    """One entry per prefab: its record, the key of its name and the English text for the key."""
    by_lower: Dict[str, List[str]] = {}
    for key in strings:
        by_lower.setdefault(key.lower(), []).append(key)
    table = NameTable()
    missing = set()
    for raw_name, location in components:
        prefab = taxonomy.normalize_prefab(raw_name.strip())
        record = records.get(location)
        if record is None:
            missing.add(prefab)
            continue
        if prefab in table.items:
            first = table.items[prefab]["data"]
            if first != record["data"]:
                table.two_records.setdefault(prefab, [first])
                if record["data"] not in table.two_records[prefab]:
                    table.two_records[prefab].append(record["data"])
            continue
        key = record["name_key"]
        matches = by_lower.get(key.lower(), []) if key else []
        texts = {clean(strings[match]) for match in matches}
        name = ""
        if len(texts) == 1:
            name = texts.pop()
        elif len(texts) > 1:
            table.ambiguous[prefab] = sorted(matches)
        if not name and prefab not in table.ambiguous:
            table.no_text[prefab] = key
        table.items[prefab] = {"name": name, "name_key": key, "data": record["data"],
                               "condition_type": record["condition_type"],
                               "weight_units": record["weight_units"]}
    table.no_record = sorted(missing - set(table.items))
    return table


def read_game(game_dir: Path, progress: bool = True) -> Tuple[NameTable, Findings, List[str]]:
    """Everything this tool reads from a game folder: the joined table, the raw findings, the parts read."""
    parts = find_parts(game_dir)
    unitypy = load_unitypy()
    folders = [part.bundles for part in parts]
    findings = Findings()
    opened = set()
    for part in parts:
        try:
            text = part.catalog.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise GameFilesError(f"{part.catalog} could not be read: {error}") from error
        wanted = wanted_bundles(asset_holders(parse_catalog(text, part.catalog.name), part.catalog.name))
        for bundle in sorted({name for names in wanted.values() for name in names}):
            if bundle in opened:
                continue
            path = next((folder / bundle for folder in [part.bundles] + folders if (folder / bundle).is_file()),
                        None)
            if path is None:
                raise GameFilesError(f"the catalog {part.catalog.name} names the bundle {bundle}, and it is "
                                     f"not in {part.bundles}. Is this part of the game fully installed?")
            if progress:
                print(f"  reading {part.name}: {bundle}", file=sys.stderr)
            scan_bundle(unitypy, path, findings)
            opened.add(bundle)
    if not findings.components or not findings.records:
        raise GameFilesError(f"the {len(opened)} bundles the catalogs name gave {len(findings.components)} "
                             f"gear prefabs and {len(findings.records)} item records, and UnityPy could not "
                             f"read {findings.unreadable} objects. The game's files may have changed in a way "
                             "this tool does not know, or UnityPy may be too old for this version of the game.")
    strings = read_strings(unitypy, game_dir / STRINGS_BUNDLE)
    return join(findings.components, findings.records, strings), findings, [part.name for part in parts]


# ---------------------------------------------------------------------------
# The two commands
# ---------------------------------------------------------------------------

def compare(game: Dict[str, str], table: Dict[str, str]) -> Dict[str, List[str]]:
    """Lines for the three kinds of difference between the game's names and GAME_NAMES."""
    new = [f"{prefab}: {game[prefab]!a}" for prefab in sorted(set(game) - set(table))]
    gone = [f"{prefab}: {table[prefab]!a}" for prefab in sorted(set(table) - set(game))]
    different = [f"{prefab}: {table[prefab]!a} -> {game[prefab]!a}"
                 for prefab in sorted(set(game) & set(table)) if game[prefab] != table[prefab]]
    return {"new": new, "different": different, "gone": gone}


def known_prefabs(names: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """The names of prefabs the item table knows, and the prefabs it does not know."""
    known = {prefab: name for prefab, name in names.items() if prefab in taxonomy.TAXONOMY_LOOKUP}
    return known, sorted(set(names) - set(known))


def command_check(game_dir: Path) -> int:
    table, findings, parts = read_game(game_dir)
    names, unknown = known_prefabs(table.names())
    version = game_version(game_dir)
    print(f"Game version: {version or 'not stated'}. Parts read: {', '.join(parts)}.")
    if not version.startswith(taxonomy.GAME_NAMES_VERSION):
        print(f"GAME_NAMES in tld_taxonomy.py was read from game version {taxonomy.GAME_NAMES_VERSION}, "
              "so differences below are to be expected.")
    print(f"Prefabs with an item record: {len(table.items)}. With an English name: {len(table.names())}.")
    differences = compare(names, taxonomy.GAME_NAMES)
    titles = {"new": "In the game and not in GAME_NAMES", "different": "Another name in the game",
              "gone": "In GAME_NAMES and not in the game"}
    for kind in ("new", "different", "gone"):
        if differences[kind]:
            print(f"{titles[kind]}: {len(differences[kind])}")
            for line in differences[kind]:
                print(f"  {line}")
    for title, lines in (("Prefabs the item table does not know", unknown),
                         ("Prefabs whose record was not found", table.no_record),
                         ("Prefabs whose key matches table keys with different texts",
                          [f"{prefab}: {', '.join(keys)}" for prefab, keys in sorted(table.ambiguous.items())]),
                         ("Prefabs whose copies name different records",
                          [f"{prefab}: {', '.join(found)}"
                           for prefab, found in sorted(table.two_records.items())])):
        if lines:
            print(f"{title}: {len(lines)}")
            for line in lines:
                print(f"  {line}")
    if findings.unreadable:
        print(f"Objects UnityPy could not read, and this tool passed over: {findings.unreadable}")
    total = sum(len(lines) for lines in differences.values())
    if total:
        print(f"{total} names differ from GAME_NAMES in tld_taxonomy.py.")
        return 1
    print(f"All {len(names)} names are the ones in GAME_NAMES in tld_taxonomy.py.")
    return 0


def command_export(game_dir: Path, target: Path) -> int:
    if target.exists() and target.is_dir():
        raise GameFilesError(f"{target} is a folder. Give the name of a file to write.")
    if game_dir.resolve() in target.resolve().parents:
        raise GameFilesError(f"{target} is inside the game folder. This tool writes nothing there.")
    table, findings, parts = read_game(game_dir)
    document = {
        "game_version": game_version(game_dir),
        "parts": parts,
        "items": {prefab: table.items[prefab] for prefab in sorted(table.items)},
        "condition_steps": [[{"step": step, "from": low} for step, low in group]
                            for group in findings.conditions],
        "no_english_text": dict(sorted(table.no_text.items())),
        "ambiguous_keys": dict(sorted(table.ambiguous.items())),
        "no_record": table.no_record,
        "two_records": dict(sorted(table.two_records.items())),
        "unreadable_objects": findings.unreadable,
    }
    try:
        target.write_text(json.dumps(document, indent=1, ensure_ascii=True) + "\n", encoding="ascii")
    except OSError as error:
        raise GameFilesError(f"{target} could not be written: {error}") from error
    print(f"Wrote {len(table.items)} items, {len(table.names())} of them with an English name, to {target}")
    return 0


def game_dir_of(option: Optional[Path]) -> Path:
    if option is not None:
        return option
    from_environment = os.environ.get(ENV_GAME_DIR, "").strip()
    if from_environment:
        return Path(from_environment)
    raise GameFilesError(f"Say where the game is installed: --game-dir FOLDER, or set {ENV_GAME_DIR}. "
                         "It is the folder that holds tld.exe.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read the item names from an installed copy of The Long Dark. Needs the package UnityPy.")
    commands = parser.add_subparsers(dest="command", required=True)
    folder_help = f"the folder that holds tld.exe. Default: the environment variable {ENV_GAME_DIR}"
    check = commands.add_parser("check", help="compare the game's names with GAME_NAMES in tld_taxonomy.py")
    check.add_argument("--game-dir", type=Path, metavar="FOLDER", help=folder_help)
    export = commands.add_parser("export", help="write what the game's files say to a JSON file")
    export.add_argument("target", type=Path, metavar="OUT_FILE", help="the JSON file to write")
    export.add_argument("--game-dir", type=Path, metavar="FOLDER", help=folder_help)
    return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
    options = build_parser().parse_args(arguments)
    try:
        game_dir = game_dir_of(options.game_dir)
        if options.command == "check":
            return command_check(game_dir)
        return command_export(game_dir, options.target)
    except GameFilesError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
