# SaveExtractor

Python tools for The Long Dark survival saves (game v2.44 and later). One tool here changes
a save file, `tld_save_apply.py`, and only after a checked backup. The readers only read,
and the editor writes its result to a new file. Python 3.9 or newer. Standard library only,
except the wiki helper, which needs the `requests` package.

| File | What it does |
|---|---|
| `tld_save_reader.py` | Lists every item in a save, as a summary or a CSV |
| `test_tld_save_reader.py` | Tests for `tld_save_reader.py`: where it writes its CSV, its region labels, how it decompresses, and the weight, liters and calories it reads |
| `tld_taxonomy.py` | The item table: category for each `GEAR_` prefab, where the prefab comes from, and the game's display name where a source states it |
| `test_tld_taxonomy.py` | Tests for the display names in `tld_taxonomy.py` |
| `tld_prefab_diff.py` | Checks the item table against saves and the game's own item catalogs |
| `tld_wiki_updater.py` | Lists items on the game's wiki that the item table does not cover. Needs internet access and the `requests` package |
| `test_tld_wiki_updater.py` | Tests for `tld_wiki_updater.py`. They need the `requests` package and never reach the wiki |
| `missing_prefabs.json` | The wiki helper's last report, written by its `--patch` option |
| `tld_save_codec.py` | Decodes a save and encodes it again, exactly. Its `verify` command checks that a save rebuilds byte for byte |
| `test_tld_save_codec.py` | Tests for `tld_save_codec.py` |
| `tld_save_edit.py` | Changes a save and writes the result to a new file. One value of one inventory item: its count, its condition, the liquid it holds, the calories left in it, or the rounds loaded in it. Or the condition of every inventory item at once. Or one item taken out of the inventory, or another one of an item added to it |
| `test_tld_save_edit.py` | Tests for `tld_save_edit.py` |
| `tld_save_apply.py` | Puts an edited file in place of the live save after a checked backup, and puts a backup back |
| `test_tld_save_apply.py` | Tests for `tld_save_apply.py` |
| `ROADMAP.md` | What gets built next, in what order, and where each piece stands |

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
| `region`, `location` | Where the item is. Region names are the game's own, so the region around the town of Milton is `Mountain Town`. A zone that joins two regions has no region in the game, so it shows its own name: `Ravine`, `Crumbling Highway`, `Cinder Hills Coal Mine`, `No. 3 Coal Mine`. A building that the game uses in several regions, such as a trailer, gets the region of the outdoor zone whose door leads to it. `region` is blank for what you carry, and when the region cannot be determined |
| `location_type` | `player_inventory`, `floor`, `scripted` (placed by the game, not interactive yet), `container`, `container_unsearched` |
| `container` | Container name, for items in containers |
| `category`, `subcategory` | From `tld_taxonomy.py`. `Unknown` means the prefab is not in the table |
| `item` | The name the game shows for the item, where the item table has it. Otherwise a name made from the prefab name: `Kerosene Lamp Spelunkers` for `GEAR_KeroseneLamp_Spelunkers` |
| `prefab` | The `GEAR_` prefab name, exactly as the save stores it. Two prefabs can share one display name, so this is the column that tells items apart |
| `count`, `condition_pct` | Stack size and condition. Both are unknown for unsearched loot |
| `x`, `y`, `z` | Position in the scene |
| `source` | `base`, `DLC01` (Tales from the Far Territory), `DLC02` (Iron Road), `mod:<pack>`, or `legacy` |
| `unit_weight_kg` | Weight of one unit in kilograms, as the save stores it. For a stack, multiply by `count`. Blank when the save stores no weight: water, other liquids, powders, papers and keys |
| `liters` | Liquid the item holds, in liters: the water you carry, a bottle or can, or the fuel in a lamp. Blank when the item holds none, or the game has not set the amount yet |
| `calories` | Calories left in one unit of a food item |

`unit_weight_kg` is the weight of a whole unit. The game keeps the same number for a food item
that is half eaten, and it does not include the liquid inside a container. So the sum of
these weights is not the carried weight that the game shows.

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

## Change one value in the inventory

`tld_save_edit.py` changes one value in a save and writes the result to a new file. The
save you name is only read. Each command works on one item in the player's inventory,
except `repair-all`, which works on all of them.

| Command | What it changes | Tested in the game |
|---|---|---|
| `set-count` | The count of a stack | Yes, on game v2.60 |
| `set-condition` | The condition of an item, or of a whole stack | Yes, on game v2.60 |
| `set-liters` | The liquid an item holds | Not yet. Its own output says so |
| `set-calories` | The calories left in a food item | Not yet. Its own output says so |
| `set-rounds` | The rounds loaded in a rifle, a revolver or a distress pistol | Not yet. Its own output says so |
| `repair-all` | The condition of every item below 100% | Not yet. Its own output says so |
| `remove-item` | One item, or one whole stack, leaves the inventory | Not yet. Its own output says so |
| `copy-item` | Another one of an item the inventory holds is added | Not yet. Its own output says so |

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

### Liters

Not tested in the game yet: nobody has loaded a save changed by `set-liters`. Try it on a
save you can afford to lose.

```powershell
python tld_save_edit.py liquids $save
python tld_save_edit.py set-liters $save --item GEAR_WaterSupplyPotable --liters 2.5 --out "$env:TEMP\sandbox15.edited"
```

`liquids` lists the inventory items that hold liquid: the water you carry, bottles and
cans, and the fuel in a lamp. An amount shown as `not set` is one the game has not filled
in yet. The amount is in liters, from 0 to 999, with up to nine decimal places. When the
inventory holds more than one of an item, add `--entry` with the number from that list.

```text
entry  item                     liters
    1  GEAR_WaterSupplyPotable  2
  180  GEAR_LampFuel            0.11630746
2 items that hold liquid among 443 inventory items in sandbox15.
```

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Item:     GEAR_WaterSupplyPotable, inventory entry 1
Change:   liters 2 -> 2.5 (stored as 2000000000 -> 2500000000)
Checked:  the edited save differs from the original in exactly this one value
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-liters has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 585,789 bytes
SHA-256:  7d13c4ced67c16f50fe5169e170cd7b7b4c7226058954afacabd8c5f9bb433f5
sandbox15 itself was not changed.
```

What the tool does not know: how much an item can hold. It will write 5 liters into a
half-liter bottle, and what the game makes of that is untested. An item whose record holds
no amount at all, such as unsafe water when you carry none, is refused, because setting it
would add a field and not change a value.

### Calories

Not tested in the game yet: nobody has loaded a save changed by `set-calories`. Try it on a
save you can afford to lose.

```powershell
python tld_save_edit.py foods $save
python tld_save_edit.py set-calories $save --item GEAR_Crackers --entry 11 --calories 300 --out "$env:TEMP\sandbox15.edited"
```

`foods` lists the inventory items with calories. `calories` is what is left, exactly as the
save holds it. `full` is the save's figure for an untouched item. The new amount runs from 1
to 9999 and may have decimals. When the inventory holds more than one of an item, add
`--entry` with the number from that list.

```text
entry  item                        count  calories  full
   11  GEAR_Crackers                         600.0  600.0
  148  GEAR_BirchbarkTea                     100.0  100.0
  150  GEAR_BurdockPrepared            1     275.0  275.0
  ...
67 items with calories among 443 inventory items in sandbox15.
```

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Item:     GEAR_Crackers, inventory entry 11
Change:   calories 600.0 -> 300.0
Checked:  the edited save differs from the original in exactly this one value
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-calories has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 585,804 bytes
SHA-256:  061fd11e19bbe0dffe2d834e020b403016678242b9af8b4def0b0ec16a41e32f
sandbox15 itself was not changed.
```

Things to know about calories:

- The amount is stored as a 32-bit float, the way the game writes it (checked against all
  176 food items in the inventories of 14 saves). A number with more digits than such a
  float holds is rounded, and the `Change` line shows what was written: `1234.56789` becomes
  `1234.56787`.
- For a stack, the save keeps one figure, the calories of one unit, and that is the figure
  that changes. The `Change` line then ends with `per unit, in a stack of 3`. In 14 real
  saves every stack of two or more holds untouched food (12 of 12), so what the game does
  with a stack of partly eaten food is unknown.
- `full` is not a limit. Two cooked fish in a real save hold more than that figure, and the
  tool writes more than it too.
- Only the calories change. The stored weight stays as it is, which is what the game does
  itself: in a real save, two partly eaten pieces of cooked moose have the same stored weight
  as the three whole ones next to them. Whether a package is open is left alone too.
- The lowest amount is 1, because the game removes an item with nothing left. Removing an
  item is a separate edit that is not built yet: see `ROADMAP.md`.

### Rounds

Not tested in the game yet: nobody has loaded a save changed by `set-rounds`. Try it on a
save you can afford to lose.

```powershell
$other = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox7"
python tld_save_edit.py weapons $other
python tld_save_edit.py set-rounds $other --item GEAR_Revolver --entry 67 --rounds 6 --out "$env:TEMP\sandbox7.edited"
```

`weapons` lists the inventory items that can hold rounds: the rounds loaded, the spent
casings still in the weapon, and how many rounds it holds at once. `set-rounds` takes the new
number of loaded rounds, from 0 to what the weapon holds. When the inventory holds more than
one of a weapon, add `--entry` with the number from that list.

```text
entry  item           rounds  spent casings  holds
   24  GEAR_Revolver       5              1      6
   67  GEAR_Revolver       2              0      6
2 items that can hold rounds among 69 inventory items in sandbox7.
```

```text
Save:     sandbox7, 623,218 bytes, passes the exact rebuild check
Item:     GEAR_Revolver, inventory entry 67
Change:   rounds 2 -> 6 (GEAR_Revolver holds 6)
Checked:  the edited save differs from the original in exactly this one value
Checked:  12 of 13 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-rounds has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox7.edited, 623,134 bytes
SHA-256:  2f822887de557676f2dfbd34951628b8d058ebf03442498f9614b7b268cced2c
sandbox7 itself was not changed.
```

Things to know about rounds:

- A save keeps the loaded rounds as a list, with one number for each round. All 39 loaded
  rounds in 14 real saves are `100`, so a round that is added is written as `100`. Rounds
  that are taken out come off the end of the list. The one value that changes is that list,
  and the tool checks every round in it after the edit.
- A save does not say how many rounds a weapon holds. The tool uses 10 for a rifle and 6 for
  a revolver, as the game's wiki states. Real saves agree: the one loaded rifle holds 10,
  and in the three revolvers with spent casings, rounds and casings add up to 6. The named
  rifles and revolvers (`GEAR_Rifle_Vaughns`, `GEAR_RevolverGreen` and so on) are taken to
  hold what the plain weapon holds. The tool uses 1 for the distress pistol. No save confirms
  that: all 12 distress pistols in the saves are empty.
- A revolver keeps its spent casings in the cylinder until it is reloaded. The tool leaves
  them alone, so the rounds must fit next to them: with one spent casing, at most 5. Reload
  the revolver in the game to drop the casings.
- The loose ammunition you carry does not change. Rounds are added to the weapon or taken
  out of it, and nothing else is touched. Whether the weapon is jammed is left alone too.
- The camera keeps its film in the same kind of record, so `weapons` lists it. All three
  cameras in the 14 saves are empty, so what a loaded camera looks like in a save is not
  known yet. `set-rounds` refuses the camera, and its line has nothing under `holds`.

### Repair everything

Not tested in the game yet: nobody has loaded a save changed by `repair-all`. Try it on a
save you can afford to lose.

```powershell
python tld_save_edit.py repair-all $save --out "$env:TEMP\sandbox15.edited"
```

`repair-all` sets the condition of every item in the inventory that is below 100% to 100%.
It takes no item name. For each item it follows the rule of `set-condition`, and it leaves
some items alone and counts them in its output.

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Change:   condition set to 100% for 107 of the 443 items in the inventory
Left:     336 at 100% already, 0 ruined, 0 burned torches or flares, 0 with a condition this tool does not change
Checked:  the edited save differs from the original in exactly these 107 values
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by repair-all has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 584,124 bytes
SHA-256:  e7671ec9ee53d914580856407e46484b5bbd29024503861a85021e962d2ab39e
sandbox15 itself was not changed.
```

Things to know about `repair-all`:

- It changes conditions and nothing else. The second check holds for all repaired items at
  once: the edited save must differ from the original in exactly those conditions, each from
  its old value to `1.0`, and in nothing else.
- A ruined item is left alone, as `set-condition` leaves it alone. So is an item whose
  condition is stored in a form the tool does not change.
- A torch or a flare that has burned is left alone too. The save keeps its burn time next to
  its condition, and in both burned torches in the real saves the condition is exactly what
  is left of 90 minutes: a torch that burned 53.6 minutes is at 40.5%. What the game makes of
  a full condition next to that burn time is not known. `set-condition` still changes such a
  torch when you name it, and that is not tested in the game either. The C# editor's repair
  sets every item to 100% and marks torches and flares as fresh.
- Hit points are left alone, as with `set-condition`. The game works them out again from the
  condition when it loads the save.
- In 14 real saves the inventories hold 1,143 items: 657 at 100%, 484 that a repair changes,
  and 2 burned torches. None is ruined.

### Remove an item

Not tested in the game yet: nobody has loaded a save changed by `remove-item`. Try it on a
save you can afford to lose.

```powershell
python tld_save_edit.py items $save
python tld_save_edit.py remove-item $save --item GEAR_Crackers --entry 11 --out "$env:TEMP\sandbox15.edited"
```

`remove-item` takes one item out of the inventory. A stack goes as a whole: to take a few
units out of a stack, use `set-count`. When the inventory holds more than one of an item,
add `--entry` with the number from `items`.

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Item:     GEAR_Crackers, inventory entry 11
Change:   removed (condition 95.8%). 442 entries are left in the inventory
Note:     every entry number above 11 is now one lower. Run items again before the next edit
Checked:  the edited inventory is the original one without this entry, and nothing else differs
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by remove-item has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 585,617 bytes
SHA-256:  b4d6df6e56608838cf623e0b9b4c88476ae56ccd9f9db635b01093144c90a893
sandbox15 itself was not changed.
```

Things to know about `remove-item`:

- Every entry number above the removed one moves down by one. Run `items` again before the
  next edit.
- The tool leaves some items in place and says why:
  - The two entries that hold the water you carry. Every real save has both. Use
    `set-liters` to empty them.
  - Clothing you are wearing. Take it off in the game first.
  - The item in your hands, and the last item you held. The save remembers both by the
    item's instance number, and that number would then point at nothing.
  - Any other item whose instance number appears a second time anywhere in the save. In 14
    real saves that happens only for the item in the hands and the last item held.
- The two saves differ in one place, the length of the inventory list. The comparison does
  not look inside a list that changed its length, so the tool then checks every item that is
  left against the original list.
- The C# editor removes an item from the list without any of these checks.

### Copy an item

Not tested in the game yet: nobody has loaded a save changed by `copy-item`. Try it on a
save you can afford to lose.

```powershell
python tld_save_edit.py items $save
python tld_save_edit.py copy-item $save --item GEAR_Crackers --entry 11 --out "$env:TEMP\sandbox15.edited"
```

`copy-item` adds another one of an item the inventory already holds. The copy goes to the
end of the inventory, so no entry number changes. It is the same as the original in
everything but its instance number: a stack is copied with its count, food with its
calories, a weapon with its rounds. When the inventory holds more than one of an item, add
`--entry` with the number from `items`.

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Item:     GEAR_Crackers, inventory entry 11
Change:   copied to a new entry at the end of the inventory, entry 443
Number:   the copy has the instance number -423202. The original keeps -281632
Checked:  the edited inventory is the original one plus this copy, and nothing else differs
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by copy-item has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 586,017 bytes
SHA-256:  73c5ad0309327896dd3a002004e0ed7e3020338a91b27a6616d92c6f9fb09133
sandbox15 itself was not changed.
```

Things to know about `copy-item`:

- It cannot add an item the inventory does not hold. That takes a record built from nothing
  for each kind of item, and it is not built yet: see `ROADMAP.md`.
- The copy gets an instance number of its own. The number is even and negative, as every
  number the game hands out while it runs is in the real saves (4,647 of them). It is below
  every number in the inventory, and the save holds it nowhere else.
- The tool does not copy some items, and says why:
  - The two entries that hold the water you carry. Use `set-liters` to add water.
  - Clothing you are wearing, because the copy would be worn as well.
  - An item whose record names that one item in another way too: a mission object, an item
    with a GUID of its own (34 of the 1,143 inventory items in the real saves), or an item
    with another item inside it, such as a respirator with its canister.
- Three things are checked after the copy is made: every original item is still in its
  place, the new item differs from its source in the instance number only, and the save
  holds the new number in that one place.

### The checks

Three checks stand between an edit and a written file. If one fails, nothing is left on disk.

1. The original must pass `tld_save_codec.py verify`.
2. The edited save, decoded again, must differ from the original in exactly what was asked
   for: one value, for `repair-all` one condition for each repaired item, for `remove-item`
   one inventory entry less, and for `copy-item` one more. Every other entry must be byte
   for byte unchanged.
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

With `TLD_SAVE_DIR` set, the edit tests also change one count, one condition, one amount of
liquid, one amount of calories and the rounds of one weapon in each real save that has
them, repair every real inventory, take one item out of each, and copy one item in each.
That happens in memory only: no file is written. The apply tests copy each real save into a temporary folder and
work on the copy. The reader tests write each real save's CSV to a temporary folder. Your
save folder is only read. Allow about ten minutes for the whole run.

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

- `region` stays blank for a scene the reader's table does not know, and for a reused
  interior (such as `TrailerC`) that no door of an outdoor zone leads to, in a save that
  has visited more than one region. No save checked so far has either.
- The names of the two mines that join regions come from the game's wiki, not from the
  game's own text.
- The reader does not work out the weight the game shows for what you carry. It reports the
  stored weight of each item (`unit_weight_kg`), which leaves out liquid, the part of a food
  item already eaten, and wet clothing.
- Powders (flour, salt, oats, gunpowder) store their amount as a weight. It is not read.
- The game's display name is known for 617 of the 1,040 prefabs: the items the C# editor
  names (game v2.27 and older), the mod items, and eleven items by way of the wiki. The
  other 423, most of them from the two DLCs, show a name made from the prefab name.
- Saves from before 2018 use a different format and are not supported.
- No save checked so far contains text outside plain ASCII. If the game writes such text
  in a form `tld_save_codec.py` does not reproduce, `verify` reports `FAILED` for that save.
- `tld_save_apply.py` knows the game by its Windows program name, `tld.exe`. On other
  systems it looks for a program named `tld`, and that has not been checked against the game.
