#!/usr/bin/env python3
from __future__ import annotations

import csv
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from merge_hoot import FileReadResult, PhoenixHootExtractor, WPILogReader, merge_to_wpilog

OUTPUT_WPILOG = "merged.wpilog"
OUTPUT_LIST_CSV = "signals.csv"
OUTPUT_AUDIT_CSV = "missing_signals.csv"
_SUFFIX_RE = re.compile(r"-log\d+$")
_OWLET_DOWNLOAD_URL = "https://docs.ctr-electronics.com/cli-tools.html"


class HootMergerGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("HootMerger")
        self.root.geometry("780x500")

        self._message_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._total_files = 1
        self._progress_done = 0
        self._is_running = False
        self._mouth_open = True

        self.folder_var = tk.StringVar()
        self.make_audit_var = tk.BooleanVar(value=False)
        self.make_list_var = tk.BooleanVar(value=False)
        self.progress_text_var = tk.StringVar(value="Progress: 0/0")

        self._build_ui()
        self._poll_messages()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        folder_row = ttk.Frame(frame)
        folder_row.pack(fill="x", pady=(0, 10))

        ttk.Label(folder_row, text="Hoot folder:").pack(side="left")
        folder_entry = ttk.Entry(folder_row, textvariable=self.folder_var)
        folder_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(folder_row, text="Browse...", command=self._pick_folder).pack(side="left")

        toggles_row = ttk.Frame(frame)
        toggles_row.pack(fill="x", pady=(0, 10))

        ttk.Checkbutton(
            toggles_row,
            text="Generate audit CSV (missing_signals.csv)",
            variable=self.make_audit_var,
        ).pack(anchor="w")

        ttk.Checkbutton(
            toggles_row,
            text="Generate signal list CSV (signals.csv)",
            variable=self.make_list_var,
        ).pack(anchor="w")

        self.run_button = ttk.Button(frame, text="Convert Folder", command=self._start_run)
        self.run_button.pack(anchor="w", pady=(0, 10))

        ttk.Label(frame, textvariable=self.progress_text_var).pack(anchor="w")
        self.progress_canvas = tk.Canvas(frame, height=56, highlightthickness=0)
        self.progress_canvas.pack(fill="x", pady=(4, 10))
        self.progress_canvas.bind("<Configure>", lambda _event: self._draw_dragon_progress())

        self.log_text = tk.Text(frame, height=18, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self._draw_dragon_progress()
        self._tick_animation()

    def _pick_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select folder containing .hoot files")
        if selected:
            self.folder_var.set(selected)

    def _start_run(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("HootMerger", "A conversion is already running.")
            return

        folder_text = self.folder_var.get().strip()
        if not folder_text:
            messagebox.showerror("HootMerger", "Select a folder first.")
            return

        folder = Path(folder_text)
        if not folder.is_dir():
            messagebox.showerror("HootMerger", f"Folder does not exist:\n{folder}")
            return

        self.run_button.configure(state="disabled")
        self._is_running = True
        self._mouth_open = True
        self._clear_log()
        self._log_ui(f"Folder: {folder}")
        self._update_progress_ui(0, 1)

        self._worker = threading.Thread(
            target=self._run_conversion,
            args=(folder, self.make_audit_var.get(), self.make_list_var.get()),
            daemon=True,
        )
        self._worker.start()

    def _run_conversion(self, folder: Path, make_audit: bool, make_list: bool) -> None:
        try:
            hoot_files = sorted(folder.glob("*.hoot"))
            if not hoot_files:
                self._post_log("No .hoot files found in selected folder.")
                self._post_done(error=True, message="No .hoot files found.")
                return

            self._post_log(f"Found {len(hoot_files)} .hoot file(s).")
            total_steps = len(hoot_files) + 1
            if make_list:
                total_steps += 1
            if make_audit:
                total_steps += 1
            completed_steps = 0
            self._post_progress(completed_steps, total_steps)

            owlet_path = self._resolve_owlet_path(folder)
            if owlet_path is None:
                guidance_lines, guidance_popup = self._build_owlet_guidance(hoot_files)
                self._post_log("No owlet executable was found.")
                for line in guidance_lines:
                    self._post_log(line)
                self._post_done(error=True, message=guidance_popup)
                return

            self._post_log(f"Using owlet: {owlet_path}")

            extractor = PhoenixHootExtractor(
                parser_mode="owlet",
                owlet_executable=str(owlet_path),
                strict_owlet_match=False,
            )

            results: list[FileReadResult] = []
            for path in hoot_files:
                self._post_log(f"Reading: {path.name}")
                results.append(extractor.read_file(path))
                completed_steps += 1
                self._post_progress(completed_steps, total_steps)

            readable_sources = sum(1 for result in results if result.samples)
            if readable_sources == 0:
                self._post_log("No readable source files/signals were found.")
                self._post_done(error=True, message="No readable source files/signals were found.")
                return

            output_wpilog = folder / OUTPUT_WPILOG
            self._post_log("Beginning WPILog write stage...")
            self._post_log(f"Writing merged output to: {output_wpilog}")
            entry_count, min_ts, max_ts = merge_to_wpilog(results, output_wpilog, metadata="Merged CTRE hoot logs")
            if min_ts is None or max_ts is None:
                self._post_log("No merged records were written.")
                self._post_done(error=True, message="No merged records were written.")
                return
            completed_steps += 1
            self._post_progress(completed_steps, total_steps)
            self._post_log("WPILog write stage complete.")

            self._post_log(f"Wrote {output_wpilog.name}")
            self._post_log(f"Source files with data: {readable_sources}")
            self._post_log(f"Entries written: {entry_count}")
            self._post_log(f"Time range (us): {min_ts} -> {max_ts}")

            if make_list:
                list_rows = self._collect_source_signal_rows(results)
                output_list = folder / OUTPUT_LIST_CSV
                self._write_list_csv(output_list, list_rows)
                self._post_log(f"Wrote {output_list.name} ({len(list_rows)} row(s))")
                completed_steps += 1
                self._post_progress(completed_steps, total_steps)

            if make_audit:
                source_rows = self._collect_source_signal_rows(results)
                merged_names = self._collect_merged_signal_names(output_wpilog)
                missing_rows = [row for row in source_rows if row[1] not in merged_names]
                output_audit = folder / OUTPUT_AUDIT_CSV
                self._write_audit_csv(output_audit, missing_rows)
                self._post_log(f"Wrote {output_audit.name} ({len(missing_rows)} missing row(s))")
                completed_steps += 1
                self._post_progress(completed_steps, total_steps)

            self._post_done(error=False, message="Conversion complete.")
        except Exception as exc:
            self._post_log(f"Unexpected error: {exc}")
            self._post_done(error=True, message=str(exc))

    @staticmethod
    def _resolve_owlet_path(selected_folder: Path) -> Path | None:
        search_dirs: list[Path] = [selected_folder]

        executable_parent = Path(sys.executable).resolve().parent
        if executable_parent not in search_dirs:
            search_dirs.append(executable_parent)

        script_parent = Path(__file__).resolve().parent
        if script_parent not in search_dirs:
            search_dirs.append(script_parent)

        cwd = Path.cwd().resolve()
        if cwd not in search_dirs:
            search_dirs.append(cwd)

        for directory in search_dirs:
            candidates = sorted(directory.glob("owlet*.exe"))
            if candidates:
                return candidates[0]

        return None

    @staticmethod
    def _normalize_merged_name(name: str) -> str:
        return _SUFFIX_RE.sub("", name)

    @staticmethod
    def _build_owlet_guidance(hoot_files: list[Path]) -> tuple[list[str], str]:
        compliancies: set[int] = set()
        for hoot_file in hoot_files:
            compliancy = PhoenixHootExtractor._read_hoot_compliancy(hoot_file)
            if compliancy is not None:
                compliancies.add(compliancy)

        lines: list[str] = [
            "Place owlet*.exe in the selected folder, next to this app, or in the current working folder.",
            f"Download owlet from: {_OWLET_DOWNLOAD_URL}",
        ]

        popup_parts: list[str] = [
            "No owlet executable found (owlet*.exe).",
            f"Download: {_OWLET_DOWNLOAD_URL}",
        ]

        if not compliancies:
            lines.append("Could not detect required owlet compliancy from the selected .hoot file(s).")
            popup_parts.append("Required compliancy could not be detected from selected .hoot file(s).")
            return lines, "\n".join(popup_parts)

        needed_labels = ", ".join(f"C{value}" for value in sorted(compliancies))
        lines.append(f"Detected required owlet compliancy: {needed_labels}")
        popup_parts.append(f"Detected required compliancy: {needed_labels}")

        PhoenixHootExtractor._load_owlet_index_if_needed()
        version_map = PhoenixHootExtractor._OWLET_VERSION_COMPLIANCY or {}

        matched_versions: list[str] = []
        for version, compliancy in sorted(version_map.items()):
            if compliancy in compliancies:
                matched_versions.append(f"owlet-{version} (C{compliancy})")

        if matched_versions:
            lines.append("Matching owlet versions from CTRE index:")
            for version_label in matched_versions:
                lines.append(f"  - {version_label}")
            popup_parts.append(f"Matching versions: {', '.join(matched_versions)}")
        else:
            lines.append("No matching version list was available from CTRE index at runtime.")

        return lines, "\n".join(popup_parts)

    @staticmethod
    def _collect_source_signal_rows(results: list[FileReadResult]) -> list[tuple[str, str, str, str]]:
        rows: set[tuple[str, str, str, str]] = set()
        for result in results:
            source_name = result.source_path.name
            for sample in result.samples:
                rows.add((source_name, sample.name, sample.type_name, ""))
        return sorted(rows, key=lambda r: (r[0].lower(), r[1].lower(), r[2].lower()))

    def _collect_merged_signal_names(self, output_wpilog: Path) -> set[str]:
        merged_samples = WPILogReader(output_wpilog).read_samples()
        return {self._normalize_merged_name(sample.name) for sample in merged_samples}

    @staticmethod
    def _write_list_csv(output_csv: Path, rows: list[tuple[str, str, str, str]]) -> None:
        with output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source_file", "signal_name", "type", "metadata"])
            for row in rows:
                writer.writerow(row)

    @staticmethod
    def _write_audit_csv(output_csv: Path, rows: list[tuple[str, str, str, str]]) -> None:
        with output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source_file", "signal_name", "type", "metadata", "reason"])
            for source_file, signal_name, type_name, metadata in rows:
                writer.writerow([source_file, signal_name, type_name, metadata, "missing_in_merged"])

    def _post_log(self, text: str) -> None:
        self._message_queue.put(("log", text))

    def _post_progress(self, done: int, total: int) -> None:
        self._message_queue.put(("progress", (done, total)))

    def _post_done(self, error: bool, message: str) -> None:
        self._message_queue.put(("done", (error, message)))

    def _poll_messages(self) -> None:
        try:
            while True:
                event, payload = self._message_queue.get_nowait()
                if event == "log":
                    self._log_ui(str(payload))
                elif event == "progress":
                    done, total = payload
                    self._update_progress_ui(int(done), int(total))
                elif event == "done":
                    error, message = payload
                    self._is_running = False
                    self._mouth_open = False
                    self._draw_dragon_progress()
                    self.run_button.configure(state="normal")
                    if error:
                        self._log_ui(f"ERROR: {message}")
                        messagebox.showerror("HootMerger", message)
                    else:
                        self._log_ui(f"OK: {message}")
                        messagebox.showinfo("HootMerger", message)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._poll_messages)

    def _update_progress_ui(self, done: int, total: int) -> None:
        self._progress_done = max(0, done)
        self._total_files = max(1, total)
        self.progress_text_var.set(f"Progress: {self._progress_done}/{total}")
        self._draw_dragon_progress()

    def _draw_dragon_progress(self) -> None:
        canvas = self.progress_canvas
        canvas.delete("all")
        bg = canvas.cget("background")

        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 56)

        left = 20
        right = width - 20
        track = max(1, right - left)
        y = height // 2

        fraction = min(1.0, max(0.0, self._progress_done / self._total_files))
        dragon_x = left + int(track * fraction)

        dot_count = 26
        if dot_count > 1:
            spacing = track / (dot_count - 1)
        else:
            spacing = 0

        for i in range(dot_count):
            dot_x = left + i * spacing
            if dot_x > (dragon_x + 10):
                color = "#FFD400"
            else:
                color = "#A9A9A9"
            canvas.create_oval(dot_x - 4, y - 4, dot_x + 4, y + 4, fill=color, outline=color)

        green_dark = "#1B8E3E"
        green_mid = "#2FAE54"
        green_light = "#49C96D"

        for segment_index in range(3, 0, -1):
            seg_x = dragon_x - (segment_index * 11)
            seg_r = 5 + segment_index
            canvas.create_oval(
                seg_x - seg_r,
                y - seg_r,
                seg_x + seg_r,
                y + seg_r,
                fill=green_mid,
                outline=green_dark,
            )

        canvas.create_polygon(
            dragon_x - 44,
            y,
            dragon_x - 32,
            y - 6,
            dragon_x - 32,
            y + 6,
            fill=green_mid,
            outline=green_dark,
            width=1,
        )

        head_r = 13
        canvas.create_oval(
            dragon_x - head_r,
            y - head_r,
            dragon_x + head_r,
            y + head_r,
            fill=green_light,
            outline=green_dark,
            width=2,
        )

        canvas.create_polygon(
            dragon_x + 6,
            y - 9,
            dragon_x + 20,
            y - 4,
            dragon_x + 20,
            y + 4,
            dragon_x + 6,
            y + 9,
            fill=green_light,
            outline=green_dark,
            width=2,
        )

        canvas.create_polygon(
            dragon_x - 7,
            y - 11,
            dragon_x - 2,
            y - 19,
            dragon_x + 2,
            y - 10,
            fill=green_mid,
            outline=green_dark,
            width=1,
        )
        canvas.create_polygon(
            dragon_x - 1,
            y - 11,
            dragon_x + 5,
            y - 18,
            dragon_x + 7,
            y - 9,
            fill=green_mid,
            outline=green_dark,
            width=1,
        )

        for spike_offset in (-18, -10, -2):
            canvas.create_polygon(
                dragon_x + spike_offset,
                y - 8,
                dragon_x + spike_offset + 3,
                y - 13,
                dragon_x + spike_offset + 6,
                y - 8,
                fill=green_mid,
                outline=green_dark,
                width=1,
            )

        if self._mouth_open:
            mouth_top = y - 7
            mouth_bottom = y + 7
            mouth_back = dragon_x + 2
            mouth_tip = dragon_x + head_r + 9
        else:
            mouth_top = y - 1
            mouth_bottom = y + 1
            mouth_back = dragon_x + 8
            mouth_tip = dragon_x + head_r + 7

        canvas.create_polygon(
            mouth_tip,
            y,
            mouth_back,
            mouth_top,
            mouth_back,
            mouth_bottom,
            fill=bg,
            outline=bg,
        )

        canvas.create_oval(
            dragon_x + 2,
            y - 7,
            dragon_x + 7,
            y - 3,
            fill="#111111",
            outline="#111111",
        )

    def _tick_animation(self) -> None:
        if self._is_running:
            self._mouth_open = not self._mouth_open
            self._draw_dragon_progress()
        self.root.after(180, self._tick_animation)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log_ui(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    HootMergerGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
