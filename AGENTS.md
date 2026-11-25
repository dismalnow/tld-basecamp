# Instructions for coding agents

This file is for AI coding agents that work in this repository. Codex reads it as it is, and
Claude Code reads it through `CLAUDE.md`. People should start with `README.md`.

## What this repository is

tld-basecamp: Python tools for The Long Dark survival saves. They list what a save holds,
check that a save rebuilds byte for byte, change single values, and put an edited file in
place after a checked backup.

| Layer | Where | Status |
|---|---|---|
| Python tools | `The Long Dark Save Editor 2/SaveExtractor` | The product. All work happens here |
| C# editor | The rest of `The Long Dark Save Editor 2` | Reference only. Do not change it |

`SaveExtractor/README.md` is the user guide and lists every tool. Read the part about a tool
before you change that tool, and update it in the same change.

## What to work on

The owner decides what is built and in what order. The plan, and the state of each piece, is
in `The Long Dark Save Editor 2/SaveExtractor/ROADMAP.md`. Do not start an item from it on
your own: ask the owner which one to take. When a change finishes or advances an item,
update that file in the same pull request.

## Commands

Run these from `The Long Dark Save Editor 2/SaveExtractor`.

```
python -m unittest                       # the whole suite, a few seconds
python -m unittest test_tld_save_edit    # one test file
```

To include the checks against real saves (they only read the saves, and take ten to fifteen
minutes), point `TLD_SAVE_DIR` at a save folder first. In PowerShell:

```
$env:TLD_SAVE_DIR = "$env:LOCALAPPDATA\Hinterland\TheLongDark\Survival"
python -m unittest
```

Style checks, when the two tools are installed:

```
python -m pycodestyle --max-line-length=112 FILE
python -m pyflakes FILE
```

GitHub runs the suite on every pull request and every push to `master`, on Windows and
Linux with Python 3.9 through 3.14. The workflow is `.github/workflows/tests.yml`.

## Rules for saves

1. Never write to a live save folder. One tool changes a live save, `tld_save_apply.py`,
   and only a person runs its `apply` and `restore` commands. An agent does not.
2. Never commit a save, a backup, a report, or any other file from someone's game folder or
   home folder.
3. Tests build their own small saves. Checks against real saves only read them and write to
   temporary folders.
4. An edit must prove its own result, the way `tld_save_edit.py` does: the edited save
   rebuilds exactly, and it differs from the original in exactly the value that was asked for.
5. A new kind of edit counts as "not tested in game" until the owner has loaded an edited
   save in the game. Until then the README and the command's own output say so.

## The save format in brief

A save file is LZF-compressed JSON. Its `m_Dict` maps entry names (`boot`, `global`, and one
per scene) to base64 text, and each of those holds another LZF-compressed JSON document.
Inside them, some text values hold one more JSON document. `tld_save_codec.py` decodes every
layer and encodes it back to the same bytes. Use it. Do not write new decoding code.

- Numbers stay exactly as the game wrote them (`Number` in `tld_save_codec.py`). A number
  that goes through `float` can come back in another form, and then the save no longer
  rebuilds byte for byte.
- Field names such as `m_SearializedGear` are the game's own spelling. Do not correct them.
- `tld_taxonomy.py` is the one item table. For a prefab name, trust the game's catalogs
  first, then real saves, then `Helpers/ItemDictionary.cs`, and the wiki last.

## Rules for code

- Python 3.9 or newer, standard library only. The one exception is `requests`, which
  `tld_wiki_updater.py` and its tests use.
- Type hints on everything new. `pathlib` for paths and `argparse` for command lines.
- Handle errors explicitly, with a message that a player can act on.
- No TODOs, stubs or placeholders.
- Lines up to 112 characters. Add no new `pycodestyle` or `pyflakes` findings. Older files
  have a few. Leave those alone unless the task is to fix them.
- One concern per change. No refactoring that the task does not need.
- Plain American English in messages, comments and documents.

## How a change is proven

Decide how the change will be proven before you write it. Put the evidence in the pull request:

- New tests that fail without the change.
- The whole suite passing, on GitHub and on your machine.
- When real saves are at hand: the suite with `TLD_SAVE_DIR` set. For a change to a reader,
  also a before and after comparison of everything it produces.
- What is still unproven, said plainly.

## Workflow

- Work on a branch and open a pull request against `master` of this repository.
- Never open a pull request, an issue or a comment on the repository this one was forked
  from, or on anyone else's.
- Pull request text has four parts: What, Why, Evidence, Limits.
- Squash merge once the tests pass, and only when the owner has said that the agent may
  merge. No force pushes, and no rewritten history on `master`.

## Stop and ask the owner first

- A new dependency.
- A change outside `SaveExtractor`, apart from this file, `CLAUDE.md` and
  `.github/workflows/tests.yml`.
- Any change to the C# code.
- Deleting files, changing repository settings, or publishing a release.
- A check that fails on a real save.
- Anything that would run against a live save.

## Privacy

This repository is public. Keep user names, email addresses, machine names, tokens and
personal paths out of files, commit messages and pull request text. In examples, write
`C:\Users\you` or `%USERPROFILE%`.
