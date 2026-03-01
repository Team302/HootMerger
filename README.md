# HootMerger

Utilities for working with CTRE Phoenix hoot logs:

- `merge_hoot.py`: merge one or more `.hoot` logs into a single `.wpilog`
- `list_signals_csv.py`: list all signal names/types from `.hoot` or `.wpilog` into a CSV
- `audit_missing_signals.py`: compare source logs to merged `.wpilog` and output missing signals to CSV
- `hoot_merger_gui.py`: simple folder-based GUI for batch conversion

## GUI (folder workflow)

Script: `hoot_merger_gui.py`

Run:

```powershell
python hoot_merger_gui.py
```

GUI behavior:

- Pick a folder containing `.hoot` files
- Click **Convert Folder**
- Always writes `merged.wpilog` in that same folder
- Optional toggle: write `missing_signals.csv`
- Optional toggle: write `signals.csv`

The GUI attempts to locate `owlet*.exe` in the selected folder, next to the app, or in the current working folder.

If owlet is missing, the GUI shows:

- Download link: `https://docs.ctr-electronics.com/cli-tools.html`
- Detected required owlet compliancy from selected `.hoot` files (for example `C7`)
- Matching owlet version labels when CTRE index data is available at runtime

## Build Windows EXE (PyInstaller)

Install dependencies:

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

Build with included script:

```powershell
.\build_gui_exe.ps1
```

Output:

- `dist\HootMergerGUI.exe`

## Requirements

- Python 3.10+
- Phoenix 6 Python package
- Owlet executable for reliable `.hoot` parsing (recommended)

Install Python dependency:

```powershell
python -m pip install phoenix6
```

Place an `owlet*.exe` in the project folder, or pass it explicitly with `--owlet`.

## 1) Merge hoot logs to WPILog

Script: `merge_hoot.py`

### Basic usage

```powershell
python merge_hoot.py <log1.hoot> <log2.hoot> ... -o merged.wpilog
```

### Recommended usage (Owlet backend)

```powershell
python merge_hoot.py <log1.hoot> <log2.hoot> ... -o merged.wpilog --parser owlet --owlet .\owlet-25.4.1-windowsx86-64.exe
```

### Strict owlet compliancy matching

```powershell
python merge_hoot.py <log1.hoot> <log2.hoot> ... -o merged.wpilog --parser owlet --owlet . --strict-owlet-match
```

When strict mode is enabled, each `.hoot` file must find a matching owlet binary with the same compliancy tag (`C#`, e.g. `C7`). The script will not fall back to mismatched binaries.

### Options

- `-o, --output` (required): output `.wpilog` path
- `--step-seconds`: replay sampling step for replay backend (default `0.02`)
- `--metadata`: extra WPILog header metadata string
- `--parser`: `auto`, `owlet`, or `replay` (default `auto`)
- `--owlet`: path to owlet executable, or a directory containing owlet binaries
- `--strict-owlet-match`: require exact owlet compliancy match (`C#`) per input file

### Signal naming behavior

- Output uses original signal names (no filename prefix)
- If a name conflicts across files, suffixes are added: `-log2`, `-log3`, etc.

### Boolean behavior

- Real boolean signals are written as WPILog `boolean`
- AdvantageScope will render these as boolean traces (dot-style values)
- Some signals from source tools may be emitted as numeric/string even if semantically boolean

### Notes

- If owlet prints warnings like `Could not read to end of input file: bad message` but still produces output, the script continues and uses that output.
- For older hoot logs, a matching older owlet version may be required.

## 2) Export signal list to CSV

Script: `list_signals_csv.py`

### Basic usage

```powershell
python list_signals_csv.py <input1.hoot> <input2.hoot> ... -o signals.csv --owlet .\owlet-25.4.1-windowsx86-64.exe
```

You can also pass `.wpilog` files directly:

```powershell
python list_signals_csv.py merged.wpilog -o signals.csv
```

### CSV output columns

- `source_file`
- `signal_name`
- `type`
- `metadata`

### Options

- `-o, --output` (required): output CSV path
- `--owlet`: path to owlet executable (needed for `.hoot` inputs)

## 3) Audit missing signals against merged log

Script: `audit_missing_signals.py`

This utility checks source `.hoot`/`.wpilog` files against a merged `.wpilog` and writes a CSV of signals that are present in source files but not present in the merged output.

### Basic usage

```powershell
python audit_missing_signals.py <source1.hoot> <source2.hoot> ... --merged merged.wpilog -o missing_signals.csv --owlet .\owlet-25.4.1-windowsx86-64.exe
```

### CSV output columns

- `source_file`
- `signal_name`
- `type`
- `metadata`
- `reason`

### Options

- `--merged` (required): merged `.wpilog` to audit
- `-o, --output` (required): output CSV path
- `--owlet`: path to owlet executable (needed for `.hoot` inputs)

## Example commands used in this repo

Merge `examples/example2`:

```powershell
python merge_hoot.py \
  examples/example2/MIBKN_Q80_63AED27550374E53202020470D2A10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_A0D7896450374E5320202047390F10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_rio_2025-03-16_12-12-54.hoot \
  -o merged_example2.wpilog \
  --parser owlet \
  --owlet .\owlet-25.4.1-windowsx86-64.exe
```

Generate signal list CSV for `example2`:

```powershell
python list_signals_csv.py \
  examples/example2/MIBKN_Q80_63AED27550374E53202020470D2A10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_A0D7896450374E5320202047390F10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_rio_2025-03-16_12-12-54.hoot \
  -o signals_example2.csv \
  --owlet .\owlet-25.4.1-windowsx86-64.exe
```

Audit missing signals for `example2` merged output:

```powershell
python audit_missing_signals.py \
  examples/example2/MIBKN_Q80_63AED27550374E53202020470D2A10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_A0D7896450374E5320202047390F10FF_2025-03-16_12-12-54.hoot \
  examples/example2/MIBKN_Q80_rio_2025-03-16_12-12-54.hoot \
  --merged merged_example2.wpilog \
  -o missing_example2.csv \
  --owlet .\owlet-25.4.1-windowsx86-64.exe
```
