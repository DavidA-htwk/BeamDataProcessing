# Beam Data Handling

Batch processing GUI for `.vtp` files produced by [BeamOnTarget](https://github.com/CharlieHills92/BeamOnTarget).

![Beam](Startup.png)

---

## Features

- **Physical smoothing** — estimates the power-density field from the Monte Carlo tally with a
  dual-graph heat-kernel operator: energy conserved exactly, no zero cells next to hot ones, and a
  per-cell error bar (statistical + smoothing bias). See `modules/vtk/physical_smooth.py`
- **Edge smoothing** — iterative neighbour-mean smoothing restricted to boundary/feature cells
- **Snapshots** — offscreen VTK renders coloured by Power Density (W/m²) and/or Total Power (W)
- **CSV export** — `max_comparison_batch.csv` (peak power density before/after) and `total_power_batch.csv`
- **Coordinate transform** — rotate + translate per-cell CSV data between beam and tokamak frames
- **Per-component config** — smooth iterations, multiplication factor, and snapshot toggles saved to JSON

---

## Project Structure

```
Value_extract/
├── Data_handling.py          # Entry point (GUI + pipeline orchestration)
├── requirements.txt
├── install.bat / install.sh
└── modules/
    ├── core/                 # settings.py, path_utils.py
    ├── vtk/                  # vtk_io.py, smoothing.py, physical_smooth.py, snapshot_max.py
    ├── processing/           # pipeline.py, workers.py
    ├── transform/            # transform_reference_frame.py, generate_report.py, ...
    └── gui/                  # gui_tab1.py, gui_tab2.py, coordinates.png
```

---

## Requirements

- Python 3.10+
- `vtk >= 9.6.2`
- `numpy >= 2.4.6`
- `openpyxl >= 3.1.5`
- `pyvista >= 0.44` (used by `generate_report.py`)

```bash
pip install -r requirements.txt
```

---

## Quick Start

```powershell
# First time — create venv and install dependencies
.\install.bat

# Launch
python .\Data_handling.py
```

Optional (without venv):

```powershell
pvpython .\Data_handling.py
```

---

## Usage

### Tab 1 — Processing

1. Paste input directory paths (one per line). `OUTPUT_*` folders expand automatically.
2. Click **Load Geometry** to scan for `.vtp` files and populate the component list.
3. Per component: set smooth iterations, multiplication factor, and which snapshots to render.
4. Click **Run Processing**.

Outputs written to the chosen output folder:

| File | Contents |
|---|---|
| `max_comparison_batch.csv` | case / scenario / filename / max before & after smoothing |
| `total_power_batch.csv` | total deposited power per file |
| `snapshots/` | PNG renders (power density and/or total power) |

### Tab 2 — Coordinate Transform

Select a preset (e.g. *DNB → Tokamak*), choose input/output units, pick an **Output format**, and click **Run Transform**.

Preset translations are defined in metres and are converted into the selected
**input coordinate unit** before being applied, so `mm` input files transform
correctly.

| Output format | Written to | Contents |
|---|---|---|
| `CSV` | `transformed/` | Per-cell table (X, Y, Z, Area, power columns) |
| `VTP` | `transformed_vtp/` | The mesh itself, points rotated/translated into the target frame |
| `Both` | both folders | Same run, both representations |

Both formats apply the same rotation, translation, unit scaling, export
selection, CSV export factor and zero-row filtering, so a CSV and a VTP from
the same run describe exactly the same cells. Two differences follow from the
file type:

- **Geometry (X, Y, Z)** cannot be switched off for a VTP — a mesh without
  coordinates is not a mesh — so the toggle is disabled in VTP-only mode.
- `total_deposited_power_W` is a CSV column but VTP field data.
- A VTP keeps the per-species arrays the CSV never had
  (`Deposited_Power_W_H+`, `Power_Density_W_m2_H0`, …). The CSV export factor
  is applied to those too, so they still sum to the total. Each array keeps
  its source storage type, so a scaled VTP is the same size as an unscaled
  one. Because the factor is baked in, do not feed a transformed VTP back
  into Tab 1 — it would be applied twice.

Note: `Power_Density_W_m2` is *not* `Deposited_Power_W / Area` in the source
data (the ratio varies per cell), so don't expect that relation to hold in
either output. The exported `Area` is computed by `vtkCellSizeFilter`, the
same value the CSV has always used.

![Coordinate system](modules/gui/coordinates.png)


---
## Path Structure

The tool automatically detects case and scenario names from the folder hierarchy:

| Structure | `case` | `scenario` |
|---|---|---|
| `OUTPUT_CDL/FFTC/dnb_3_+10_+2/SMOOTHED/` | `FFTC` | `dnb_3_+10_+2` |
| `OUTPUT_FFTC/dnb_3_+10_+2/SMOOTHED/` | `FFTC` | `dnb_3_+10_+2` |

---

## Config File

Settings are saved automatically to `config/data_handling_settings.json` on each run and restored on next launch. You can also save/load named config files from the GUI using the **Save config** / **Load config** buttons.
