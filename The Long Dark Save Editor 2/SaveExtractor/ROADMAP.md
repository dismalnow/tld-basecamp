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
| I | Inventory edits: add an item, remove an item, repair everything, liters, calories, rounds | In progress |
| P | Player edits: condition, calories, thirst, fatigue, cold, position, cure afflictions, skills | Not started |
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
| Film in the camera | Asked for by the owner on 2026-11-24. The camera uses the weapon record, and no save at hand has a loaded camera. Waiting for one |
| Repair everything in the inventory | Not started |
| Remove an item | Not started |
| Add an item | Not started |

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
