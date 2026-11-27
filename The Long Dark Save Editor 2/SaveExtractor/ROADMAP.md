# Roadmap

What gets built, in what order, and where each piece stands. The owner decides the order.
`README.md` in this folder says what the tools do today. This file says what comes next.

Coding agents: do not start an item on your own. Ask the owner which one to take. When a
change finishes or advances an item, update this file in the same pull request.

## Build order

| Block | What it adds | State |
|---|---|---|
| F | Foundations: the tests run on GitHub, the wiki helper has tests, the folder has one decompressor, and coding agents have a context file | Done (pull requests 1 to 4) |
| R | Reader depth: more of what a save holds, read correctly | Built (pull requests 5, 6 and 8). Open: the owner compares a few weights, liters and calories with the game's inventory screen |
| I | Inventory edits: add an item, remove an item, repair everything, liters, calories, rounds | Built (pull requests 9 to 15), except for adding an item the inventory does not hold and film in the camera. Not tested in game. Open: the owner's session in the game, from the checklist in the block report |
| P | Player edits: condition, calories, thirst, fatigue, cold, position, cure afflictions, skills | Built (pull requests 16 to 20). Not tested in game. `cure` covers five kinds of affliction, and the others wait for a save that holds one. Open: the owner's session in the game, from the checklist in the block report |
| Trader | The Trader: read his state (trust, contacts, offers, special requests), a report command, and one edit (trust) | Not started. The owner has not yet confirmed the place in the order or the edit |
| T | Live tracker: where animals, corpses, Tale objects and one-of-a-kind items are as of the last game save, then a watcher that follows new saves | Not started |
| U | Front end: a standalone desktop client that follows the design of the C# editor | Not started |

## Block R in detail

| Item | State |
|---|---|
| Weight, liters and calories for every item | Done (pull request 5) |
| The game's display name where a source states it | Done for 617 of 1,040 prefabs (pull request 6). The other 423 need the game's own text table |
| A region for every record outside the inventory | Done (pull request 8). In 14 real saves no such record has a blank region now |

## Block I in detail

| Item | State |
|---|---|
| Liters: the liquid an inventory item holds | Built (pull request 9). Not tested in game |
| Calories of a food item | Built (pull request 10). Not tested in game |
| Rounds in a weapon | Built (pull request 11). Not tested in game |
| Film in the camera | Decided by the owner on 2026-11-24: treat the camera like a gun, with three numbers: shots left, shots taken, and where the images are. The save keeps them in the camera's own list of rounds, in two photo counters, and in a photo list. All of these are empty in the 14 saves at hand, so this waits for a save with film loaded and photos taken |
| Repair everything in the inventory | Built (pull request 12). Not tested in game. Torches and flares that have burned are left alone |
| Remove an item | Built (pull request 13). Not tested in game |
| Add an item | First step built (pull request 14): `copy-item` adds another one of an item the inventory holds. Not tested in game. An item the inventory does not hold is not started: it needs a record built for each kind of item, and it waits for the in-game results |
| Several edits put in place at once | Built (pull request 15): `apply` lists the inventory entries that an edited file adds or takes away, and every other value under its entry number in the live save. Before that, a file with an entry more or less was shown as one changed value, or as hundreds |

## Block P in detail

| Item | State |
|---|---|
| Condition, calories, and the bars for thirst, fatigue and cold | Built (pull request 16): `player` lists the five, `set-player` changes them. Not tested in game |
| A value the game left out of the save | Built (pull request 17). Not tested in game. The game leaves a number out when it is 0. `set-player` puts it in as the first field of its record, where the game writes it, and checks that record on its own. A missing condition is left alone |
| Skills | Built (pull request 18): `skills` lists the points of the nine skills, and `set-skill` changes the points of one. Not tested in game. The save holds points and no level, so the tool works in points |
| Cure afflictions | Built (pull request 19) for the five kinds that a real save has shown in use or that are stored the same way: hypothermia, sprain pain, sprained ankles, sprained wrists and burns. `afflictions` lists them, and `cure` ends every case. Not tested in game. Other afflictions wait for a save that holds one |
| Position | Built (pull request 20): `position` prints the scene and where the character stands, and `set-position` moves the character inside that scene. Not tested in game. The tool does not know the ground, so the numbers have to come from a spot that is known to be good |

## Review fixes

On 2026-11-25 Codex, a coding agent that had not written this code, reviewed the edits that
are not tested in game and the `apply` command. It reported ten findings. Each one was
checked on copies of real saves or in the code, and all ten hold. Codex then wrote nine
fixes from written instructions. Each fix is checked again and merged as a pull request of
its own. None of them is a new kind of edit, so none adds to what waits for a test in the
game.

| Fix | What it changes | State |
|---|---|---|
| Entry table | `apply` refuses a file that does not hold the entries of the live save, by name and in the same order. Before, a file with no entries at all passed every check | Done (pull request 21) |
| Preview fields | The `apply` preview lists a field that was put in or taken out with its value. Before, it gave two lists of field names and no numbers | Done (pull request 22). A list that is longer or shorter than before is still one line with the two lengths |
| Pairing | The `apply` preview matches inventory entries by instance number and item name. Before, a copy that got the number of a removed item was shown as that item with other values | Done (pull request 23) |
| Last change | `apply` refuses a file that would undo the last `apply` or `restore`, which it finds by comparing with the newest backup. `--allow-undo` puts such a file in place all the same. Before, two files made from one save could be put in place one after the other, and the second undid the first without a word | Done (pull request 24). The check looks one change back, needs the backup of that change in the backup folder, and takes a list that is longer or shorter than before as one value |
| Unread documents | Every edit refuses a save that holds a nested document the codec could not open, and the checks for worn clothing and for a rope refuse a record they cannot read. Before, such a record counted as not worn and no rope | Done (pull request 25). Text that is not JSON counts as ordinary text and stops no edit |
| Nested numbers | `remove-item` also checks the instance number of an item inside the entry, such as the canister in a respirator. Before, the entry was removed even when the save still pointed at the item inside it | Done (pull request 26) |
| Liters | `set-liters` refuses to change an amount written with a decimal point or an exponent, which is not how the game writes it, because its unit cannot be known. A new amount with more than nine decimal places is refused however many digits it has | Written. In review |
| Copy number range | `copy-item` refuses when the instance number for the copy would fall below the lowest number a 32-bit field holds | Written. In review |
| Dead at zero | `set-player` refuses to change a condition of zero or less, as it already refused a missing one | Written. In review |

## What holds for every block

- A new kind of edit counts as "not tested in game" until the owner has loaded an edited
  save in the game. In-game tests happen once per block, from a checklist.
- Each block ends with a report to the owner: what was merged, the evidence, what is still
  unproven, and what comes next.

## Stretch goals

Far out. Nothing here is scheduled.

- **Try to repair a damaged save.** Asked for by the owner on 2026-11-24. Where the reader
  stands today, from a test with 434 deliberately damaged copies of 14 real saves: it refuses
  262, reads 103 and names the locations it could not read, reads 68 correctly, and reads 1
  with a wrong character and no warning. A save carries no checksum. A first step would be to
  salvage every location that still reads and to say what is lost.

## Not planned

- Feats and profile edits. The owner looks at this again after block P.
- The three cheat switches of the C# editor.
- Story-mode saves, and saves from before 2018.
