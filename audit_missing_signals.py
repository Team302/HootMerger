#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CONTROL_START = 0
_SUFFIX_RE = re.compile(r"-(\d+)$")


@dataclass(frozen=True)
class SignalEntry:
    source_file: str
    signal_name: str
    type_name: str
    metadata: str


def _read_len_string(buf: bytes, pos: int) -> tuple[str, int]:
    if pos + 4 > len(buf):
        raise ValueError("invalid string length field")
    length = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    end = pos + length
    if end > len(buf):
        raise ValueError("invalid string payload")
    return buf[pos:end].decode("utf-8", errors="replace"), end


def _parse_wpilog_entries(wpilog_path: Path, source_file: str) -> list[SignalEntry]:
    entries: dict[int, SignalEntry] = {}

    with wpilog_path.open("rb") as fh:
        if fh.read(6) != b"WPILOG":
            raise ValueError(f"{wpilog_path} is not a WPILOG file")

        fh.read(2)
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

            entry_id = int.from_bytes(fh.read(entry_len), "little", signed=False)
            payload_size = int.from_bytes(fh.read(size_len), "little", signed=False)
            fh.read(ts_len)
            payload = fh.read(payload_size)

            if entry_id != 0 or len(payload) < 5:
                continue

            if payload[0] != CONTROL_START:
                continue

            started_entry_id = struct.unpack_from("<I", payload, 1)[0]
            pos = 5
            try:
                signal_name, pos = _read_len_string(payload, pos)
                type_name, pos = _read_len_string(payload, pos)
                metadata, pos = _read_len_string(payload, pos)
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


def _find_owlet(executable: str | None) -> str | None:
    if executable:
        p = Path(executable)
        if p.exists():
            return str(p)

    local = sorted(Path.cwd().glob("owlet*.exe"))
    if local:
        return str(local[0])
    return None


def _convert_hoot_to_wpilog(hoot_path: Path, owlet_exe: str) -> Path | None:
    temp_output = Path(tempfile.gettempdir()) / f"{hoot_path.stem}.audit.wpilog"
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


def _collect_source_entries(inputs: list[Path], owlet_exe: str | None) -> list[SignalEntry]:
    all_entries: list[SignalEntry] = []

    for path in inputs:
        if not path.exists():
            print(f"[WARN] Missing file: {path}", file=sys.stderr)
            continue

        source_name = path.name
        suffix = path.suffix.lower()

        if suffix == ".wpilog":
            try:
                all_entries.extend(_parse_wpilog_entries(path, source_name))
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

            converted = _convert_hoot_to_wpilog(path, owlet_exe)
            if converted is None:
                continue

            try:
                all_entries.extend(_parse_wpilog_entries(converted, source_name))
            except Exception as exc:
                print(f"[WARN] Could not parse converted output for {path}: {exc}", file=sys.stderr)
            continue

        print(f"[WARN] Unsupported input extension for {path} (expected .hoot or .wpilog)", file=sys.stderr)

    unique = {(e.source_file, e.signal_name, e.type_name, e.metadata): e for e in all_entries}
    return sorted(unique.values(), key=lambda e: (e.source_file.lower(), e.signal_name.lower(), e.type_name.lower()))


def _normalize_merged_name(name: str) -> str:
    return _SUFFIX_RE.sub("", name)


def _write_missing_csv(missing: list[SignalEntry], output_csv: Path) -> None:
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_file", "signal_name", "type", "metadata", "reason"])
        for item in missing:
            writer.writerow([item.source_file, item.signal_name, item.type_name, item.metadata, "missing_in_merged"])


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit source logs and output CSV of signals missing from a merged WPILog"
    )
    parser.add_argument("inputs", nargs="+", help="Input .hoot and/or .wpilog source files")
    parser.add_argument("--merged", required=True, help="Merged .wpilog file to audit against")
    parser.add_argument("-o", "--output", required=True, help="Output CSV path for missing signals")
    parser.add_argument(
        "--owlet",
        default=None,
        help="Path to owlet executable (if omitted, searches current directory for owlet*.exe)",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    input_paths = [Path(p) for p in args.inputs]
    merged_path = Path(args.merged)
    output_csv = Path(args.output)

    if not merged_path.exists():
        print(f"[ERROR] Merged file not found: {merged_path}", file=sys.stderr)
        return 2

    owlet_exe = _find_owlet(args.owlet)
    source_entries = _collect_source_entries(input_paths, owlet_exe)
    if not source_entries:
        print("[ERROR] No source signal entries found.", file=sys.stderr)
        return 3

    try:
        merged_entries = _parse_wpilog_entries(merged_path, merged_path.name)
    except Exception as exc:
        print(f"[ERROR] Could not parse merged file {merged_path}: {exc}", file=sys.stderr)
        return 4

    merged_names = {_normalize_merged_name(e.signal_name) for e in merged_entries}

    missing = [e for e in source_entries if e.signal_name not in merged_names]
    _write_missing_csv(missing, output_csv)

    print(f"Source entries: {len(source_entries)}")
    print(f"Merged entries: {len(merged_entries)}")
    print(f"Missing entries: {len(missing)}")
    print(f"Wrote audit CSV: {output_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
