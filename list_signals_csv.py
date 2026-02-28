#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CONTROL_START = 0


@dataclass(frozen=True)
class SignalEntry:
    source_file: str
    signal_name: str
    type_name: str
    metadata: str


def read_len_string(buf: bytes, pos: int) -> tuple[str, int]:
    if pos + 4 > len(buf):
        raise ValueError("invalid string length field")
    length = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    end = pos + length
    if end > len(buf):
        raise ValueError("invalid string payload")
    return buf[pos:end].decode("utf-8", errors="replace"), end


def parse_wpilog_entries(wpilog_path: Path, source_file: str) -> list[SignalEntry]:
    entries: dict[int, SignalEntry] = {}

    with wpilog_path.open("rb") as fh:
        if fh.read(6) != b"WPILOG":
            raise ValueError(f"{wpilog_path} is not a WPILOG file")

        fh.read(2)  # version
        extra_len = struct.unpack("<I", fh.read(4))[0]
        fh.read(extra_len)

        while True:
            first = fh.read(1)
            if not first:
                break

            bitfield = first[0]
            entry_len = (bitfield & 0x3) + 1
            size_len = ((bitfield >> 2) & 0x3) + 1
            ts_len = ((bitfield >> 4) & 0x7) + 1

            entry_id = int.from_bytes(fh.read(entry_len), byteorder="little", signed=False)
            payload_size = int.from_bytes(fh.read(size_len), byteorder="little", signed=False)
            fh.read(ts_len)
            payload = fh.read(payload_size)

            if entry_id != 0 or len(payload) < 5:
                continue

            control_type = payload[0]
            if control_type != CONTROL_START:
                continue

            started_entry_id = struct.unpack_from("<I", payload, 1)[0]
            pos = 5
            try:
                signal_name, pos = read_len_string(payload, pos)
                type_name, pos = read_len_string(payload, pos)
                metadata, pos = read_len_string(payload, pos)
            except ValueError:
                continue

            entries[started_entry_id] = SignalEntry(
                source_file=source_file,
                signal_name=signal_name,
                type_name=type_name,
                metadata=metadata,
            )

    unique = {(e.source_file, e.signal_name, e.type_name, e.metadata): e for e in entries.values()}
    return sorted(unique.values(), key=lambda e: (e.source_file.lower(), e.signal_name.lower(), e.type_name.lower()))


def find_owlet(executable: str | None) -> str | None:
    if executable:
        p = Path(executable)
        if p.exists():
            return str(p)
    local = sorted(Path.cwd().glob("owlet*.exe"))
    if local:
        return str(local[0])
    return None


def convert_hoot_to_wpilog(hoot_path: Path, owlet_exe: str) -> Path | None:
    temp_output = Path(tempfile.gettempdir()) / f"{hoot_path.stem}.signals.wpilog"
    if temp_output.exists():
        temp_output.unlink()

    cmd = [owlet_exe, str(hoot_path), str(temp_output), "--format=wpilog", "--unlicensed"]
    run = subprocess.run(cmd, capture_output=True, text=True)

    if not temp_output.exists() or temp_output.stat().st_size == 0:
        detail = run.stderr.strip() or run.stdout.strip() or f"exit code {run.returncode}"
        print(f"[WARN] Failed to convert {hoot_path}: {detail}", file=sys.stderr)
        return None

    if run.returncode != 0:
        print(
            f"[WARN] owlet returned non-zero for {hoot_path}, but output was produced; continuing.",
            file=sys.stderr,
        )

    return temp_output


def collect_entries(inputs: list[Path], owlet_exe: str | None) -> list[SignalEntry]:
    all_entries: list[SignalEntry] = []

    for path in inputs:
        if not path.exists():
            print(f"[WARN] Missing file: {path}", file=sys.stderr)
            continue

        source_name = path.name
        suffix = path.suffix.lower()

        if suffix == ".wpilog":
            try:
                all_entries.extend(parse_wpilog_entries(path, source_name))
            except Exception as exc:
                print(f"[WARN] Could not parse {path}: {exc}", file=sys.stderr)
            continue

        if suffix == ".hoot":
            if owlet_exe is None:
                print(
                    f"[WARN] No owlet executable found; cannot parse {path}. Use --owlet or place owlet*.exe in the folder.",
                    file=sys.stderr,
                )
                continue

            converted = convert_hoot_to_wpilog(path, owlet_exe)
            if converted is None:
                continue

            try:
                all_entries.extend(parse_wpilog_entries(converted, source_name))
            except Exception as exc:
                print(f"[WARN] Could not parse converted output for {path}: {exc}", file=sys.stderr)
            continue

        print(f"[WARN] Unsupported input extension for {path} (expected .hoot or .wpilog)", file=sys.stderr)

    unique = {(e.source_file, e.signal_name, e.type_name, e.metadata): e for e in all_entries}
    return sorted(unique.values(), key=lambda e: (e.source_file.lower(), e.signal_name.lower(), e.type_name.lower()))


def write_csv(entries: list[SignalEntry], output_csv: Path) -> None:
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_file", "signal_name", "type", "metadata"])
        for e in entries:
            writer.writerow([e.source_file, e.signal_name, e.type_name, e.metadata])


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List all signals and types from .hoot/.wpilog files into a CSV"
    )
    parser.add_argument("inputs", nargs="+", help="Input .hoot and/or .wpilog files")
    parser.add_argument("-o", "--output", required=True, help="Output CSV path")
    parser.add_argument(
        "--owlet",
        default=None,
        help="Path to owlet executable (if omitted, searches current directory for owlet*.exe)",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    input_paths = [Path(p) for p in args.inputs]
    output_csv = Path(args.output)
    owlet_exe = find_owlet(args.owlet)

    entries = collect_entries(input_paths, owlet_exe)
    if not entries:
        print("[ERROR] No signal entries found.", file=sys.stderr)
        return 2

    write_csv(entries, output_csv)
    print(f"Wrote {len(entries)} entries to {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
