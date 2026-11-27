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
| `tld_save_edit.py` | Changes a save and writes the result to a new file. One value of one inventory item: its count, its condition, the liquid it holds, the calories left in it, or the rounds loaded in it. Or the condition of every inventory item at once. Or one item taken out of the inventory, or another one of an item added to it. Or the player's own values: condition, calories, and the bars for thirst, fatigue and cold. Or the points of one skill. Or an end to hypothermia, sprains and burns. Or the place where the character stands |
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
  - Clothing you are wearing. Take it off in the game first. An item whose clothing record
    the tool cannot read stays as well.
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
  - Clothing you are wearing, because the copy would be worn as well. An item whose clothing
    record the tool cannot read is not copied either.
  - An item whose record names that one item in another way too: a mission object, an item
    with a GUID of its own (34 of the 1,143 inventory items in the real saves), or an item
    with another item inside it, such as a respirator with its canister.
- Three things are checked after the copy is made: every original item is still in its
  place, the new item differs from its source in the instance number only, and the save
  holds the new number in that one place.

### The checks

Three checks stand between an edit and a written file. If one fails, nothing is left on disk.

1. The original must pass `tld_save_codec.py verify`, and it must hold no nested document
   that the codec could not open. The game writes a nested document without spaces, and the
   codec opens every one of those. A document written another way, with spaces between its
   values for example, stays plain text. The tool then cannot see what is inside it: whether
   an item is worn, whether the character is on a rope, or where the save holds an instance
   number. So every edit refuses such a save and names the place. Another editor may have
   written that document. None of the 14 real saves holds one. Text that is not JSON is
   left alone, so a journal note in square brackets is no obstacle.
2. The edited save, decoded again, must differ from the original in exactly what was asked
   for: one value, for `repair-all` one condition for each repaired item, for `remove-item`
   one inventory entry less, for `copy-item` one more, for `set-player` and `set-position`
   one value for each option, and for `cure` the values that end each case. Every other
   entry must be byte for byte unchanged.
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

## Change the player's condition and needs

`tld_save_edit.py` also changes the player's own values: condition, calories, and the three
bars for thirst, fatigue and cold. As with the inventory, the save you name is only read,
the result goes to a new file, and the three checks above stand in between.

Not tested in the game yet: nobody has loaded a save changed by `set-player`. Try it on a
save you can afford to lose.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox6"
python tld_save_edit.py player $save
python tld_save_edit.py set-player $save --condition 100 --calories 2500 --hydration 100 --rest 100 --out "$env:TEMP\sandbox6.edited"
```

`player` lists the five values, each the way the game shows it and the way the save holds it:

```text
value      in the game        in the save
condition  60.6%              m_CurrentHPProxy 60.6337242
calories   2453.3             m_CurrentReserveCaloriesProxy 2453.29224
hydration  90.3% of the bar   m_CurrentThirstProxy 9.663491
rest       38.4% of the bar   m_CurrentFatigueProxy 61.640625
warmth     100.0% of the bar  m_CurrentFreezingProxy is left out, which means 0
The player's 5 values in sandbox6.
For hydration, rest and warmth the save holds the inverse of the bar: 0 is a full bar, and 100 an empty one.
```

`set-player` takes one or more of these options and changes them in one edit:

| Option | What it sets | From | To |
|---|---|---|---|
| `--condition` | The player's condition, in percent | 1 | 100 |
| `--calories` | The calories the player has in store | 0 | 2500 |
| `--hydration` | The thirst bar, in percent of a full bar. 100 is not thirsty at all | 0 | 100 |
| `--rest` | The fatigue bar, in percent of a full bar. 100 is fully rested | 0 | 100 |
| `--warmth` | The cold bar, in percent of a full bar. 100 is warm | 0 | 100 |

```text
Save:     sandbox6, 291,658 bytes, passes the exact rebuild check
Change:   condition 60.6% -> 100.0% (stored as m_CurrentHPProxy 60.6337242 -> 100.0)
Change:   calories 2453.3 -> 2500.0 (stored as m_CurrentReserveCaloriesProxy 2453.29224 -> 2500.0)
Change:   hydration 90.3% of the bar -> 100.0% of the bar (stored as m_CurrentThirstProxy 9.663491 -> 0.0)
Change:   rest 38.4% of the bar -> 100.0% of the bar (stored as m_CurrentFatigueProxy 61.640625 -> 0.0)
Checked:  the edited save differs from the original in exactly these 4 values
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-player has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox6.edited, 291,695 bytes
SHA-256:  ac45e1d7e442b55bd1815bb3da4e9a17bebb0d944f63b95e2d110ca6ebfdfd95
sandbox6 itself was not changed.
```

Things to know about the player's values:

- Every number is given the way the game shows it, so 100 is the best number for all five.
  The save holds thirst, fatigue and cold the other way round: 0 is a full bar, and 100 an
  empty one. The game's wiki says so of thirst: it "is actually the inverse of what is
  displayed". So `--hydration 80` is written as a thirst of 20, and the `Change` line shows
  both. In one of 14 real saves all three had been set to 100 with another editor, which is
  dehydrated, exhausted and freezing.
- The game leaves a number out of a save when it is 0, and `player` then says `is left out,
  which means 0`. In the 14 real saves that is so for the cold in seven, and for the fatigue
  in one. `set-player` puts such a number in as the first field of its record. That is where
  the game writes it in every real save that holds one (62 of 62). The `Change` line says so,
  and one more check runs: the record must be the original one with that number in front, and
  nothing else in it may differ.

  ```text
  Change:   warmth 100.0% of the bar -> 60.0% of the bar (the game had left m_CurrentFreezingProxy out of the save, which means 0. It is put in as 40.0)
  Checked:  the number that was put in is the first in its record, where the game writes it, and nothing else in that record differs
  ```

  A save that holds no condition is the exception. That is the save of a dead character, and
  the tool does not bring it back.
- The numbers are written as 32-bit floats, the way the game writes them (checked against
  all 62 numbers that the 14 real saves hold for these five values).
- Only the numbers change. Next to each one the save keeps what the character last said
  about it and whether it is in the journal, in fields such as
  `m_ThirstLevelForPreviousVoiceOver` and `m_DehydratedInLog`. Those are left alone. What the
  game makes of a full thirst bar next to a note that says "dehydrated" is not known.
- The limits are what the real saves show. Their calories run from 423 to 2,491. One of them
  holds a condition of 105 while the Well Fed bonus is active, and the tool stops at 100.
- A number that would leave less than 0.0001 in the save, such as `--hydration 99.99999`, is
  refused. The game writes numbers that small in another form. Use 100.

## Change the points of a skill

`tld_save_edit.py` also changes the points of one of the nine skills. The save you name is
only read, the result goes to a new file, and the three checks above stand in between.

Not tested in the game yet: nobody has loaded a save changed by `set-skill`. Try it on a
save you can afford to lose.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox6"
python tld_save_edit.py skills $save
python tld_save_edit.py set-skill $save --skill cooking --points 150 --out "$env:TEMP\sandbox6.edited"
```

`skills` lists the nine skills with their points, the way the save holds them:

```text
skill               points  in the save
fire-starting           12  m_Points 12
carcass-harvesting       0  m_Points is left out, which means 0
cooking                  3  m_Points 3
ice-fishing              0  m_Points is left out, which means 0
rifle                    0  m_Points is left out, which means 0
archery                  0  m_Points is left out, which means 0
mending                  0  m_Points is left out, which means 0
revolver                 0  m_Points is left out, which means 0
gunsmithing              0  m_Points is left out, which means 0
The 9 skills in sandbox6.
The save holds points and no level. The game works the level out from the points.
```

`set-skill` takes one skill by its name in the first column, and the new points as a whole
number from 0 to 9999:

| `--skill` | In the game | Its record in the save |
|---|---|---|
| `fire-starting` | Fire Starting | `m_Skill_FirestartingSerialized` |
| `carcass-harvesting` | Carcass Harvesting | `m_Skill_CarcassHarvestingSerialized` |
| `cooking` | Cooking | `m_Skill_CookingSerialized` |
| `ice-fishing` | Ice Fishing | `m_Skill_IceFishingSerialized` |
| `rifle` | Rifle Firearm | `m_Skill_RifleSerialized` |
| `archery` | Archery | `m_Skill_ArcherySerialized` |
| `mending` | Mending | `m_Skill_ClothingRepairSerialized` |
| `revolver` | Revolver Firearm | `m_Skill_RevolverSerialized` |
| `gunsmithing` | Gunsmithing | `m_Skill_GunsmithingSerialized` |

```text
Save:     sandbox6, 291,658 bytes, passes the exact rebuild check
Skill:    cooking (m_Skill_CookingSerialized)
Change:   points 3 -> 150
Checked:  the edited save differs from the original in exactly this one value
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-skill has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox6.edited, 291,736 bytes
SHA-256:  a96e22d22df1b3afac5f950e43030f33c9ce4934d2c3ac1a74250e31078acc81
sandbox6 itself was not changed.
```

Things to know about skills:

- The save holds points and no level. The game works the level out from the points. How
  many points a level takes is fixed by the game, and the save does not hold it. The game's
  wiki, on its page about the Skilled Survivor achievement, names totals for level 5: 350
  for Cooking and for Mending, 250 for Ice Fishing, 200 for Fire Starting and 125 for
  Archery. This tool has not checked those figures against the game.
- The game leaves the points out of a save when they are 0, and `skills` then says `is left
  out, which means 0`. In the 14 real saves that is so for 74 of the 126 skill records.
  `set-skill` puts the points in as the first field of the record. That is where the game
  writes them in every real record that holds them (52 of 52, four of which hold a second
  number behind the points). The `Change` line says so, and the check of a number that was
  put in runs, as it does for the player's values:

  ```text
  Change:   points 0 -> 40 (the game had left m_Points out of the save, which means 0. It is put in as 40)
  Checked:  the number that was put in is the first in its record, where the game writes it, and nothing else in that record differs
  ```

- `--points 0` on a skill that has points writes a 0. The game would leave the number out.
  The old C# editor writes a 0 as well, and two of the 14 real saves hold such zeros.
- Only the points change. The record of Carcass Harvesting can hold a second number,
  `m_NumHoursToConvertToSkillPoints`, which is below 1 in all five real records that hold
  it. That number is left alone.
- The save has another record with "skills" in its name, `m_PlayerSkillsSerialized`, with
  three numbers of its own. `set-skill` does not read or change it.
- The limit of 9999 only catches a slip of the hand. The highest number in the 14 real
  saves is 264.

## Cure afflictions

`tld_save_edit.py` also ends afflictions. It reads five kinds: hypothermia, sprain pain,
sprained ankles, sprained wrists and burns. `cure` ends every case of those five that the
save holds. The save you name is only read, the result goes to a new file, and the three
checks above stand in between.

Not tested in the game yet: nobody has loaded a save changed by `cure`. Try it on a save
you can afford to lose.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox10"
python tld_save_edit.py afflictions $save
python tld_save_edit.py cure $save --out "$env:TEMP\sandbox10.edited"
```

`afflictions` lists the five kinds, and how many cases of each the save holds:

```text
affliction      cases  in the save
hypothermia         0  m_HypothermiaSerialized
sprain-pain         1  m_SprainPainSerialized
sprained-ankle      0  m_SprainedAnkleSerialized
sprained-wrist      2  m_SprainedWristSerialized
burns               0  m_BurnsSerialized
3 cases of these 5 kinds of affliction in sandbox10.
A save can hold other afflictions, such as frostbite or food poisoning. This tool does not read them.
```

`cure` takes `--out` and nothing else:

```text
Save:     sandbox10, 703,636 bytes, passes the exact rebuild check
Cured:    sprain-pain, 1 case (m_SprainPainSerialized: 3 lists emptied)
Cured:    sprained-wrist, 2 cases (m_SprainedWristSerialized: 5 lists emptied)
Checked:  the edited save differs from the original in exactly these 8 values
Checked:  6 of 7 entries are byte for byte unchanged (all except global)
Untested: no save changed by cure has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox10.edited, 703,596 bytes
SHA-256:  a7b82252f4ad9c63858899a31f866df393dedc81fc5d7f01ad9447d8e8dd7560
sandbox10 itself was not changed.
```

Things to know about curing:

- The save holds an affliction in one of two ways. Hypothermia and burns are there or they
  are gone, and the switch `m_Active` says which. `cure` sets it from `true` to `false`.
  Sprain pain and sprains can be there several times. Their record keeps several lists,
  each with one item for every case, and `cure` empties those lists: three for sprain pain,
  five for sprained ankles and for sprained wrists.
- Everything else in a record stays: how long the hypothermia has lasted, whether a burn is
  bandaged, the timers of the pain sounds. The game leaves the same things behind. One of
  the 14 real saves holds what is left of hypothermia and of a burn that are over, with
  every such field still in place and the switch gone.
- The game leaves `m_Active` out of a record while it is off. `cure` writes `false`, as the
  old C# editor does.
- These five kinds are the ones that a real save has shown in use. Of the 14 real saves,
  two hold hypothermia, one holds a burn, two hold sprained wrists and two hold sprain
  pain. A sprained ankle is stored exactly like a sprained wrist, and no real save holds
  one.
- Other afflictions are neither read nor changed: frostbite, food poisoning, dysentery,
  infections, blood loss, broken ribs, cabin fever, parasites and the rest. `afflictions`
  does not list them, so a save that shows no case here can still hold one of those. A kind
  gets added once a real save shows how the game stores it in use.
- The bonuses in the same part of the save, such as Well Fed, are left alone.
- If one of the five kinds is not stored the way the tool expects, `cure` changes nothing
  and names the kind. A save with nothing to cure is refused as well.
- `cure` does not touch the bars. A character who is still freezing gets hypothermia again:
  see `--warmth` under `set-player`.

## Move the character

`tld_save_edit.py` also moves the character inside the scene the save is in. The save you
name is only read, the result goes to a new file, and the three checks above stand in
between.

Not tested in the game yet: nobody has loaded a save changed by `set-position`. Try it on a
save you can afford to lose.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox15"
python tld_save_edit.py position $save
python tld_save_edit.py set-position $save --x -0.797 --y 0.0165060163 --z -1.38356638 --out "$env:TEMP\sandbox15.edited"
```

`position` prints the scene and the three numbers of the position:

```text
Scene:     GreyMothersHouseA
Position:  x -1.464066, y -0.5301075, z -1.0880059
Where the character stands in sandbox15. y is the height. The numbers count inside this scene only.
```

`set-position` takes one or more of `--x`, `--y` and `--z`:

```text
Save:     sandbox15, 585,798 bytes, passes the exact rebuild check
Scene:    GreyMothersHouseA. It is not changed, and the numbers count inside it only
Change:   x -1.464066 -> -0.797
Change:   y -0.5301075 -> 0.0165060163
Change:   z -1.0880059 -> -1.38356638
Checked:  the edited save differs from the original in exactly these 3 values
Checked:  7 of 8 entries are byte for byte unchanged (all except global)
Untested: no save changed by set-position has been loaded in the game yet. Try it on a save you can afford to lose
Written:  C:\Users\you\AppData\Local\Temp\sandbox15.edited, 585,824 bytes
SHA-256:  627128049ec34383720290dc88df46e7ebebca5d15482e79e83b7f85c696719d
sandbox15 itself was not changed.
```

Things to know about moving:

- The tool does not know the ground, the walls or the floors. A spot inside a wall or under
  the floor is written like any other. Take the numbers from a spot that is known to be
  good: where the character stands in another save of the same scene, as `position` prints
  it, or where an item lies, from the `x`, `y` and `z` columns of the reader's CSV. The
  example above uses the spot where the character of another real save stands in the same
  house.
- The numbers count inside one scene. Every region and every building has numbers of its
  own. The tool does not take the character to another scene.
- Only the three numbers of `m_SaveGamePosition` change, which are the numbers the old C#
  editor changes. Every one of the 14 real saves tells the game to put the character at
  that position when it loads (`m_TeleportPlayerSaveGamePosition`).
- The save keeps two more things about the place, and both are left alone. One is the
  direction the character faces, `m_SaveGameRotation`. The other is
  `m_PlayerInVehicleCameraPos`, which in all 14 real saves is the position with the height
  of the eyes added to `y`: 1.75, or 0.83 in two saves. After a move it still holds the old
  place. What the game makes of that is part of the in-game test.
- A number that the save holds already stays as the game wrote it, and the output says
  `Same`. The edit goes ahead when one or more of the other numbers changes.
- A character in a vehicle, in a snow shelter or on a rope is not moved. `position` says so.
  One of the 14 real saves has the character in a vehicle. When the tool cannot read the
  record of the rope, it does not move the character either, and `position` names the
  record.
- The numbers are written as 32-bit floats, the way the game writes them (checked against
  41 of the 42 numbers in the 14 real saves). They may run from -100000 to 100000, and the
  farthest in a real save is 1,980. A number closer to 0 than 0.0001 is refused, because the
  game writes numbers that small with an exponent, as it did for the one number left of the
  42. Use 0.
- A number with a minus sign and an exponent can be taken for an option. Write it after an
  equals sign: `--y=-1.1920929E-07`.

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
4. The edited file holds the same entries as the live save (`boot`, `global`, and one for
   each scene), in the same order. `tld_save_edit.py` never adds, removes or moves an entry,
   so a file with other entries did not come from it. The refusal names the entries that
   are missing or extra. A file with no entries at all passes the three checks above, and
   this one stops it.
5. The edited file keeps the last change that `apply` or `restore` made to the save. This
   tool does not touch the time that the game stamped on a save, so check 3 cannot tell
   apart two edited files that were made from the same save. Putting the second one in place
   would undo the first without a word. To catch that, the tool reads the newest backup of
   the save in the backup folder. When the game has not written the save since that backup
   was taken, the backup is the save as it was before the last change, and the edited file
   must still hold that change:
   - An inventory entry that the last change removed is still gone.
   - An entry that it added is still there.
   - A value that it changed is not back at what the backup holds. The value may have
     changed again, and the item that holds it may have been removed.

   The refusal names the first thing that the file would undo. The check cannot know why a
   file lacks the last change, so it also refuses a file that takes a part of it back on
   purpose, and a file that brings back something a `restore` took away. To take the whole
   change back, use `restore`. To put such a file in place as it is, add `--allow-undo`.

Check 5 has limits. It does not run when the backup folder holds no backup of the save,
when the newest one cannot be read, or when the game has written the save since that backup
was taken. It sees only the backup folder in use, so a backup that was moved away, or
another `--backup-dir`, hides the last change from it. It looks one change back and no
further. A list that is longer or shorter than before counts as one value. When the check
has run, the preview says so and names the backup. With `--allow-undo` it names the first
thing that goes:

```text
Checked:    the edited file keeps the last change made to the save (backup sandbox15.backup-20261124-051500)
```

```text
Undoes:     the last change made to the save, or a part of it, as --allow-undo allows (backup sandbox15.backup-20261124-051500): m_Dict > global > m_Inventory_Serialized > m_SerializedItems[3] > m_SerializedGear > m_StackableItemSerialized > m_UnitsProxy would lose the change from 40 to 50
```

Then it works in this order and stops at the first step that fails:

6. The live save is copied to the backup folder under a new name, and the copy is read back.
7. The edited content is put in place of the live save in one step, and the save is read
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

`--dry-run` runs every check, prints what would happen, and changes nothing. `--allow-undo`
lets `apply` put a file in place that takes back the last change to the save, or a part of
it: see check 5 above.

Exit code 0 means done, 1 means refused or stopped with the reason printed, and 2 means a
file was not found or an option was wrong.

Checked in the game (v2.60, changelist 189457) on 2026-11-24, on a survival save:

- `apply` put an edit of Wood Matches from 40 to 50 in place. The game showed 50.
- With the game running, `restore` refused and changed nothing.
- With the game closed, `restore` backed up the game's newer save and put the earlier one
  back. The game showed 40, and the save file was byte for byte the one from before the
  test.

### Several edits, then one apply

An edit command reads one file and writes a new one. So edits can follow each other: give
each command the file that the one before it wrote, and put the last file in place.

```powershell
$save = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival\sandbox15"
python tld_save_edit.py set-liters $save --item GEAR_WaterSupplyPotable --liters 2.5 --out "$env:TEMP\sandbox15.step1"
python tld_save_edit.py copy-item "$env:TEMP\sandbox15.step1" --item GEAR_Crackers --entry 11 --out "$env:TEMP\sandbox15.step2"
python tld_save_edit.py remove-item "$env:TEMP\sandbox15.step2" --item GEAR_BirchbarkTea --entry 148 --out "$env:TEMP\sandbox15.step3"
python tld_save_apply.py apply $save --from "$env:TEMP\sandbox15.step3" --dry-run
```

`apply` compares the last file with the live save and lists what differs before it changes
anything. For these three edits its output starts like this:

```text
Save:       sandbox15, 585,798 bytes, last saved by the game at 2026-11-24 04:49:13
Edited:     C:\Users\you\AppData\Local\Temp\sandbox15.step3, 585,980 bytes
Removed:    1 inventory entry (entry numbers as in the save)
              entry 148, GEAR_BirchbarkTea
Added:      1 inventory entry (entry numbers as in the edited file)
              entry 442, GEAR_Crackers
Changes:    1 other value differs (entry numbers as in the save)
              m_Dict > global > m_Inventory_Serialized > m_SerializedItems[1] > m_SerializedGear > m_WaterSupplySerialized > m_VolumeProxy: 2000000000 -> 2500000000
Checked:    the game is not running
Checked:    both files pass the exact rebuild check
Checked:    the edited file was made from the save as it is now
```

- `Removed` lists the inventory entries that the edited file no longer holds, each under its
  entry number in the live save. `Added` lists the entries that only the edited file holds,
  each under its entry number in that file: the copied crackers were entry 443 after the
  second edit, and they are entry 442 after the third, which took out an entry before them.
  A block is left out when it has nothing to list.
- `Changes` lists every other value that differs. It names an inventory entry by its number
  in the live save, also when an entry before it is gone.
  - A field that only one of the two files holds is listed with its value on that side and
    `(left out)` on the other, as in `m_Points: (left out) -> 40`. That is how a number
    shows up that `set-player` or `set-skill` put in because the game had left it out.
  - When the fields that both files hold are in another order, that is a line of its own.
    Where in its record a new field sits is not shown.
  - A list that is longer or shorter than before is one line with the two lengths, as in
    `2 items -> 0 items` after `cure`. What changed inside such a list is not listed.
- Each block shows ten lines at most and counts the rest.
- The entries of the two inventories are matched by instance number and item name. The name
  counts because `copy-item` can give a copy the number that a removed item had, and that
  is listed as one entry removed and one added. When a number is missing or occurs twice,
  or when the entries that both inventories hold are in another order, the inventories are
  compared position by position. An inventory of another length is then one line under
  `Changes`, and nothing is listed as removed or added.

Things to know about several edits in a row:

- `remove-item` moves every entry number above the removed one down by one. Make it the last
  edit, or run `items` on the file it wrote before the next edit.
- An edit never overwrites a file, so every step needs a file name that is not taken. For a
  second round, use other names or another folder.
- `apply` checks the last file against the live save as it is at that moment. When the game
  has saved since the first edit was made, it refuses, and the edits have to be made again.
- Two files that were each made from the live save cannot be put in place one after the
  other. The second one does not hold what the first one changed, so `apply` refuses it
  (check 5 above). Give each edit the file that the edit before it wrote, as in the example,
  or make the second file once the first is in place.

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
them, repair every real inventory, take one item out of each, copy one item in each, change
the player's five values, change the points of up to two skills in each, cure each real
save that holds one of the five afflictions, and move the character in each. That happens
in memory only: no file is written. The apply tests copy each real save into a temporary
folder and work on the copy. The reader tests write each real save's CSV to a temporary
folder. Your save folder is only read. Allow ten to fifteen minutes for the whole run.

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
