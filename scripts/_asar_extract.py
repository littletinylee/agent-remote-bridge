"""Extract selected packages out of an Electron .asar archive (dev diagnostic).

    python scripts/_asar_extract.py <asar> <needle> [<outdir>]

Prints the file list of any package whose path contains <needle> and, when an
output dir is given, writes the files there so they can be read.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def load_header(path: Path) -> tuple[dict, int]:
    """Electron asar layout: u32 4 | u32 pickle_size | u32 json_size | json.

    The JSON begins at byte 16 and the data section at 8 + pickle_size (the
    json_size field includes a little padding, hence raw_decode).
    """
    with path.open("rb") as fh:
        head = fh.read(12)
        _, pickle_size, json_size = struct.unpack("<III", head)
        fh.seek(16)
        raw = fh.read(json_size + 64)
    obj, consumed = json.JSONDecoder().raw_decode(raw.decode("utf-8"))
    return obj, 8 + pickle_size


def walk(node: dict, prefix: str, needle: str, out: list[tuple[str, dict]]) -> None:
    for name, child in (node.get("files") or {}).items():
        path = f"{prefix}/{name}" if prefix else name
        if "files" in child:
            walk(child, path, needle, out)
        elif needle.lower() in path.lower():
            out.append((path, child))


def main() -> int:
    asar = Path(sys.argv[1])
    needle = sys.argv[2] if len(sys.argv) > 2 else "dsh-mcp-client"
    outdir = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    header, base = load_header(asar)
    found: list[tuple[str, dict]] = []
    walk(header, "", needle, found)
    print(f"{len(found)} file(s) matching {needle!r}")
    for path, meta in found:
        print(f"  {meta.get('size', 0):>8}  {path}")
    if outdir is None:
        return 0
    with asar.open("rb") as fh:
        for path, meta in found:
            if "offset" not in meta:
                continue
            fh.seek(base + int(meta["offset"]))
            blob = fh.read(int(meta["size"]))
            dest = outdir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
    print(f"extracted to {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
