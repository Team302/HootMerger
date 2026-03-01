# ====================================================================================================================================================
# Copyright 2026 Lake Orion Robotics FIRST Team 302
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
# OR OTHER DEALINGS IN THE SOFTWARE.
# ====================================================================================================================================================
#!/usr/bin/env python3
# pyright: reportMissingImports=false
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

if "CTR_TARGET" not in os.environ:
    os.environ["CTR_TARGET"] = "Replay"

try:
    from phoenix6.hoot_replay import HootReplay  # type: ignore[import-not-found]
except ModuleNotFoundError:
    HootReplay = None


CONTROL_START = 0
CONTROL_FINISH = 1
CONTROL_SET_METADATA = 2
SUPPORTED_WPILOG_TYPES = {"double", "float", "int64", "boolean", "string", "double[]", "float[]", "raw"}
GAP_WARNING_US = 500_000


def _run_subprocess_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


@dataclass(frozen=True)
class Sample:
    name: str
    type_name: str
    timestamp_us: int
    value: Any


@dataclass(frozen=True)
class FileReadResult:
    source_path: Path
    samples: list[Sample]


def _should_force_boolean(signal_name: str) -> bool:
    low = signal_name.lower()
    if not low.startswith("phoenix6/"):
        return False

    if "fault" in low:
        return True

    if "stickyfault" in low or "fieldfault" in low:
        return True

    if "isproenable" in low or "isproenabled" in low:
        return True

    if "isprolicensed" in low:
        return True

    if low.endswith("/s1closed") or low.endswith("/s2closed"):
        return True

    return False


def _coerce_scalar_to_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return float(value) != 0.0

    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "t", "1", "yes", "y", "enabled", "enable", "on"}:
            return True
        if low in {"false", "f", "0", "no", "n", "disabled", "disable", "off"}:
            return False

    return None


class WPILogWriter:
    def __init__(self, file_path: Path, metadata: str = "") -> None:
        self._fh = file_path.open("wb")
        self._write_header(metadata)

    def close(self) -> None:
        self._fh.close()

    def _write_header(self, metadata: str) -> None:
        metadata_bytes = metadata.encode("utf-8")
        self._fh.write(b"WPILOG")
        self._fh.write(struct.pack("<H", 0x0100))
        self._fh.write(struct.pack("<I", len(metadata_bytes)))
        self._fh.write(metadata_bytes)

    @staticmethod
    def _encode_uvarint(value: int, width: int) -> bytes:
        return value.to_bytes(width, byteorder="little", signed=False)

    @staticmethod
    def _width_for_unsigned(value: int, max_width: int) -> int:
        for width in range(1, max_width + 1):
            if value < (1 << (8 * width)):
                return width
        raise ValueError(f"value {value} exceeds {max_width} bytes")

    def _write_record(self, entry_id: int, timestamp_us: int, payload: bytes) -> None:
        if entry_id < 0:
            raise ValueError("entry_id must be >= 0")
        if timestamp_us < 0:
            raise ValueError("timestamp_us must be >= 0")

        entry_width = self._width_for_unsigned(entry_id, 4)
        size_width = self._width_for_unsigned(len(payload), 4)
        timestamp_width = self._width_for_unsigned(timestamp_us, 8)

        header_len_bitfield = (
            (entry_width - 1)
            | ((size_width - 1) << 2)
            | ((timestamp_width - 1) << 4)
        )

        self._fh.write(bytes([header_len_bitfield]))
        self._fh.write(self._encode_uvarint(entry_id, entry_width))
        self._fh.write(self._encode_uvarint(len(payload), size_width))
        self._fh.write(self._encode_uvarint(timestamp_us, timestamp_width))
        self._fh.write(payload)

    @staticmethod
    def _encode_len_string(value: str) -> bytes:
        raw = value.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    def start_entry(self, name: str, type_name: str, metadata: str, entry_id: int, timestamp_us: int = 0) -> None:
        payload = bytearray()
        payload.append(CONTROL_START)
        payload.extend(struct.pack("<I", entry_id))
        payload.extend(self._encode_len_string(name))
        payload.extend(self._encode_len_string(type_name))
        payload.extend(self._encode_len_string(metadata))
        self._write_record(0, timestamp_us, bytes(payload))

    def append_record(self, entry_id: int, timestamp_us: int, type_name: str, value: Any) -> None:
        is_raw_like = (
            type_name == "raw"
            or type_name.startswith("struct:")
            or type_name.startswith("proto:")
            or type_name in {"json", "msgpack"}
        )
        if type_name not in SUPPORTED_WPILOG_TYPES and not is_raw_like:
            raise ValueError(f"unsupported WPILog type: {type_name}")

        if type_name == "double":
            payload = struct.pack("<d", float(value))
        elif type_name == "float":
            payload = struct.pack("<f", float(value))
        elif type_name == "int64":
            payload = struct.pack("<q", int(value))
        elif type_name == "boolean":
            payload = b"\x01" if bool(value) else b"\x00"
        elif type_name == "string":
            payload = str(value).encode("utf-8")
        elif type_name == "double[]":
            payload = b"".join(struct.pack("<d", float(v)) for v in value)
        elif type_name == "float[]":
            payload = b"".join(struct.pack("<f", float(v)) for v in value)
        elif is_raw_like:
            if isinstance(value, bytes):
                payload = value
            elif isinstance(value, bytearray):
                payload = bytes(value)
            else:
                raise ValueError(f"raw-like type '{type_name}' requires bytes payload")
        else:
            raise ValueError(f"unhandled type: {type_name}")

        self._write_record(entry_id, timestamp_us, payload)


class WPILogReader:
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path

    @staticmethod
    def _read_exact(fh: Any, count: int) -> bytes:
        data = fh.read(count)
        if len(data) != count:
            raise EOFError("unexpected end of file")
        return data

    @staticmethod
    def _read_uvarint(data: bytes) -> int:
        return int.from_bytes(data, byteorder="little", signed=False)

    @staticmethod
    def _read_len_string(buf: bytes, pos: int) -> tuple[str, int]:
        if pos + 4 > len(buf):
            raise ValueError("invalid string length field")
        length = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        end = pos + length
        if end > len(buf):
            raise ValueError("invalid string contents")
        return buf[pos:end].decode("utf-8", errors="replace"), end

    @staticmethod
    def _decode_value(signal_name: str, type_name: str, payload: bytes) -> tuple[str, Any] | None:
        if type_name == "double":
            if len(payload) != 8:
                return None
            return ("double", struct.unpack("<d", payload)[0])
        if type_name == "float":
            if len(payload) != 4:
                return None
            return ("float", struct.unpack("<f", payload)[0])
        if type_name == "int64":
            if len(payload) != 8:
                return None
            return ("int64", struct.unpack("<q", payload)[0])
        if type_name == "boolean":
            if len(payload) != 1:
                return None
            return ("boolean", payload[0] != 0)
        if type_name == "string":
            return ("string", payload.decode("utf-8", errors="replace"))
        if type_name in {"json", "msgpack", "raw"}:
            return (type_name, payload)
        if type_name == "double[]":
            if (len(payload) % 8) != 0:
                return None
            return ("double[]", [
                struct.unpack_from("<d", payload, idx)[0]
                for idx in range(0, len(payload), 8)
            ])
        if type_name == "float[]":
            if (len(payload) % 4) != 0:
                return None
            return ("float[]", [
                struct.unpack_from("<f", payload, idx)[0]
                for idx in range(0, len(payload), 4)
            ])
        if type_name == "boolean[]":
            return ("double[]", [1.0 if b != 0 else 0.0 for b in payload])
        if type_name == "int64[]":
            if (len(payload) % 8) != 0:
                return None
            return ("double[]", [
                float(struct.unpack_from("<q", payload, idx)[0])
                for idx in range(0, len(payload), 8)
            ])
        if type_name == "string[]":
            if len(payload) < 4:
                return None
            count = struct.unpack_from("<I", payload, 0)[0]
            pos = 4
            values: list[str] = []
            for _ in range(count):
                if pos + 4 > len(payload):
                    return None
                item_len = struct.unpack_from("<I", payload, pos)[0]
                pos += 4
                end = pos + item_len
                if end > len(payload):
                    return None
                values.append(payload[pos:end].decode("utf-8", errors="replace"))
                pos = end
            return ("string", ",".join(values))

        if type_name.startswith("struct:") or type_name.startswith("proto:"):
            return (type_name, payload)

        return ("raw", payload)

    def read_samples(self) -> list[Sample]:
        entries: dict[int, tuple[str, str, str]] = {}
        samples: list[Sample] = []

        with self._file_path.open("rb") as fh:
            if self._read_exact(fh, 6) != b"WPILOG":
                raise ValueError(f"{self._file_path} is not a WPILOG file")
            _version = struct.unpack("<H", self._read_exact(fh, 2))[0]
            extra_len = struct.unpack("<I", self._read_exact(fh, 4))[0]
            _ = self._read_exact(fh, extra_len)

            while True:
                first = fh.read(1)
                if not first:
                    break

                bitfield = first[0]
                entry_len = (bitfield & 0x3) + 1
                size_len = ((bitfield >> 2) & 0x3) + 1
                ts_len = ((bitfield >> 4) & 0x7) + 1

                entry = self._read_uvarint(self._read_exact(fh, entry_len))
                size = self._read_uvarint(self._read_exact(fh, size_len))
                timestamp_us = self._read_uvarint(self._read_exact(fh, ts_len))
                payload = self._read_exact(fh, size)

                if entry == 0:
                    if len(payload) < 1:
                        continue
                    control_type = payload[0]
                    if control_type == CONTROL_START and len(payload) >= 5:
                        entry_id = struct.unpack_from("<I", payload, 1)[0]
                        pos = 5
                        try:
                            name, pos = self._read_len_string(payload, pos)
                            type_name, pos = self._read_len_string(payload, pos)
                            metadata, pos = self._read_len_string(payload, pos)
                        except ValueError:
                            continue
                        entries[entry_id] = (name, type_name, metadata)
                    elif control_type == CONTROL_FINISH and len(payload) >= 5:
                        entry_id = struct.unpack_from("<I", payload, 1)[0]
                        entries.pop(entry_id, None)
                    elif control_type == CONTROL_SET_METADATA and len(payload) >= 9:
                        entry_id = struct.unpack_from("<I", payload, 1)[0]
                        if entry_id in entries:
                            old_name, old_type, _old_meta = entries[entry_id]
                            try:
                                new_meta, _ = self._read_len_string(payload, 5)
                            except ValueError:
                                continue
                            entries[entry_id] = (old_name, old_type, new_meta)
                    continue

                if entry not in entries:
                    continue

                name, type_name, _metadata = entries[entry]
                decoded_result = self._decode_value(name, type_name, payload)
                if decoded_result is None:
                    continue
                mapped_type_name, decoded_value = decoded_result

                if _should_force_boolean(name):
                    coerced = _coerce_scalar_to_boolean(decoded_value)
                    if coerced is not None:
                        mapped_type_name = "boolean"
                        decoded_value = coerced

                samples.append(
                    Sample(
                        name=name,
                        type_name=mapped_type_name,
                        timestamp_us=int(timestamp_us),
                        value=decoded_value,
                    )
                )

        return samples


class PhoenixHootExtractor:
    _OWLET_INDEX_URL = "https://redist.ctr-electronics.com/index.json"
    _OWLET_VERSION_COMPLIANCY: dict[str, int] | None = None
    _OWLET_INDEX_ATTEMPTED = False

    _GETTER_SPECS: list[tuple[str, str]] = [
        ("double", "get_double"),
        ("float", "get_float"),
        ("int64", "get_integer"),
        ("string", "get_string"),
        ("double[]", "get_double_array"),
        ("float[]", "get_float_array"),
        ("raw", "get_raw"),
        ("boolean", "get_boolean"),
        ("boolean[]", "get_boolean_array"),
        ("int64[]", "get_integer_array"),
        ("string[]", "get_string_array"),
    ]

    def __init__(
        self,
        step_seconds: float = 0.02,
        max_steps: int = 2_000_000,
        parser_mode: str = "auto",
        owlet_executable: str | None = None,
        strict_owlet_match: bool = False,
    ) -> None:
        self._step_seconds = step_seconds
        self._max_steps = max_steps
        self._parser_mode = parser_mode
        self._owlet_executable = owlet_executable
        self._strict_owlet_match = strict_owlet_match

    @staticmethod
    def _extract_version_from_owlet_name(path: Path) -> str | None:
        match = re.search(r"owlet-(\d+\.\d+\.\d+)", path.name, flags=re.IGNORECASE)
        if not match:
            return None
        return match.group(1)

    @classmethod
    def _load_owlet_index_if_needed(cls) -> None:
        if cls._OWLET_INDEX_ATTEMPTED:
            return
        cls._OWLET_INDEX_ATTEMPTED = True
        cls._OWLET_VERSION_COMPLIANCY = {}

        try:
            with urllib.request.urlopen(cls._OWLET_INDEX_URL, timeout=8) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return

        try:
            tools = data.get("Tools", [])
            owlet_tool = next(tool for tool in tools if tool.get("Name") == "owlet")
            items = owlet_tool.get("Items", [])
        except (StopIteration, AttributeError):
            return

        for item in items:
            version = item.get("Version")
            compliancy = item.get("Compliancy")
            if isinstance(version, str) and isinstance(compliancy, int):
                cls._OWLET_VERSION_COMPLIANCY[version] = compliancy

    @classmethod
    def _extract_compliancy_from_owlet_name(cls, path: Path) -> int | None:
        match = re.search(r"-C(\d+)", path.name, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None

        version = cls._extract_version_from_owlet_name(path)
        if version is None:
            return None

        cls._load_owlet_index_if_needed()
        if cls._OWLET_VERSION_COMPLIANCY is None:
            return None
        return cls._OWLET_VERSION_COMPLIANCY.get(version)

    @classmethod
    def _format_available_owlet_versions(cls, candidates: list[Path]) -> str:
        if not candidates:
            return "none found"

        labels: list[str] = []
        for candidate in candidates:
            compliancy = cls._extract_compliancy_from_owlet_name(candidate)
            if compliancy is None:
                labels.append(candidate.name)
            else:
                labels.append(f"{candidate.name} (C{compliancy})")
        return ", ".join(labels)

    @staticmethod
    def _read_hoot_compliancy(hoot_path: Path) -> int | None:
        try:
            with hoot_path.open("rb") as fh:
                fh.seek(70)
                data = fh.read(2)
            if len(data) < 1:
                return None
            return data[0]
        except OSError:
            return None

    @classmethod
    def _find_owlet_executable(
        cls,
        hoot_path: Path,
        explicit_path: str | None = None,
        strict_match: bool = False,
    ) -> str | None:
        compliancy = cls._read_hoot_compliancy(hoot_path)
        required_str = f"C{compliancy}" if compliancy is not None else "unknown compliancy"

        if explicit_path:
            p = Path(explicit_path)
            if p.is_file():
                parsed = cls._extract_compliancy_from_owlet_name(p)
                if compliancy is not None:
                    if parsed is None:
                        print(
                            f"[WARN] Could not verify compliancy from explicit owlet filename: {p}. {hoot_path} requires {required_str}.",
                            file=sys.stderr,
                        )
                        if strict_match:
                            print(
                                f"[WARN] strict owlet matching is enabled; refusing unverified owlet executable: {p}",
                                file=sys.stderr,
                            )
                            return None
                    elif parsed != compliancy:
                        print(
                            f"[WARN] {hoot_path} requires owlet compliancy {required_str}, but explicit owlet is C{parsed}: {p}",
                            file=sys.stderr,
                        )
                        if strict_match:
                            print(
                                "[WARN] strict owlet matching is enabled; refusing mismatched owlet executable.",
                                file=sys.stderr,
                            )
                            return None
                return str(p)
            if p.is_dir():
                all_candidates = sorted(p.glob("owlet*.exe"))
                if compliancy is not None:
                    matches = [
                        candidate
                        for candidate in all_candidates
                        if cls._extract_compliancy_from_owlet_name(candidate) == compliancy
                    ]
                    if matches:
                        return str(matches[0])
                    print(
                        f"[WARN] {hoot_path} requires owlet compliancy {required_str}, but no matching binary was found in {p}. "
                        f"Available: {cls._format_available_owlet_versions(all_candidates)}",
                        file=sys.stderr,
                    )
                    if strict_match:
                        print(
                            "[WARN] strict owlet matching is enabled; aborting owlet selection.",
                            file=sys.stderr,
                        )
                        return None
                any_matches = all_candidates
                if any_matches:
                    return str(any_matches[0])
                print(
                    f"[WARN] No owlet binaries found in {p}. {hoot_path} needs owlet compliancy {required_str}.",
                    file=sys.stderr,
                )

        cwd = Path.cwd()
        cwd_candidates = sorted(cwd.glob("owlet*.exe"))
        if compliancy is not None:
            matches = [
                candidate
                for candidate in cwd_candidates
                if cls._extract_compliancy_from_owlet_name(candidate) == compliancy
            ]
            if matches:
                return str(matches[0])
            if cwd_candidates:
                print(
                    f"[WARN] {hoot_path} requires owlet compliancy {required_str}, but no matching local binary was found. "
                    f"Available: {cls._format_available_owlet_versions(cwd_candidates)}",
                    file=sys.stderr,
                )
                if strict_match:
                    print(
                        "[WARN] strict owlet matching is enabled; aborting owlet selection.",
                        file=sys.stderr,
                    )
                    return None
        local = sorted(cwd.glob("owlet*.exe"))
        if local:
            return str(local[0])
        if compliancy is not None:
            print(
                f"[WARN] No owlet binary found. {hoot_path} needs owlet compliancy {required_str}.",
                file=sys.stderr,
            )
        return None

    @staticmethod
    def _scan_owlet_signal_ids(owlet: str, hoot_path: Path) -> list[str]:
        scan_cmd = [owlet, str(hoot_path), os.devnull, "--scan", "--full-scan"]
        run = _run_subprocess_capture(scan_cmd)
        if run.returncode != 0:
            return []

        text = (run.stdout or "") + "\n" + (run.stderr or "")
        signal_ids: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            match = re.search(r":\s*([0-9a-fA-F]+)\s*$", line)
            if not match:
                continue
            signal_id = match.group(1).lower()
            if signal_id not in seen:
                seen.add(signal_id)
                signal_ids.append(signal_id)
        return signal_ids

    def _read_file_with_owlet(self, hoot_path: Path) -> FileReadResult | None:
        owlet = self._find_owlet_executable(
            hoot_path,
            self._owlet_executable,
            strict_match=self._strict_owlet_match,
        )
        if owlet is None:
            return None

        compliancy = self._read_hoot_compliancy(hoot_path)
        if compliancy is not None and compliancy < 6:
            print(
                f"[WARN] {hoot_path} appears too old for modern owlet decoding (compliancy {compliancy})",
                file=sys.stderr,
            )

        check_pro_cmd = [owlet, str(hoot_path), "--check-pro"]
        check_pro_run = _run_subprocess_capture(check_pro_cmd)
        if check_pro_run.returncode == 0:
            out = (check_pro_run.stdout or "")
            if "NOT" in out:
                print(
                    f"[WARN] {hoot_path} may include limited Phoenix signals (non-Pro content reported by owlet).",
                    file=sys.stderr,
                )

        temp_output = Path(tempfile.gettempdir()) / f"{hoot_path.stem}.owlet.wpilog"
        cmd = [owlet, str(hoot_path), str(temp_output), "-f", "wpilog"]

        run = _run_subprocess_capture(cmd)
        if run.returncode != 0:
            fallback_cmd = [owlet, str(hoot_path), str(temp_output), "--format=wpilog", "--unlicensed"]
            run = _run_subprocess_capture(fallback_cmd)

        if not temp_output.exists() or temp_output.stat().st_size == 0:
            if run.returncode != 0:
                print(
                    f"[WARN] owlet conversion failed for {hoot_path}: {run.stderr.strip() or run.stdout.strip()}",
                    file=sys.stderr,
                )
            print(f"[WARN] owlet produced no output for {hoot_path}", file=sys.stderr)
            return None

        if run.returncode != 0:
            print(
                f"[WARN] owlet reported non-zero exit for {hoot_path}, but output was produced; continuing.",
                file=sys.stderr,
            )

        try:
            samples = WPILogReader(temp_output).read_samples()
        except Exception as exc:
            print(f"[WARN] Could not parse owlet output for {hoot_path}: {exc}", file=sys.stderr)
            return None

        if not samples:
            print(f"[WARN] No readable signals found in {hoot_path} via owlet", file=sys.stderr)
        return FileReadResult(source_path=hoot_path, samples=samples)

    @classmethod
    def _iter_getters(cls) -> Iterable[tuple[str, Callable[[str], Any]]]:
        if HootReplay is None:
            return []
        getters: list[tuple[str, Callable[[str], Any]]] = []
        for internal_type, getter_name in cls._GETTER_SPECS:
            getter = getattr(HootReplay, getter_name, None)
            if callable(getter):
                getters.append((internal_type, getter))
        return getters

    @staticmethod
    def _extract_candidate_signal_names(file_path: Path) -> list[str]:
        data = file_path.read_bytes()
        tokens = re.findall(rb"[\x20-\x7E]{4,}", data)

        candidates: set[str] = set()
        for token in tokens:
            try:
                text = token.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError:
                continue

            if len(text) < 4 or len(text) > 160:
                continue
            if " " in text:
                continue
            if "\x00" in text:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_./:\-\[\]()]+", text):
                continue
            if not any(ch.isalpha() for ch in text):
                continue
            if text.isupper() and len(text) > 24:
                continue

            candidates.add(text)

        return sorted(candidates)

    @staticmethod
    def _to_fpga_us(timestamp_seconds: float) -> int:
        return int(round(float(timestamp_seconds) * 1_000_000.0))

    def _probe_signal(self, name: str) -> tuple[str, Callable[[str], Any]] | None:
        for internal_type, getter in self._iter_getters():
            measurement = getter(name)
            if measurement.status.is_ok():
                return internal_type, getter
        return None

    @staticmethod
    def _map_to_wpilog_type(signal_name: str, internal_type: str, value: Any) -> tuple[str, Any] | None:
        if internal_type in {"double", "float"}:
            mapped = ("double", float(value))
        elif internal_type == "int64":
            mapped = ("int64", int(value))
        elif internal_type == "string":
            mapped = ("string", str(value))
        elif internal_type == "double[]":
            mapped = ("double[]", [float(v) for v in value])
        elif internal_type == "float[]":
            mapped = ("float[]", [float(v) for v in value])
        elif internal_type == "boolean":
            mapped = ("boolean", bool(value))
        elif internal_type == "boolean[]":
            mapped = ("double[]", [1.0 if bool(v) else 0.0 for v in value])
        elif internal_type == "int64[]":
            mapped = ("double[]", [float(v) for v in value])
        elif internal_type == "string[]":
            mapped = ("string", ",".join(str(v) for v in value))
        else:
            return None

        if _should_force_boolean(signal_name):
            coerced = _coerce_scalar_to_boolean(mapped[1])
            if coerced is not None:
                return ("boolean", coerced)

        return mapped

    def read_file(self, hoot_path: Path) -> FileReadResult:
        if self._parser_mode in {"owlet", "auto"}:
            owlet_result = self._read_file_with_owlet(hoot_path)
            if owlet_result is not None:
                return owlet_result
            if self._parser_mode == "owlet":
                return FileReadResult(source_path=hoot_path, samples=[])

        samples: list[Sample] = []

        try:
            load_status = HootReplay.load_file(str(hoot_path))
        except Exception as exc:
            print(f"[WARN] Could not open {hoot_path}: {exc}", file=sys.stderr)
            return FileReadResult(source_path=hoot_path, samples=[])

        if not load_status.is_ok() or not HootReplay.is_file_loaded():
            print(
                f"[WARN] Could not open {hoot_path}: load status {load_status.name}",
                file=sys.stderr,
            )
            HootReplay.close_file()
            return FileReadResult(source_path=hoot_path, samples=[])

        HootReplay.stop()
        HootReplay.pause()

        candidate_names = self._extract_candidate_signal_names(hoot_path)
        if not candidate_names:
            print(f"[WARN] No candidate signals found in {hoot_path}", file=sys.stderr)
            HootReplay.close_file()
            return FileReadResult(source_path=hoot_path, samples=[])

        active_signals: dict[str, tuple[str, Callable[[str], Any]]] = {}
        for name in candidate_names:
            found = self._probe_signal(name)
            if found is not None:
                active_signals[name] = found

        if not active_signals:
            print(f"[WARN] No readable signals found in {hoot_path}", file=sys.stderr)
            HootReplay.close_file()
            return FileReadResult(source_path=hoot_path, samples=[])

        last_timestamps: dict[str, int] = {}
        step_count = 0
        while not HootReplay.is_finished() and step_count < self._max_steps:
            step_status = HootReplay.step_timing(self._step_seconds)
            if not step_status.is_ok():
                break

            for name, (internal_type, getter) in active_signals.items():
                measurement = getter(name)
                if not measurement.status.is_ok():
                    continue

                ts_us = self._to_fpga_us(measurement.timestamp)
                if last_timestamps.get(name) == ts_us:
                    continue

                mapped = self._map_to_wpilog_type(name, internal_type, measurement.value)
                if mapped is None:
                    print(
                        f"[WARN] Skipping unmappable signal type '{internal_type}' for '{name}'",
                        file=sys.stderr,
                    )
                    continue

                wpilog_type, mapped_value = mapped
                samples.append(Sample(name=name, type_name=wpilog_type, timestamp_us=ts_us, value=mapped_value))
                last_timestamps[name] = ts_us

            step_count += 1

        HootReplay.close_file()

        if not samples:
            print(f"[WARN] {hoot_path} has no readable signals", file=sys.stderr)

        return FileReadResult(source_path=hoot_path, samples=samples)


def align_file_timestamps(results: list[FileReadResult]) -> dict[Path, int]:
    starts: dict[Path, int] = {}
    for result in results:
        if result.samples:
            starts[result.source_path] = min(s.timestamp_us for s in result.samples)

    if not starts:
        return {}

    global_start = min(starts.values())
    return {path: starts[path] - global_start for path in starts}


def merge_to_wpilog(results: list[FileReadResult], output_path: Path, metadata: str) -> tuple[int, int | None, int | None]:
    offsets = align_file_timestamps(results)

    writer = WPILogWriter(output_path, metadata=metadata)
    entry_ids: dict[tuple[Path, str], int] = {}
    entry_names: dict[tuple[Path, str], str] = {}
    used_entry_names: set[str] = set()
    next_entry_id = 1

    merged_rows: list[tuple[int, Path, Sample]] = []
    for result in results:
        offset = offsets.get(result.source_path, 0)
        for sample in result.samples:
            adjusted_ts = sample.timestamp_us - offset
            if adjusted_ts < 0:
                adjusted_ts = 0
            merged_rows.append((adjusted_ts, result.source_path, sample))

    merged_rows.sort(key=lambda row: row[0])

    prev_ts: int | None = None
    for adjusted_ts, source_path, sample in merged_rows:
        key = (source_path, sample.name)
        if key not in entry_ids:
            entry_ids[key] = next_entry_id
            base_name = sample.name
            candidate_name = base_name
            suffix = 2
            while candidate_name in used_entry_names:
                candidate_name = f"{base_name}-log{suffix}"
                suffix += 1

            entry_names[key] = candidate_name
            used_entry_names.add(candidate_name)
            writer.start_entry(
                name=candidate_name,
                type_name=sample.type_name,
                metadata="",
                entry_id=next_entry_id,
                timestamp_us=adjusted_ts,
            )
            next_entry_id += 1

        if prev_ts is not None and (adjusted_ts - prev_ts) > GAP_WARNING_US:
            delta_ms = (adjusted_ts - prev_ts) / 1000.0
            print(f"[WARN] Large merged timestamp gap detected: {delta_ms:.1f} ms", file=sys.stderr)

        writer.append_record(entry_ids[key], adjusted_ts, sample.type_name, sample.value)
        prev_ts = adjusted_ts

    writer.close()

    if not merged_rows:
        return 0, None, None

    return len(entry_ids), merged_rows[0][0], merged_rows[-1][0]


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge multiple CTRE .hoot logs into one WPILib .wpilog file"
    )
    parser.add_argument("hoot_files", nargs="+", help="Input .hoot files")
    parser.add_argument("-o", "--output", required=True, help="Output .wpilog path")
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=0.02,
        help="Replay sampling step in seconds (default: 0.02)",
    )
    parser.add_argument(
        "--metadata",
        default="Merged CTRE hoot logs",
        help="WPILog extra header metadata",
    )
    parser.add_argument(
        "--parser",
        choices=["auto", "owlet", "replay"],
        default="auto",
        help="Hoot parsing backend (default: auto)",
    )
    parser.add_argument(
        "--owlet",
        default=None,
        help="Path to owlet executable or directory containing owlet binaries (if omitted, searches current directory)",
    )
    parser.add_argument(
        "--strict-owlet-match",
        action="store_true",
        help="Require exact owlet compliancy match (C#) for each .hoot file; do not fall back to mismatched binaries",
    )
    return parser.parse_args(list(argv))


def _expand_input_paths(raw_inputs: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[Path] = set()

    for raw in raw_inputs:
        if not raw.exists():
            print(f"[WARN] File or directory does not exist: {raw}", file=sys.stderr)
            continue

        if raw.is_dir():
            hoot_files = sorted(path for path in raw.iterdir() if path.is_file() and path.suffix.lower() == ".hoot")
            if not hoot_files:
                print(f"[WARN] No .hoot files found in directory: {raw}", file=sys.stderr)
                continue
            for path in hoot_files:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                expanded.append(path)
            continue

        resolved = raw.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        expanded.append(raw)

    return expanded


def main(argv: Iterable[str]) -> int:
    if HootReplay is None:
        print(
            "[ERROR] phoenix6 is not installed in the active Python environment. Run: pip install phoenix6",
            file=sys.stderr,
        )
        return 4

    args = parse_args(argv)

    hoot_paths = _expand_input_paths([Path(p) for p in args.hoot_files])
    output_path = Path(args.output)

    if not hoot_paths:
        print("[ERROR] No input .hoot files were found.", file=sys.stderr)
        return 2

    extractor = PhoenixHootExtractor(
        step_seconds=args.step_seconds,
        parser_mode=args.parser,
        owlet_executable=args.owlet,
        strict_owlet_match=args.strict_owlet_match,
    )
    results: list[FileReadResult] = []

    for path in hoot_paths:
        results.append(extractor.read_file(path))

    readable_source_files = sum(1 for r in results if r.samples)
    if readable_source_files == 0:
        print("[ERROR] No readable source files/signals were found.", file=sys.stderr)
        return 2

    entry_count, min_ts, max_ts = merge_to_wpilog(results, output_path, metadata=args.metadata)

    if min_ts is None or max_ts is None:
        print("[ERROR] No merged records were written.", file=sys.stderr)
        return 3

    print("Merge complete")
    print(f"  Output file: {output_path}")
    print(f"  Source files: {readable_source_files}")
    print(f"  Total entries written: {entry_count}")
    print(f"  Time range covered (us): {min_ts} -> {max_ts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
