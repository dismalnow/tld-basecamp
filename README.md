# tld-basecamp

Python tools for The Long Dark survival saves. They list what a save holds, check that a
save can be rebuilt byte for byte, change one value, and put the result in place after a
checked backup.

The tools and their guide are in
[`The Long Dark Save Editor 2/SaveExtractor`](The%20Long%20Dark%20Save%20Editor%202/SaveExtractor).

| Tool | What it does |
|---|---|
| `tld_save_reader.py` | Lists every item in a save, as a summary or a CSV |
| `tld_save_codec.py` | Checks that a save can be rebuilt byte for byte (`verify`) |
| `tld_save_edit.py` | Changes one value and writes the result to a new file |
| `tld_save_apply.py` | Puts an edited file in place after a checked backup, and puts a backup back |

Only `tld_save_apply.py` changes a save file, and it backs the save up first.

Checked against game v2.60 (changelist 189457). These tools need Python 3.9 or newer and
nothing outside the standard library.

## Where this came from

This repository started as a fork of
[FINDarkside/TLD-Save-Editor](https://github.com/FINDarkside/TLD-Save-Editor), a C# save
editor for The Long Dark. That editor's source is still here, unchanged, in
`The Long Dark Save Editor 2`. It is kept as reference and is not developed here: the
Python compressor is a port of its `Helpers/CLZF.cs`, and the first item table came from
its `Helpers/ItemDictionary.cs`.

This is a fan project. It is not affiliated with or endorsed by Hinterland Studio, the
maker of The Long Dark.

## License

MIT. See [`LICENSE`](LICENSE). The LZF compressor code carries its own notice, in
`Helpers/CLZF.cs` and in `tld_save_codec.py`.
