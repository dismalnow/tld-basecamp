# SaveExtractor

Python tools for The Long Dark survival saves (game v2.44 and later). One tool here changes
a save file, `tld_save_apply.py`, and only after a checked backup. The readers only read,
and the editor writes its result to a new file. Python 3.9 or newer. Standard library only,
except the wiki helper, which needs the `requests` package.

| File | What it does |
|---|---|
| `tld_save_reader.py` | Lists every item in a save, as a summary or a CSV |
| `test_tld_save_reader.py` | Tests for `tld_save_reader.py`: where it writes its CSV, its region labels, and how it decompresses |
| `tld_taxonomy.py` | The item table: category for each `GEAR_` prefab, and where the prefab comes from |
| `tld_prefab_diff.py` | Checks the item table against saves and the game's own item catalogs |
| `tld_wiki_updater.py` | Lists items on the game's wiki that the item table does not cover. Needs internet access and the `requests` package |
| `test_tld_wiki_updater.py` | Tests for `tld_wiki_updater.py`. They need the `requests` package and never reach the wiki |
| `missing_prefabs.json` | The wiki helper's last report, written by its `--patch` option |
| `tld_save_codec.py` | Decodes a save and encodes it again, exactly. Its `verify` command checks that a save rebuilds byte for byte |
| `test_tld_save_codec.py` | Tests for `tld_save_codec.py` |
| `tld_save_edit.py` | Changes one value in a save and writes the result to a new file: the count of an inventory stack, or the condition of an inventory item |
| `test_tld_save_edit.py` | Tests for `tld_save_edit.py` |
| `tld_save_apply.py` | Puts an edited file in place of the live save after a checked backup, and puts a backup back |
| `test_tld_save_apply.py` | Tests for `tld_save_apply.py` |

Run the commands below from this folder. From anywhere else, put this folder's full
path in front of the script name, or Python will not find the script.

## Read a save

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox1"
python tld_save_reader.py $save --summary
python tld_save_reader.py $save --csv
python tld_save_reader.py $save --out items.csv
python tld_save_reader.py $save --out items.csv --include-unsearched
```

`--csv` writes the item list to `TLD-save-reports` in your home folder, in a file named
after the save: `C:\Users\you\TLD-save-reports\sandbox1.csv`. The folder is made when it
is missing, and a new run replaces the earlier report for that save. `--out` writes the
CSV to the file you name. Nothing is written to the game's save folder unless you point
`--out` there.

`--include-unsearched` adds the loot waiting in containers you have not opened yet. That
is a spoiler, so it is off by default.

### CSV columns

| Column | Meaning |
|---|---|
| `region`, `location` | Where the item is. `region` is blank when it cannot be determined. Region names are the game's own, so the region around the town of Milton is `Mountain Town` |
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
  bytes as the game's own compressor. The license notice of that file is reproduced in
  `tld_save_codec.py`, next to the port.

A document that cannot be written back identically is refused. Nothing is changed silently.

Checked on 2026-11-20: 15 of 15 real saves rebuild byte for byte. They span game
changelists 157357 to 189457 (v2.60).

## Change a count or a condition

`tld_save_edit.py` changes one value in a save and writes the result to a new file. The
save you name is only read. It can set the count of a stack in the player's inventory, or
the condition of an item in the player's inventory.

### Count

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

### Condition

```powershell
python tld_save_edit.py items $save
python tld_save_edit.py set-condition $save --item GEAR_WoolSocks --condition 100 --out "$env:TEMP\sandbox15.edited"
```

`items` lists everything in the inventory, stacks or not, with the same columns as
`stacks`. The condition is in percent, from 1 to 100, and may have decimals (`62.5`). For
a stack it applies to the whole stack. When the inventory holds more than one of an item,
add `--entry` with the number from that list.

```text
Save:     sandbox15, 587,364 bytes, passes the exact rebuild check
Item:     GEAR_WoolSocks, inventory entry 15
Change:   condition 70.0% -> 100.0% (stored as 0.699579835 -> 1.0)
Checked:  the edited save differs from the original in exactly this one value
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 587,355 bytes
SHA-256:  9951f85a39865fa1c54bf244eb8bb80973c2140d7885a9b610860cc39bc1eaca
sandbox15 itself was not changed.
```

A ruined item (condition 0) is refused: the game keeps more than the condition for it.

### The checks

Three checks stand between an edit and a written file. If one fails, nothing is left on disk.

1. The original must pass `tld_save_codec.py verify`.
2. The edited save, decoded again, must differ from the original in exactly the value asked
   for, and every other entry must be byte for byte unchanged.
3. The new file is read back and compared with what was meant to be written.

The new file must not exist yet, and it must not be named like a live save (`sandbox1`,
`challenge3` and so on). The tool never overwrites anything. Putting the new file in place
of the original is a separate step, done by `tld_save_apply.py`: see the next section.

Exit code 0 means done, 1 means refused with the reason printed, and 2 means the save was
not found or an option was wrong.

Things to know about these edits:

- An item's stored weight does not change with its count (checked across 476 stacks in 16
  saves), so nothing else in the item record needs updating.
- The condition is the fraction in `m_NormalizedCondition`, written the way the game
  writes a 32-bit float (checked against all 1,224 inventory items in 15 saves). The item
  record also holds hit points in `m_CurrentHPProxy`. This tool leaves that value alone,
  as the C# editor's repair does. The game reads the condition when it loads a save and
  works the hit points out again: in a save the C# editor wrote on 2026-11-09, eleven
  items had the two values disagreeing. The game's next save held ten of them, each
  with its condition kept and its hit points worked out again.
- The C# editor sets the save's "cheats used" flag whenever it saves. This tool does not.
  It changes only the value you ask for.

Checked on 2026-11-21: one count changed in each of 16 real saves, and the C# editor's own
loader read every result back with exactly that one difference.

Checked in the game (v2.60, changelist 189457) on a survival save:

- 2026-11-21: Wood Matches set from 23 to 40. The game showed 40, and its own next save
  still held 40.
- 2026-11-22: Wool Socks set from 70% to 100%. The game showed 100%, and its own next
  save held 100% with the hit points worked out again, from 52.5 to 75.0.

Both of those saves, as the game wrote them, pass `verify`.

## Put an edited save in place, or put a backup back

`tld_save_apply.py` is the only tool here that changes a save file. `apply` puts a file
that `tld_save_edit.py` wrote in place of the live save, after copying the live save to a
backup folder. `restore` puts a backup back. Close the game first.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox15"
python tld_save_edit.py set-count $save --item GEAR_WoodMatches --count 50 --out "$env:TEMP\sandbox15.edited"
python tld_save_apply.py apply $save --from "$env:TEMP\sandbox15.edited"
```

```text
Save:       sandbox15, 585,798 bytes, last saved by the game at 2026-11-24 04:49:13
Edited:     C:\Users\you\AppData\Local\Temp\sandbox15.edited, 585,798 bytes
Changes:    1 value differs
              m_Dict > global > m_Inventory_Serialized > m_SerializedItems[3] > m_SerializedGear > m_StackableItemSerialized > m_UnitsProxy: 40 -> 50
Checked:    the game is not running
Checked:    both files pass the exact rebuild check
Checked:    the edited file was made from the save as it is now
Backup:     C:\Users\you\TLD-save-backups\sandbox15.backup-20261124-051500, read back and identical
Replaced:   sandbox15 now holds the edited save, read back and identical
To undo:    python tld_save_apply.py restore "C:\Users\you\AppData\Local\Hinterland\TheLongDark\Survival\sandbox15" --from "C:\Users\you\TLD-save-backups\sandbox15.backup-20261124-051500"
```

`apply` refuses, and changes nothing, unless all of this holds:

1. The game is not running. The tool looks for a running program named `tld` (`tld.exe`
   on Windows).
2. The live save and the edited file both pass `tld_save_codec.py verify`.
3. The edited file was made from the live save as it is now. The game stamps the time on a
   save whenever it writes it, so the tool can tell when the game has saved since the edit
   was made. It then refuses, because putting the file in place would undo what the game
   saved. Make the edit again from the current save.

Then it works in this order and stops at the first step that fails:

4. The live save is copied to the backup folder under a new name, and the copy is read back.
5. The edited content is put in place of the live save in one step, and the save is read
   back.

To see the backups of a save and put one back:

```powershell
python tld_save_apply.py backups $save
python tld_save_apply.py restore $save --from "$HOME\TLD-save-backups\sandbox15.backup-20261124-051500"
```

`restore` first backs up whatever the live save holds at that moment, so a restore can be
undone as well. It also works when the live save is damaged or missing. A file is only ever
put in place of the save it came from.

Backups go to `TLD-save-backups` in your home folder, or to the folder you name with
`--backup-dir`. A backup is named after the save, the date and the time, for example
`sandbox15.backup-20261124-051500`. The folder must be outside the game's save folder,
because Steam Cloud syncs everything under `Hinterland\TheLongDark`. This tool never
deletes or overwrites a backup.

`--dry-run` runs every check, prints what would happen, and changes nothing.

Exit code 0 means done, 1 means refused or stopped with the reason printed, and 2 means a
file was not found or an option was wrong.

Checked in the game (v2.60, changelist 189457) on 2026-11-24, on a survival save:

- `apply` put an edit of Wood Matches from 40 to 50 in place. The game showed 50.
- With the game running, `restore` refused and changed nothing.
- With the game closed, `restore` backed up the game's newer save and put the earlier one
  back. The game showed 40, and the save file was byte for byte the one from before the
  test.

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

### Compare with the wiki

`tld_wiki_updater.py` fetches the item lists from the game's Fandom wiki and reports the
wiki items that the item table does not cover. It needs internet access and the
`requests` package (`pip install requests`). It does not change `tld_taxonomy.py`.

```powershell
python tld_wiki_updater.py
python tld_wiki_updater.py --save "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox1"
python tld_wiki_updater.py --patch
```

`--save` also lists the prefabs in that save that the item table lacks. `--patch` writes
the report to `missing_prefabs.json` in this folder. When a wiki category cannot be
fetched, the run exits with 1 and leaves `missing_prefabs.json` as it is.

The wiki ranks last as a source of item names, after the game's catalogs, real saves and
`ItemDictionary.cs`. Wiki pages use display names, so check a new name against a real save
before adding it to the helper's name table.

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

With `TLD_SAVE_DIR` set, the edit tests also change one count and one condition in each
real save. That happens in memory only: no file is written. The apply tests copy each real
save into a temporary folder and work on the copy. The reader tests write each real save's
CSV to a temporary folder. Your save folder is only read. Allow about five minutes for the
whole run.

The expected compressor output in the tests comes from the C# compressor in this
repository, `Helpers\CLZF.cs`.

The tests for the wiki helper need the `requests` package, as the helper does. Without it
they are skipped, and the run still ends with `OK`. They never reach the wiki: a stand-in
answers in its place, and the report goes to a temporary folder.

GitHub runs the same tests on every pull request and on every push to `master`, on
Windows and Linux with Python 3.9 through 3.14. The workflow is
`.github/workflows/tests.yml` at the top of the repository. It installs `requests` for
the wiki helper's tests. It has no real saves, so the checks that need `TLD_SAVE_DIR` are
skipped there.

## Known gaps

- `region` stays blank for the two mine transition zones and for reused interiors
  (such as `TrailerC`) in a save that has visited more than one region.
- Water is reported as one stack. The volume is not read.
- Saves from before 2018 use a different format and are not supported.
- No save checked so far contains text outside plain ASCII. If the game writes such text
  in a form `tld_save_codec.py` does not reproduce, `verify` reports `FAILED` for that save.
- `tld_save_apply.py` knows the game by its Windows program name, `tld.exe`. On other
  systems it looks for a program named `tld`, and that has not been checked against the game.
