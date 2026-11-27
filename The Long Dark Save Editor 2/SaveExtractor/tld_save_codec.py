#!/usr/bin/env python3
"""
tld_save_codec.py: decode a The Long Dark save and encode it again, exactly.

Usage:
    python tld_save_codec.py verify PATH [PATH ...]

PATH is a save file, or a folder that holds save files (sandbox1, challenge3, ...).
verify decodes every layer of a save, encodes it again, and reports whether the result
is byte for byte the file it started from.

Exit codes: 0 every save was identical, 1 at least one was not, 2 a path was wrong.

This module only reads. Nothing in it writes a file.

Layers of a save, outside in:
    file           LZF-compressed JSON text: the slot document
    m_Dict[name]   base64 of LZF-compressed JSON text: "boot", "global", one document per scene
    text values    some text values hold another JSON document as text, several levels deep

Two things make an exact rebuild possible:
    - Numbers are kept exactly as written (Number). The game writes forms such as
      6.204471E-05, which Python's json module would write back as 6.204471e-05.
    - lzf_compress is a port of Helpers/CLZF.cs in this repository. It produces the same
      bytes as the game's own compressor.

A document that cannot be written back identically is refused with SaveFormatError.
Nothing is changed silently.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union, cast

ENTRY_TABLE = "m_Dict"

# The game's save file names. Same rule as Util.GetSaveFiles in the C# editor.
SAVE_NAME = re.compile(r"(ep[0-9])?(sandbox|challenge|story|relentless)[0-9]+")


class SaveFormatError(ValueError):
    """The data is not a save this module can decode and write back identically."""


# ---------------------------------------------------------------------------
# LZF compression
# ---------------------------------------------------------------------------
#
# The LZF code in this section follows Helpers/CLZF.cs in this repository: lzf_compress is a port
# of its lzf_compress, and lzf_decompress reads the same format. CLZF.cs carries the notice below.
# Its first condition says that redistributions of source code must retain the notice, so it is
# copied here word for word. It applies to the code in this section.
#
#     Improved version to C# LibLZF Port:
#     Copyright (c) 2010 Roman Atachiants <kelindar@gmail.com>
#
#     Original CLZF Port:
#     Copyright (c) 2005 Oren J. Maurice <oymaurice@hazorea.org.il>
#
#     Original LibLZF Library  Algorithm:
#     Copyright (c) 2000-2008 Marc Alexander Lehmann <schmorp@schmorp.de>
#
#     Redistribution and use in source and binary forms, with or without modifica-
#     tion, are permitted provided that the following conditions are met:
#
#       1.  Redistributions of source code must retain the above copyright notice,
#           this list of conditions and the following disclaimer.
#
#       2.  Redistributions in binary form must reproduce the above copyright
#           notice, this list of conditions and the following disclaimer in the
#           documentation and/or other materials provided with the distribution.
#
#       3.  The name of the author may not be used to endorse or promote products
#           derived from this software without specific prior written permission.
#
#     THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
#     WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MER-
#     CHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO
#     EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPE-
#     CIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
#     PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
#     OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
#     WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTH-
#     ERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
#     OF THE POSSIBILITY OF SUCH DAMAGE.
#
#     Alternatively, the contents of this file may be used under the terms of
#     the GNU General Public License version 2 (the "GPL"), in which case the
#     provisions of the GPL are applicable instead of the above. If you wish to
#     allow the use of your version of this file only under the terms of the
#     GPL and not to allow others to use your version of this file under the
#     BSD license, indicate your decision by deleting the provisions above and
#     replace them with the notice and other provisions required by the GPL. If
#     you do not delete the provisions above, a recipient may use your version
#     of this file under either the BSD or the GPL.
#
# End of the notice from Helpers/CLZF.cs.

_HASH_SIZE = 1 << 14
_MAX_LITERAL_RUN = 1 << 5
_MAX_DISTANCE = 1 << 13
_MAX_MATCH = (1 << 8) + (1 << 3)
_UINT32 = 0xFFFFFFFF


def lzf_decompress(data: bytes) -> bytes:
    """Decompress LZF data. Raises SaveFormatError when the data is not a complete LZF stream."""
    out = bytearray()
    position = 0
    end = len(data)
    while position < end:
        control = data[position]
        position += 1
        if control < _MAX_LITERAL_RUN:
            run = control + 1
            if position + run > end:
                raise SaveFormatError("LZF data ends inside a run of plain bytes")
            out += data[position:position + run]
            position += run
            continue
        length = control >> 5
        if length == 7:
            if position >= end:
                raise SaveFormatError("LZF data ends inside a back-reference")
            length += data[position]
            position += 1
        if position >= end:
            raise SaveFormatError("LZF data ends inside a back-reference")
        distance = ((control & 0x1F) << 8) + data[position] + 1
        position += 1
        length += 2
        start = len(out) - distance
        if start < 0:
            raise SaveFormatError("LZF back-reference points before the start of the data")
        if distance >= length:
            out += out[start:start + length]
        else:
            # The copy overlaps its own output, so the last `distance` bytes repeat.
            pattern = bytes(out[start:])
            repeats, remainder = divmod(length, distance)
            out += pattern * repeats + pattern[:remainder]
    return bytes(out)


def _hash_slot(hval: int) -> int:
    # Mirrors this line of Helpers/CLZF.cs, including its unusual shift count:
    #   ((hval ^ (hval << 5)) >> (int)(((3 * 8 - HLOG)) - hval * 5) & (HSIZE - 1))
    # C# uses only the low five bits of a 32-bit shift count, hence "& 31".
    return (((hval ^ (hval << 5)) & _UINT32) >> ((10 - hval * 5) & 31)) & (_HASH_SIZE - 1)


def lzf_compress(data: bytes) -> bytes:
    """Compress with LZF, byte for byte the way Helpers/CLZF.cs lzf_compress does."""
    size = len(data)
    if size == 0:
        return b""
    if size == 1:
        # The C# code cannot take a single byte. One run of plain bytes is the only encoding.
        return bytes((0, data[0]))
    out = bytearray()
    table = [0] * _HASH_SIZE
    index = 0
    literals = 0
    hval = (data[0] << 8) | data[1]
    while True:
        if index < size - 2:
            hval = ((hval << 8) | data[index + 2]) & _UINT32
            slot = _hash_slot(hval)
            reference = table[slot]
            table[slot] = index
            distance = index - reference - 1
            if (distance < _MAX_DISTANCE and index + 4 < size and reference > 0
                    and data[reference] == data[index]
                    and data[reference + 1] == data[index + 1]
                    and data[reference + 2] == data[index + 2]):
                length = 2
                max_length = min(size - index - length, _MAX_MATCH)
                while True:
                    length += 1
                    if length >= max_length or data[reference + length] != data[index + length]:
                        break
                if literals:
                    out.append(literals - 1)
                    out += data[index - literals:index]
                    literals = 0
                length -= 2
                index += 1
                if length < 7:
                    out.append((distance >> 8) + (length << 5))
                else:
                    out.append((distance >> 8) + (7 << 5))
                    out.append(length - 7)
                out.append(distance & 0xFF)
                index += length - 1
                hval = (data[index] << 8) | data[index + 1]
                hval = ((hval << 8) | data[index + 2]) & _UINT32
                table[_hash_slot(hval)] = index
                index += 1
                hval = ((hval << 8) | data[index + 2]) & _UINT32
                table[_hash_slot(hval)] = index
                index += 1
                continue
        elif index == size:
            break
        literals += 1
        index += 1
        if literals == _MAX_LITERAL_RUN:
            out.append(_MAX_LITERAL_RUN - 1)
            out += data[index - literals:index]
            literals = 0
    if literals:
        out.append(literals - 1)
        out += data[index - literals:index]
    return bytes(out)


# ---------------------------------------------------------------------------
# Exact JSON
# ---------------------------------------------------------------------------

_NUMBER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_NUMBER_WORDS = frozenset({"NaN", "Infinity", "-Infinity"})


class Number:
    """A JSON number kept exactly as written, for example 6.204471E-05, 250000000 or Infinity."""

    __slots__ = ("literal",)
    literal: str

    def __init__(self, literal: str) -> None:
        if literal not in _NUMBER_WORDS and _NUMBER_PATTERN.fullmatch(literal) is None:
            raise ValueError(f"not a JSON number: {literal!r}")
        object.__setattr__(self, "literal", literal)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Number cannot be changed; put a new Number in its place")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Number) and self.literal == other.literal

    def __hash__(self) -> int:
        return hash((Number, self.literal))

    def __repr__(self) -> str:
        return f"Number({self.literal!r})"

    def __reduce__(self) -> Tuple[type, Tuple[str]]:
        return (Number, (self.literal,))


@dataclass
class NestedJson:
    """A text value whose content is another JSON document. `value` is that document, decoded."""

    __slots__ = ("value",)
    value: "JsonValue"


JsonValue = Union[None, bool, Number, str, NestedJson, List["JsonValue"], Dict[str, "JsonValue"]]

_encode_text = json.JSONEncoder(ensure_ascii=False).encode


def _load(text: str) -> JsonValue:
    try:
        return json.loads(text, parse_float=Number, parse_int=Number, parse_constant=Number)
    except RecursionError as error:
        raise SaveFormatError("JSON is nested too deeply to read") from error
    except ValueError as error:
        raise SaveFormatError(f"not valid JSON: {error}") from error


def _open_text(text: str) -> Union[str, NestedJson]:
    """Decode a text value that holds a JSON document. Anything else comes back unchanged."""
    if len(text) < 2 or text[0] not in "{[" or text[-1] not in "}]":
        return text
    try:
        inner = _open_nested(_load(text))
        rendered = render_json(inner)
    except (SaveFormatError, RecursionError):
        return text
    if rendered != text:
        # It parses, but not in a form this module writes. Keep it as plain text, untouched.
        return text
    return NestedJson(inner)


def holds_unread_document(text: str) -> bool:
    """Whether a text value holds a JSON document that decoding keeps as plain text.

    A text value becomes a NestedJson only when this module can write the document in it back
    identically. A JSON object or list with spaces between its values, with white space around
    it, with a repeated field name, or nested too deeply to read stays plain text, and nothing
    that reads the decoded save looks inside it. Text that is not JSON is ordinary text, also
    when it begins and ends like a document: "[Day 3] wolves at the dam [twice]".
    """
    document = text.strip(" \t\n\r")        # the white space JSON allows around a document
    if len(document) < 2 or document[0] not in "{[" or document[-1] not in "}]":
        return False
    try:
        _load(document)
    except SaveFormatError as error:
        # Not JSON, so not a document. JSON nested too deeply to read is one that stays unread.
        return isinstance(error.__cause__, RecursionError)
    return not isinstance(_open_text(text), NestedJson)


def _open_nested(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _open_text(value)
    if isinstance(value, list):
        for position, item in enumerate(value):
            if isinstance(item, (str, list, dict)):
                value[position] = _open_nested(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (str, list, dict)):
                value[key] = _open_nested(item)
    return value


def parse_json(text: str) -> JsonValue:
    """Parse a JSON document so that render_json gives back the identical text.

    Numbers become Number. Text values that hold another JSON document become NestedJson.
    Raises SaveFormatError when the document cannot be written back identically.
    """
    try:
        value = _open_nested(_load(text))
        rendered = render_json(value)
    except RecursionError as error:
        raise SaveFormatError("JSON is nested too deeply to read") from error
    if rendered != text:
        limit = min(len(rendered), len(text))
        position = next((i for i in range(limit) if rendered[i] != text[i]), limit)
        nearby = text[max(0, position - 20):position + 20]
        raise SaveFormatError(
            "valid JSON, but it cannot be written back identically "
            f"(first difference at character {position}: {nearby!r}). Usual causes: spaces between "
            "values, a repeated field name, or a text escape this tool does not write"
        )
    return value


def _render(value: JsonValue, parts: List[str]) -> None:
    if isinstance(value, str):
        parts.append(_encode_text(value))
    elif isinstance(value, Number):
        parts.append(value.literal)
    elif isinstance(value, dict):
        parts.append("{")
        first = True
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"field names must be text, not {type(key).__name__}")
            if not first:
                parts.append(",")
            first = False
            parts.append(_encode_text(key))
            parts.append(":")
            _render(item, parts)
        parts.append("}")
    elif isinstance(value, list):
        parts.append("[")
        for position, item in enumerate(value):
            if position:
                parts.append(",")
            _render(item, parts)
        parts.append("]")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif value is None:
        parts.append("null")
    elif isinstance(value, NestedJson):
        parts.append(_encode_text(render_json(value.value)))
    else:
        raise TypeError(
            f"cannot write a value of type {type(value).__name__}. Numbers must be Number objects"
        )


def render_json(value: JsonValue) -> str:
    """Write a value as JSON text, in the compact form the game uses."""
    parts: List[str] = []
    _render(value, parts)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Saves
# ---------------------------------------------------------------------------

@dataclass
class DecodedSave:
    """A save with every layer decoded.

    slot is the slot document. slot["m_Dict"] maps each entry name ("boot", "global",
    scene names) to its decoded document.
    """

    slot: Dict[str, JsonValue]

    @property
    def entries(self) -> Dict[str, JsonValue]:
        return cast(Dict[str, JsonValue], self.slot[ENTRY_TABLE])


def _decode_document(blob: bytes, where: str) -> JsonValue:
    try:
        text = lzf_decompress(blob).decode("utf-8")
    except UnicodeDecodeError as error:
        raise SaveFormatError(f"{where}: the decompressed data is not UTF-8 text") from error
    except SaveFormatError as error:
        raise SaveFormatError(f"{where}: {error}") from error
    try:
        return parse_json(text)
    except SaveFormatError as error:
        raise SaveFormatError(f"{where}: {error}") from error


def _encode_document(value: JsonValue) -> bytes:
    return lzf_compress(render_json(value).encode("utf-8"))


def decode_save(data: bytes) -> DecodedSave:
    """Decode the bytes of a save file.

    Raises SaveFormatError when any layer cannot be decoded exactly.
    """
    slot = _decode_document(data, "save file")
    if not isinstance(slot, dict) or not isinstance(slot.get(ENTRY_TABLE), dict):
        raise SaveFormatError(
            f"decoded, but there is no {ENTRY_TABLE} entry table, so this is not a TLD save")
    entries = slot[ENTRY_TABLE]
    for name, encoded in entries.items():
        if not isinstance(encoded, str):
            raise SaveFormatError(
                f"entry {name!r}: expected base64 text, found {type(encoded).__name__}")
        try:
            blob = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise SaveFormatError(f"entry {name!r}: not valid base64 ({error})") from error
        if base64.b64encode(blob).decode("ascii") != encoded:
            raise SaveFormatError(f"entry {name!r}: base64 is in a form this tool does not write")
        entries[name] = _decode_document(blob, f"entry {name!r}")
    return DecodedSave(slot)


def encode_save(save: DecodedSave) -> bytes:
    """Encode a decoded save back into the bytes of a save file. Every layer is written afresh."""
    slot = dict(save.slot)
    slot[ENTRY_TABLE] = {
        name: base64.b64encode(_encode_document(document)).decode("ascii")
        for name, document in save.entries.items()
    }
    return _encode_document(slot)


def _count_documents(value: JsonValue) -> int:
    """How many JSON documents are nested inside this value, as text."""
    if isinstance(value, NestedJson):
        return 1 + _count_documents(value.value)
    if isinstance(value, dict):
        return sum(_count_documents(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_documents(item) for item in value)
    return 0


@dataclass(frozen=True)
class VerifyResult:
    identical: bool   # the rebuilt save is byte for byte the original
    entries: int      # m_Dict entries decoded
    documents: int    # JSON documents decoded, nested ones included
    detail: str       # empty when identical, otherwise what is wrong


def _describe_difference(original: bytes, rebuilt: bytes) -> str:
    # decode_save already proved that every document's text is written back identically,
    # so a difference can only come from the compression.
    before = json.loads(lzf_decompress(original))[ENTRY_TABLE]
    after = json.loads(lzf_decompress(rebuilt))[ENTRY_TABLE]
    changed = [name for name, encoded in before.items() if after.get(name) != encoded]
    if changed:
        return "the content is identical, but the compressed bytes differ for: " + ", ".join(changed)
    return "the content is identical, but the compressed bytes of the slot document differ"


def verify_save(data: bytes) -> VerifyResult:
    """Decode the bytes of a save file, encode them again, and compare with the original."""
    try:
        save = decode_save(data)
    except SaveFormatError as error:
        return VerifyResult(False, 0, 0, f"cannot be decoded exactly: {error}")
    entries = len(save.entries)
    documents = 1 + entries + _count_documents(save.slot)
    rebuilt = encode_save(save)
    if rebuilt == data:
        return VerifyResult(True, entries, documents, "")
    return VerifyResult(False, entries, documents, _describe_difference(data, rebuilt))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _natural_key(path: Path) -> Tuple[str, int]:
    match = re.fullmatch(r"(.*?)([0-9]+)", path.name)
    return (match.group(1), int(match.group(2))) if match else (path.name, 0)


def find_saves(folder: Path) -> List[Path]:
    """Save files directly inside a folder, by the game's naming rule, in natural order."""
    saves = [path for path in folder.iterdir() if path.is_file() and SAVE_NAME.fullmatch(path.name)]
    return sorted(saves, key=_natural_key)


def _collect(paths: Sequence[Path]) -> Optional[List[Path]]:
    saves: List[Path] = []
    for path in paths:
        if path.is_dir():
            try:
                found = find_saves(path)
            except OSError as error:
                print(f"Could not list {path}: {error}", file=sys.stderr)
                return None
            if not found:
                print(f"No save files found in {path}", file=sys.stderr)
                return None
            saves.extend(found)
        elif path.is_file():
            saves.append(path)
        else:
            print(f"Not found: {path}", file=sys.stderr)
            return None
    return saves


def _run_verify(paths: Sequence[Path]) -> int:
    saves = _collect(paths)
    if saves is None:
        return 2
    width = max(len(save.name) for save in saves)
    identical = 0
    for save in saves:
        try:
            data = save.read_bytes()
        except OSError as error:
            print(f"{save.name:<{width}}  FAILED")
            print(f"    could not read the file: {error}")
            continue
        result = verify_save(data)
        status = "IDENTICAL" if result.identical else "FAILED"
        print(f"{save.name:<{width}}  {len(data):>11,} bytes  {result.entries:>3} entries  "
              f"{result.documents:>7,} documents  {status}", flush=True)
        if result.identical:
            identical += 1
        else:
            print(f"    {result.detail}")
    print(f"{identical} of {len(saves)} saves rebuild byte for byte.")
    return 0 if identical == len(saves) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decode a TLD save and encode it again, exactly. Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tld_save_codec.py verify sandbox1
  python tld_save_codec.py verify "$env:LOCALAPPDATA\\Hinterland\\TheLongDark\\Survival"

Exit codes: 0 every save was identical, 1 at least one was not, 2 a path was wrong.
        """,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    verify = commands.add_parser(
        "verify", help="check that a save can be decoded and rebuilt byte for byte")
    verify.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                        help="a save file, or a folder that holds save files")
    args = parser.parse_args(argv)
    return _run_verify(args.paths)


if __name__ == "__main__":
    sys.exit(main())
