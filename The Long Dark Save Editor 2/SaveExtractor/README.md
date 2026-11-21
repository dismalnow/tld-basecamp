# SaveExtractor

Python tools for The Long Dark survival saves (game v2.44 and later). No tool here changes
a save file: the readers only read, and the editor writes its result to a new file.
Standard library only, Python 3.9 or newer.

| File | What it does |
|---|---|
| `tld_save_reader.py` | Lists every item in a save, as a summary or a CSV |
| `tld_taxonomy.py` | The item table: category for each `GEAR_` prefab, and where the prefab comes from |
| `tld_prefab_diff.py` | Checks the item table against saves and the game's own item catalogs |
| `tld_save_codec.py` | Decodes a save and encodes it again, exactly. Its `verify` command checks that a save rebuilds byte for byte |
| `test_tld_save_codec.py` | Tests for `tld_save_codec.py` |
| `tld_save_edit.py` | Changes one value in a save and writes the result to a new file. For now: the count of an inventory stack |
| `test_tld_save_edit.py` | Tests for `tld_save_edit.py` |

The wiki helper lives in `..\tld_mediawiki_api\tld_wiki_updater.py`.

Run the commands below from this folder. From anywhere else, put this folder's full
path in front of the script name, or Python will not find the script.

## Read a save

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox1"
python tld_save_reader.py $save --summary
python tld_save_reader.py $save --out items.csv
python tld_save_reader.py $save --out items.csv --include-unsearched
```

`--csv` without `--out` writes `<save>.csv` next to the save, which is inside the game's
save folder. Use `--out` to write somewhere else.

`--include-unsearched` adds the loot waiting in containers you have not opened yet. That
is a spoiler, so it is off by default.

### CSV columns

| Column | Meaning |
|---|---|
| `region`, `location` | Where the item is. `region` is blank when it cannot be determined |
| `location_type` | `player_inventory`, `floor`, `scripted` (placed by the game, not interactive yet), `container`, `container_unsearched` |
| `container` | Container name, for items in containers |
| `category`, `subcategory` | From `tld_taxonomy.py`. `Unknown` means the prefab is not in the table |
| `item`, `prefab` | Readable name and the `GEAR_` prefab name |
| `count`, `condition_pct` | Stack size and condition. Both are unknown for unsearched loot |
| `x`, `y`, `z` | Position in the scene |
| `source` | `base`, `DLC01` (Tales from the Far Territory), `DLC02` (Iron Road), `mod:<pack>`, or `legacy` |

## Check that a save can be rebuilt exactly

`tld_save_codec.py` decodes every layer of a save, encodes it again, and compares the
result with the original file, byte for byte. It is the safety check for any future tool
that changes a save: an edit can only be trusted if an unedited save comes back identical.

```powershell
python tld_save_codec.py verify "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
python tld_save_codec.py verify "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox1"
```

Given a folder, it checks the save files directly inside it (`sandbox1`, `challenge3`,
`ep1sandbox2` and so on) and ignores everything else. Allow about a second per save.

```text
sandbox1     1,096,887 bytes   20 entries    7,400 documents  IDENTICAL
sandbox15      180,665 bytes    6 entries    2,786 documents  IDENTICAL
2 of 2 saves rebuild byte for byte.
```

| Result | Meaning |
|---|---|
| `IDENTICAL` | The rebuilt save is byte for byte the original |
| `FAILED` | It is not. The next line says why |

Exit code 0 means every save was identical, 1 means at least one failed, and 2 means a
path was wrong.

Two things make an exact rebuild possible:

- Numbers are kept exactly as the game wrote them. The game writes forms such as
  `6.204471E-05`, which Python's `json` module would write back as `6.204471e-05`.
- The compressor is a port of `Helpers\CLZF.cs` in this repository. It produces the same
  bytes as the game's own compressor.

A document that cannot be written back identically is refused. Nothing is changed silently.

Checked on 2026-11-20: 15 of 15 real saves rebuild byte for byte. They span game
changelists 157357 to 189457 (v2.60).

## Change the count of a stack

`tld_save_edit.py` changes one value in a save and writes the result to a new file. The
save you name is only read. For now it does one thing: it sets the count of a stack in the
player's inventory.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox15"
python tld_save_edit.py stacks $save
python tld_save_edit.py set-count $save --item GEAR_WoodMatches --count 40 --out "$env:TEMP\sandbox15.edited"
```

`stacks` lists every stack with its entry number, item name, count and condition. When the
inventory holds more than one stack of an item, add `--entry` with the number from that list.

```text
Save:     sandbox15, 511,102 bytes, passes the exact rebuild check
Item:     GEAR_WoodMatches, inventory entry 3
Change:   count 23 -> 40
Checked:  the edited save differs from the original in exactly this one value
Checked:  6 of 7 entries are byte for byte unchanged (all except global)
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 511,094 bytes
SHA-256:  dd9ac33a4768c7e57526981128d245876b97c279078c0b0f4be79f1f3aacb31d
sandbox15 itself was not changed.
```

Three checks stand between an edit and a written file. If one fails, nothing is left on disk.

1. The original must pass `tld_save_codec.py verify`.
2. The edited save, decoded again, must differ from the original in exactly the value asked
   for, and every other entry must be byte for byte unchanged.
3. The new file is read back and compared with what was meant to be written.

The new file must not exist yet, and it must not be named like a live save (`sandbox1`,
`challenge3` and so on). The tool never overwrites anything. Putting the new file in place
of the original is a separate step that you do yourself, with the game closed and after a
backup.

Exit code 0 means done, 1 means refused with the reason printed, and 2 means the save was
not found or an option was wrong.

Two things to know about this edit:

- An item's stored weight does not change with its count (checked across 476 stacks in 16
  saves), so nothing else in the item record needs updating.
- The C# editor sets the save's "cheats used" flag whenever it saves. This tool does not.
  It changes only the value you ask for.

Checked on 2026-11-21: one count changed in each of 16 real saves, and the C# editor's own
loader read every result back with exactly that one difference.

## Keep the item table current

Run this after a game update or after adding an item mod. It exits with 0 when every
prefab is classified and lists the missing names otherwise.

```powershell
$game = "C:\Program Files (x86)\Steam\steamapps\common\TheLongDark"   # your game folder
python tld_prefab_diff.py `
  --catalog "$game\tld_Data\StreamingAssets\aa\catalog.json" `
  --catalog "$game\tld_dlc\TFTFT\DLC01.json" `
  --catalog "$game\tld_dlc\IronRoad\DLC02.json"

python tld_prefab_diff.py (Get-ChildItem "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox*" -Exclude *.csv).FullName
```

The catalogs are the authoritative list of game items. Saves add the items that mods
define. Add each missing name to `TAXONOMY_LOOKUP`, and to `DLC_PREFABS` or `MOD_PREFABS`
when it is not a base game item.

## Where the item table comes from

Checked on 2026-11-16 against game v2.60 (changelist 189457):

- the game's addressables catalogs: 710 gear prefabs (438 base, 246 DLC01, 26 DLC02)
- `Helpers\ItemDictionary.cs` in this repository: 473 names, current to game v2.27
- 15 real saves, one of them a developer-console `add_all_gear` dump
- five ModComponent packs: FoodPackByTKG, RetroFood, ItemPiles, MoreWood, LitharsRidge

The categories are a grouping for reports. They are not the game's inventory categories.

## Run the tests

```powershell
python -m unittest -v
```

The tests build their own small saves, so they need no game files. To check your real
saves as well, point `TLD_SAVE_DIR` at their folder first:

```powershell
$env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
python -m unittest -v
```

With `TLD_SAVE_DIR` set, the edit tests also change one count in each real save. That
happens in memory only: no file is written. Allow a minute or two for the whole run.

The expected compressor output in the tests comes from the C# compressor in this
repository, `Helpers\CLZF.cs`.

## Known gaps

- `region` stays blank for the two mine transition zones and for reused interiors
  (such as `TrailerC`) in a save that has visited more than one region.
- Water is reported as one stack. The volume is not read.
- Saves from before 2018 use a different format and are not supported.
- No save checked so far contains text outside plain ASCII. If the game writes such text
  in a form `tld_save_codec.py` does not reproduce, `verify` reports `FAILED` for that save.
