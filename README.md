# BeamDataProcessing

A batch processing tool for `.vtp` files produced by **BeamOnTarget** (https://github.com/CharlieHills92/BeamOnTarget).

---

## Features

- **Edge smoothing** — applies the Smart-Smooth-EDGE algorithm (single or multi-pass) restricted to boundary and feature-edge cells, preserving interior geometry
- **Max comparison** — records the peak `Power_Density_W_m2` before and after smoothing for every file in a CSV log
- **Snapshots** — renders an offscreen PNG of the cell with the highest scalar value, coloured by a blue→red heat map
- **Coordinate transform** — extracts per-cell geometry and scalar data from `.vtp` files, applies a Z-rotation + translation, and exports a transformed CSV
- **Persistent config** — all settings are saved to and loaded from a `.json` file

---

## Requirements

- Python 3.10+
- `vtk >= 9.6.2`
- `numpy >= 2.4.6`
- `openpyxl >= 3.1.5`
- `pyvista >= 0.44` (used by `generate_report.py`)

Install all dependencies with:

```bash
pip install -r requirements.txt
```

---

## Quick Start (Windows)

**First time setup** — run the installer once:

```powershell
.\install.bat
```

**Every subsequent session:**

# Launch the GUI
python .\Data_handling.py
```

Or with ParaView's bundled Python (no venv needed):

```powershell
pvpython .\Data_handling.py
```

---

## GUI Overview

The application has two tabs.

### Tab 1 — Processing

1. Paste one or more input directory paths (one per line). `OUTPUT_*` folders are automatically expanded into their immediate subfolders.
2. Set the glob pattern (default `smoothed_results_*.vtp`) and an optional name filter.
3. Choose an output folder for the CSV log (defaults to `output/` next to the script).
4. Set **Smooth iterations** (default 1). Higher values diffuse edge-ring spikes further without touching interior cells.
5. Optionally enable **Save max-point snapshots** to generate a PNG per file.
6. Click **Run Processing**.

Output: `output/max_comparison_batch.csv` with columns:

| column | description |
|---|---|
| `case` | case identifier (e.g. `FFTC`) |
| `scenario` | scenario identifier (e.g. `dnb_3_+10_+2`) |
| `filename` | source `.vtp` filename |
| `max_before` | peak value before smoothing |
| `max_after` | peak value after smoothing |
| `delta` | absolute difference |
| `discrepancy` | `YES` if delta > 0 |

### Tab 2 — Coordinate Transform

Applies a rigid-body transform to the cell centroids extracted from each `.vtp` file:

1. Select input/output coordinate units (`mm` or `m`).
2. Enter the Z-axis rotation angle and X/Y/Z translation.
3. Choose which properties to export (geometry, area, power, power load).
4. Set a multiplication factor applied to power values.
5. Click **Run Transform**.

![Coordinate system](coordinates.png)

---

## Path Structure

The tool automatically detects case and scenario names from the folder hierarchy:

| Structure | `case` | `scenario` |
|---|---|---|
| `OUTPUT_CDL/FFTC/dnb_3_+10_+2/SMOOTHED/` | `FFTC` | `dnb_3_+10_+2` |
| `OUTPUT_FFTC/dnb_3_+10_+2/SMOOTHED/` | `FFTC` | `dnb_3_+10_+2` |

---

## Project Structure

```
BeamDataProcessing/
├── Data_handling.py          # Main entry point — GUI + processing pipeline
├── requirements.txt
├── install.bat               # Windows one-click setup
├── install.sh                # Linux/macOS setup
├── coordinates.png           # Coordinate system reference image
└── modules/
    ├── snapshot_max.py           # Offscreen VTK PNG renderer
    ├── generate_report.py        # Extracts per-cell data to CSV
    ├── transform_reference_frame.py  # Applies rotation + translation to CSV
    ├── transform_vtp.py          # VTP-level transform utilities
    └── Extract_results.py        # Result extraction helpers
```

---

## Config File

Settings are saved automatically to `config/data_handling_settings.json` on each run and restored on next launch. You can also save/load named config files from the GUI using the **Save config** / **Load config** buttons.
