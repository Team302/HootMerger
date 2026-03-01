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

from merge_hoot import FileReadResult, PhoenixHootExtractor, WPILogReader, merge_to_wpilog

OUTPUT_WPILOG = "merged.wpilog"
OUTPUT_LIST_CSV = "signals.csv"
OUTPUT_AUDIT_CSV = "missing_signals.csv"
_SUFFIX_RE = re.compile(r"-log\d+$")


class HootMergerGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("HootMerger")
        self.root.geometry("780x500")

        self._message_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None

        self.folder_var = tk.StringVar()
        self.make_audit_var = tk.BooleanVar(value=False)
        self.make_list_var = tk.BooleanVar(value=False)

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

        self.log_text = tk.Text(frame, height=18, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

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
        self._clear_log()
        self._log(f"Folder: {folder}")

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
                self._log("No .hoot files found in selected folder.")
                self._notify_done(error=True, message="No .hoot files found.")
                return

            self._log(f"Found {len(hoot_files)} .hoot file(s).")

            owlet_path = self._resolve_owlet_path(folder)
            if owlet_path is None:
                self._log("No owlet executable was found.")
                self._log("Place owlet*.exe in the selected folder, next to this app, or in the current working folder.")
                self._notify_done(error=True, message="No owlet executable found (owlet*.exe).")
                return

            self._log(f"Using owlet: {owlet_path}")

            extractor = PhoenixHootExtractor(
                parser_mode="owlet",
                owlet_executable=str(owlet_path),
                strict_owlet_match=False,
            )

            results: list[FileReadResult] = []
            for path in hoot_files:
                self._log(f"Reading: {path.name}")
                results.append(extractor.read_file(path))

            readable_sources = sum(1 for result in results if result.samples)
            if readable_sources == 0:
                self._log("No readable source files/signals were found.")
                self._notify_done(error=True, message="No readable source files/signals were found.")
                return

            output_wpilog = folder / OUTPUT_WPILOG
            entry_count, min_ts, max_ts = merge_to_wpilog(results, output_wpilog, metadata="Merged CTRE hoot logs")
            if min_ts is None or max_ts is None:
                self._log("No merged records were written.")
                self._notify_done(error=True, message="No merged records were written.")
                return

            self._log(f"Wrote {output_wpilog.name}")
            self._log(f"Source files with data: {readable_sources}")
            self._log(f"Entries written: {entry_count}")
            self._log(f"Time range (us): {min_ts} -> {max_ts}")

            if make_list:
                list_rows = self._collect_source_signal_rows(results)
                output_list = folder / OUTPUT_LIST_CSV
                self._write_list_csv(output_list, list_rows)
                self._log(f"Wrote {output_list.name} ({len(list_rows)} row(s))")

            if make_audit:
                source_rows = self._collect_source_signal_rows(results)
                merged_names = self._collect_merged_signal_names(output_wpilog)
                missing_rows = [row for row in source_rows if row[1] not in merged_names]
                output_audit = folder / OUTPUT_AUDIT_CSV
                self._write_audit_csv(output_audit, missing_rows)
                self._log(f"Wrote {output_audit.name} ({len(missing_rows)} missing row(s))")

            self._notify_done(error=False, message="Conversion complete.")
        except Exception as exc:
            self._log(f"Unexpected error: {exc}")
            self._notify_done(error=True, message=str(exc))

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

    def _notify_done(self, error: bool, message: str) -> None:
        prefix = "ERROR: " if error else "OK: "
        self._message_queue.put(prefix + message)

    def _poll_messages(self) -> None:
        try:
            while True:
                message = self._message_queue.get_nowait()
                self._log(message)
                self.run_button.configure(state="normal")
                if message.startswith("ERROR: "):
                    messagebox.showerror("HootMerger", message.removeprefix("ERROR: "))
                elif message.startswith("OK: "):
                    messagebox.showinfo("HootMerger", message.removeprefix("OK: "))
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._poll_messages)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log(self, text: str) -> None:
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
