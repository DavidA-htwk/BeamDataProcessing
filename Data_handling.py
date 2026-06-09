"""
Data_handling.py
----------------
Batch pipeline (no GUI required).  For each file matching the chosen pattern
inside the selected input folder:

  1. Find the max Power_Density_W_m2  (before smoothing)
  2. Apply the Smart-Smooth-EDGE algorithm in memory
  3. Find the max again               (after smoothing)
  4. Append one row per file to a CSV log:
       case | scenario | filename | max_before | max_after | delta | discrepancy

Run with:
    pvpython  Data_handling.py          # ParaView's bundled Python
    python    Data_handling.py          # regular Python if 'vtk' and 'numpy' are installed
"""

import os
import re
import sys
import glob
import csv
import json
import threading
import time
from multiprocessing.pool import ThreadPool
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox

try:
    import vtk
    import numpy as np
    from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk
except ImportError:
    sys.exit(
        "ERROR: vtk and numpy are required.\n"
        "  Run with pvpython, or:  pip install vtk numpy"
    )

from modules.snapshot_max import save_max_snapshot, precompute_snapshot
from modules.generate_report import extract_cells_to_csv
from modules import transform_reference_frame as _trf

# ── Configuration ─────────────────────────────────────────────────────────────
ARRAY_NAME    = "Power_Density_W_m2"
FEATURE_ANGLE = 30.0   # degrees — matches Smart_Smooth_EDGE.py


SETTINGS_FILE = Path(__file__).resolve().parent / "config" / "data_handling_settings.json"


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(cfg: dict) -> None:
    try:
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save settings: {e}")


# ── GUI ───────────────────────────────────────────────────────────────────────
def run_gui():
    settings = load_settings()

    root = tk.Tk()
    root.title("Data Handling")
    root.resizable(True, True)

    # ── Notebook ────────────────────────────────────────────────────────────
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=False, padx=10, pady=(10, 0))

    tab1 = tk.Frame(notebook)
    tab2 = tk.Frame(notebook)
    notebook.add(tab1, text="Processing")
    notebook.add(tab2, text="Coordinate Transform")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — Processing
    # ══════════════════════════════════════════════════════════════════════════

    # ── Directory list ────────────────────────────────────────────────────────
    tk.Label(tab1, text="Paste input directory paths (one per line):", anchor="w").pack(
        fill="x", padx=10, pady=(10, 2)
    )
    text_frame = tk.Frame(tab1)
    text_frame.pack(fill="both", expand=True, padx=10, pady=2)
    scrollbar = tk.Scrollbar(text_frame)
    scrollbar.pack(side="right", fill="y")
    text_box = tk.Text(
        text_frame, width=90, height=10,
        yscrollcommand=scrollbar.set, wrap="none",
    )
    text_box.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=text_box.yview)
    if settings.get("input_dirs"):
        text_box.insert("1.0", "\n".join(settings["input_dirs"]))

    # ── Glob pattern & name filter ────────────────────────────────────────────
    opt_frame = tk.Frame(tab1)
    opt_frame.pack(fill="x", padx=10, pady=(6, 0))

    tk.Label(opt_frame, text="Glob pattern:", anchor="w").grid(row=0, column=0, sticky="w")
    pattern_var = tk.StringVar(value=settings.get("pattern", "smoothed_results_*.vtp"))
    tk.Entry(opt_frame, textvariable=pattern_var, width=40).grid(
        row=0, column=1, sticky="w", padx=(6, 20)
    )

    tk.Label(opt_frame, text="Name filter (comma-separated):", anchor="w").grid(
        row=0, column=2, sticky="w"
    )
    filter_var = tk.StringVar(value=settings.get("name_filter", ""))
    tk.Entry(opt_frame, textvariable=filter_var, width=40).grid(
        row=0, column=3, sticky="w", padx=6
    )

    # ── Output folder ─────────────────────────────────────────────────────────
    out_frame = tk.Frame(tab1)
    out_frame.pack(fill="x", padx=10, pady=(6, 0))

    output_folder_var = tk.StringVar(value=settings.get("output_folder", ""))
    output_label_var = tk.StringVar(
        value=settings.get("output_folder") or "(script output/ folder)"
    )

    def choose_output():
        folder = filedialog.askdirectory(title="Select OUTPUT folder for CSV log")
        if folder:
            output_folder_var.set(folder)
            output_label_var.set(folder)

    tk.Button(out_frame, text="Choose output folder…", command=choose_output).pack(side="left")
    tk.Label(out_frame, textvariable=output_label_var, fg="grey", anchor="w").pack(
        side="left", padx=8
    )

    # ── Smooth iterations ──────────────────────────────────────────────────────
    smooth_iter_frame = tk.Frame(tab1)
    smooth_iter_frame.pack(fill="x", padx=10, pady=(6, 0))
    tk.Label(smooth_iter_frame, text="Smooth iterations:", anchor="w").pack(side="left")
    smooth_iter_var = tk.IntVar(value=int(settings.get("smooth_iterations", 1)))
    tk.Spinbox(
        smooth_iter_frame, from_=0, to=20, width=5,
        textvariable=smooth_iter_var,
    ).pack(side="left", padx=(6, 0))
    tk.Label(
        smooth_iter_frame,
        text="(0 = no smoothing, snapshot only; edge-ring cells only)",
        fg="#888888",
    ).pack(side="left", padx=(8, 0))

    # ── Snapshot checkbox ─────────────────────────────────────────────────────
    snap_var = tk.BooleanVar(value=settings.get("save_snapshots", False))
    tk.Checkbutton(
        tab1,
        text="Save max-point snapshots (PNG per file)",
        variable=snap_var,
        anchor="w",
    ).pack(fill="x", padx=10, pady=(6, 0))

    # ── Config save / load ────────────────────────────────────────────────────
    cfg_frame = tk.Frame(tab1)
    cfg_frame.pack(fill="x", padx=10, pady=(8, 8))

    tk.Label(cfg_frame, text="Config file:", anchor="w").pack(side="left")
    cfg_path_var = tk.StringVar(value=str(SETTINGS_FILE))
    tk.Entry(cfg_frame, textvariable=cfg_path_var, width=55).pack(side="left", padx=(6, 4))

    def _current_cfg() -> dict:
        raw = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        return {
            "input_dirs":        dirs,
            "output_folder":     output_folder_var.get(),
            "pattern":           pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":       filter_var.get().strip(),
            "smooth_iterations": smooth_iter_var.get(),
            "save_snapshots":    snap_var.get(),
            "transform": {
                "preset":       xfm_preset_var.get(),
                "unit":         xfm_unit_var.get(),
                "output_unit":  xfm_out_unit_var.get(),
                "angle_deg":    xfm_angle_var.get(),
                "dx":           xfm_dx_var.get(),
                "dy":           xfm_dy_var.get(),
                "dz":           xfm_dz_var.get(),
                "pattern":      xfm_pattern_var.get(),
                "name_filter":  xfm_filter_var.get(),
                "export_geom":  xfm_export_geom.get(),
                "export_area":  xfm_export_area.get(),
                "export_power": xfm_export_power.get(),
                "export_pload": xfm_export_pload.get(),
                "mult":         xfm_mult_var.get(),
                "ignore_zeros": xfm_ignore_zeros.get(),
            },
        }

    def _apply_cfg(loaded: dict) -> None:
        text_box.delete("1.0", "end")
        if loaded.get("input_dirs"):
            text_box.insert("1.0", "\n".join(loaded["input_dirs"]))
        pattern_var.set(loaded.get("pattern", "smoothed_results_*.vtp"))
        filter_var.set(loaded.get("name_filter", ""))
        smooth_iter_var.set(int(loaded.get("smooth_iterations", 1)))
        snap_var.set(loaded.get("save_snapshots", False))
        out = loaded.get("output_folder", "")
        output_folder_var.set(out)
        output_label_var.set(out or "(script output/ folder)")
        xfm = loaded.get("transform", {})
        if xfm:
            _p = xfm.get("preset", "")
            if _p not in _trf.TRANSFORM_PRESETS:
                _p = list(_trf.TRANSFORM_PRESETS.keys())[0]
            xfm_preset_var.set(_p)
            on_preset_selected()
            xfm_unit_var.set(xfm.get("unit", "m"))
            xfm_out_unit_var.set(xfm.get("output_unit", xfm.get("unit", "m")))
            xfm_angle_var.set(xfm.get("angle_deg", "-116.0"))
            xfm_dx_var.set(xfm.get("dx", "11.410436"))
            xfm_dy_var.set(xfm.get("dy", "26.617882"))
            xfm_dz_var.set(xfm.get("dz", "0.920"))
            xfm_pattern_var.set(xfm.get("pattern", "smoothed_results_*.vtp"))
            xfm_filter_var.set(xfm.get("name_filter", ""))
            xfm_export_geom.set(bool(xfm.get("export_geom", True)))
            xfm_export_area.set(bool(xfm.get("export_area", True)))
            xfm_export_power.set(bool(xfm.get("export_power", True)))
            xfm_export_pload.set(bool(xfm.get("export_pload", True)))
            xfm_mult_var.set(str(xfm.get("mult", "1.0")))
            xfm_ignore_zeros.set(bool(xfm.get("ignore_zeros", False)))

    def on_save_cfg():
        path = filedialog.asksaveasfilename(
            title="Save config as…",
            initialfile=Path(cfg_path_var.get()).name,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        cfg_path_var.set(path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_current_cfg(), f, indent=2)
            log(f"Config saved: {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_load_cfg():
        path = filedialog.askopenfilename(
            title="Load config…",
            initialdir=str(Path(cfg_path_var.get()).parent),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        cfg_path_var.set(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            _apply_cfg(loaded)
            log(f"Config loaded: {path}")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def _open_cfg_file():
        path = Path(cfg_path_var.get())
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo("Not found", f"Config file does not exist yet:\n{path}")

    tk.Button(cfg_frame, text="Save config", width=11, command=on_save_cfg).pack(side="left", padx=2)
    tk.Button(cfg_frame, text="Load config", width=11, command=on_load_cfg).pack(side="left", padx=2)
    tk.Button(cfg_frame, text="Open file",   width=9,  command=_open_cfg_file).pack(side="left", padx=2)

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — Coordinate Transform
    # ══════════════════════════════════════════════════════════════════════════
    xfm_s = settings.get("transform", {})

    # ── Run button for tab 1 ───────────────────────────────────────────────────
    tab1_btn_frame = tk.Frame(tab1)
    tab1_btn_frame.pack(pady=(6, 10))
    tab1_run_btn = tk.Button(
        tab1_btn_frame, text="Run Processing", width=16, bg="#d4000e", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    tab1_run_btn.pack(side="left", padx=6)

    # ── Preset selector (two-row radio grid) ───────────────────────────────

    _first_preset = list(_trf.TRANSFORM_PRESETS.keys())[0]
    _saved_preset = xfm_s.get("preset", _first_preset)
    if _saved_preset not in _trf.TRANSFORM_PRESETS:
        _saved_preset = _first_preset
    xfm_preset_var = tk.StringVar(value=_saved_preset)

    # Hidden vars — updated by preset, consumed by _get_transform_params
    xfm_angle_var = tk.StringVar(value=xfm_s.get("angle_deg", str(_trf.DEFAULT_ANGLE_DEG)))
    xfm_dx_var    = tk.StringVar(value=xfm_s.get("dx",        str(_trf.DEFAULT_DX)))
    xfm_dy_var    = tk.StringVar(value=xfm_s.get("dy",        str(_trf.DEFAULT_DY)))
    xfm_dz_var    = tk.StringVar(value=xfm_s.get("dz",        str(_trf.DEFAULT_DZ)))
    xfm_unit_var  = tk.StringVar(value=xfm_s.get("unit", "m"))

    xfm_summary_var = tk.StringVar(value="")

    def on_preset_selected(event=None):
        name = xfm_preset_var.get()
        if name not in _trf.TRANSFORM_PRESETS:
            return
        p = _trf.TRANSFORM_PRESETS[name]
        xfm_angle_var.set(str(p["angle_deg"]))
        xfm_dx_var.set(str(p["dx"]))
        xfm_dy_var.set(str(p["dy"]))
        xfm_dz_var.set(str(p["dz"]))
        xfm_unit_var.set(p["unit"])
        xfm_summary_var.set(
            f"θz = {p['angle_deg']}°   Δx = {p['dx']} m   Δy = {p['dy']} m   Δz = {p['dz']} m"
        )

    on_preset_selected()  # sync hidden vars with selected preset on startup

    _presets_row0 = ["DNB → Tokamak",  "HNB1 → Tokamak", "HNB2 → Tokamak", "HNB3 → Tokamak"]
    _presets_row1 = ["Tokamak → DNB",  "Tokamak → HNB1", "Tokamak → HNB2", "Tokamak → HNB3"]
    _presets_row2 = ["No Transformation"]

    preset_lframe = tk.LabelFrame(tab2, text="Coordinate Transform Preset", padx=8, pady=6)
    preset_lframe.pack(fill="x", padx=10, pady=(8, 4))

    _radio_frame = tk.Frame(preset_lframe)
    _radio_frame.grid(row=0, column=0, sticky="nw", padx=(0, 8))

    tk.Label(_radio_frame, text="→ Tokamak:", fg="#444444", width=10, anchor="e").grid(
        row=0, column=0, sticky="e", padx=(0, 6)
    )
    for col, name in enumerate(_presets_row0):
        tk.Radiobutton(
            _radio_frame, text=name, variable=xfm_preset_var, value=name,
            command=on_preset_selected,
        ).grid(row=0, column=col + 1, sticky="w", padx=(0, 10))

    tk.Label(_radio_frame, text="Tokamak →:", fg="#444444", width=10, anchor="e").grid(
        row=1, column=0, sticky="e", padx=(0, 6)
    )
    for col, name in enumerate(_presets_row1):
        tk.Radiobutton(
            _radio_frame, text=name, variable=xfm_preset_var, value=name,
            command=on_preset_selected,
        ).grid(row=1, column=col + 1, sticky="w", padx=(0, 10))

    tk.Label(_radio_frame, text="Other:", fg="#444444", width=10, anchor="e").grid(
        row=2, column=0, sticky="e", padx=(0, 6)
    )
    for col, name in enumerate(_presets_row2):
        tk.Radiobutton(
            _radio_frame, text=name, variable=xfm_preset_var, value=name,
            command=on_preset_selected,
        ).grid(row=2, column=col + 1, sticky="w", padx=(0, 10))

    tk.Label(_radio_frame, text="", width=10).grid(row=3, column=0)
    tk.Label(
        _radio_frame, textvariable=xfm_summary_var,
        fg="#555555", font=("Consolas", 8), anchor="w",
    ).grid(row=3, column=1, columnspan=4, sticky="w", pady=(4, 0))

    # Coordinate image on the right inside preset_lframe
    _coord_img_path = str(Path(__file__).resolve().parent / "coordinates.png")
    _coord_photo = None
    try:
        from PIL import Image as _PILImage, ImageTk as _PILImageTk
        _pil = _PILImage.open(_coord_img_path)
        _target_h = 70
        _target_w = int(_target_h * _pil.width / _pil.height)
        _pil = _pil.resize((_target_w, _target_h), _PILImage.LANCZOS)
        _coord_photo = _PILImageTk.PhotoImage(_pil)
    except Exception:
        try:
            _raw = tk.PhotoImage(file=_coord_img_path)
            _factor = max(1, _raw.height() // 70)
            _coord_photo = _raw.subsample(_factor, _factor)
        except Exception:
            _coord_photo = None

    if _coord_photo is not None:
        _img_lbl = tk.Label(preset_lframe, image=_coord_photo)
        _img_lbl.image = _coord_photo
        _img_lbl.grid(row=0, column=1, padx=(16, 4), pady=2, sticky="ns")

    # ── Unit ──────────────────────────────────────────────────────────────────
    unit_frame = tk.Frame(tab2)
    unit_frame.pack(fill="x", padx=10, pady=(4, 0))
    tk.Label(unit_frame, text="Input coordinate unit:", anchor="w", width=22).pack(side="left")
    for unit_label in ("mm", "m"):
        tk.Radiobutton(
            unit_frame, text=unit_label, variable=xfm_unit_var, value=unit_label
        ).pack(side="left", padx=4)

    out_unit_frame = tk.Frame(tab2)
    out_unit_frame.pack(fill="x", padx=10, pady=(2, 0))
    tk.Label(out_unit_frame, text="Output coordinate unit:", anchor="w", width=22).pack(side="left")
    xfm_out_unit_var = tk.StringVar(value=xfm_s.get("output_unit", xfm_s.get("unit", "m")))
    for unit_label in ("mm", "m"):
        tk.Radiobutton(
            out_unit_frame, text=unit_label, variable=xfm_out_unit_var, value=unit_label
        ).pack(side="left", padx=4)
    tk.Label(out_unit_frame, text="(no conversion = same as input)",
             fg="#888888").pack(side="left", padx=(8, 0))


    # ── File selection ────────────────────────────────────────────────────────
    xfm_opt_frame = tk.Frame(tab2)
    xfm_opt_frame.pack(fill="x", padx=10, pady=(8, 0))
    tk.Label(xfm_opt_frame, text="Glob pattern:", anchor="w").grid(row=0, column=0, sticky="w")
    xfm_pattern_var = tk.StringVar(value=xfm_s.get("pattern", "smoothed_results_*.vtp"))
    tk.Entry(xfm_opt_frame, textvariable=xfm_pattern_var, width=40).grid(
        row=0, column=1, sticky="w", padx=(6, 20)
    )
    tk.Label(xfm_opt_frame, text="Name filter (comma-separated):", anchor="w").grid(
        row=0, column=2, sticky="w"
    )
    xfm_filter_var = tk.StringVar(value=xfm_s.get("name_filter", ""))
    tk.Entry(xfm_opt_frame, textvariable=xfm_filter_var, width=40).grid(
        row=0, column=3, sticky="w", padx=6
    )

    # ── Export options ────────────────────────────────────────────────────────
    export_lframe = tk.LabelFrame(tab2, text="Properties to export", padx=8, pady=4)
    export_lframe.pack(fill="x", padx=10, pady=(8, 4))
    _exp_row = tk.Frame(export_lframe)
    _exp_row.pack(fill="x")
    xfm_export_geom = tk.BooleanVar(value=bool(xfm_s.get("export_geom", True)))
    tk.Checkbutton(_exp_row, text="Geometry (X, Y, Z)",
                   variable=xfm_export_geom).pack(side="left", padx=(0, 12))
    xfm_export_area = tk.BooleanVar(value=bool(xfm_s.get("export_area", True)))
    tk.Checkbutton(_exp_row, text="Cell area",
                   variable=xfm_export_area).pack(side="left", padx=(0, 12))
    xfm_export_power = tk.BooleanVar(value=bool(xfm_s.get("export_power", True)))
    tk.Checkbutton(_exp_row, text="Power (Deposited_Power_W)",
                   variable=xfm_export_power).pack(side="left", padx=(0, 12))
    xfm_export_pload = tk.BooleanVar(value=bool(xfm_s.get("export_pload", True)))
    tk.Checkbutton(_exp_row, text="Power load (Power_Density_W_m2)",
                   variable=xfm_export_pload).pack(side="left")

    xfm_misc_frame = tk.Frame(tab2)
    xfm_misc_frame.pack(fill="x", padx=10, pady=(2, 4))
    tk.Label(xfm_misc_frame, text="Multiplication factor:",
             font=("Segoe UI", 9, "bold")).pack(side="left")
    xfm_mult_var = tk.StringVar(value=str(xfm_s.get("mult", "1.0")))
    tk.Entry(xfm_misc_frame, textvariable=xfm_mult_var, width=10).pack(side="left", padx=(8, 0))
    tk.Label(xfm_misc_frame, text="(applied to power & power load)",
             fg="#64748b").pack(side="left", padx=(8, 20))
    xfm_ignore_zeros = tk.BooleanVar(value=bool(xfm_s.get("ignore_zeros", False)))
    tk.Checkbutton(xfm_misc_frame, text="Ignore zero-valued rows",
                   variable=xfm_ignore_zeros).pack(side="left")

    # ── Run button for tab 2 ─────────────────────────────────────────────────
    tab2_btn_frame = tk.Frame(tab2)
    tab2_btn_frame.pack(pady=(8, 10))
    tab2_run_btn = tk.Button(
        tab2_btn_frame, text="Run Transform", width=16, bg="#0060c0", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    tab2_run_btn.pack(side="left", padx=6)

    # ── Shared Log area ───────────────────────────────────────────────────────
    tk.Label(root, text="Log:", anchor="w").pack(fill="x", padx=10, pady=(10, 2))
    log_frame = tk.Frame(root)
    log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
    log_scroll = tk.Scrollbar(log_frame)
    log_scroll.pack(side="right", fill="y")
    log_box = tk.Text(
        log_frame, width=90, height=14, state="disabled",
        bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9),
        yscrollcommand=log_scroll.set, wrap="none",
    )
    log_box.pack(side="left", fill="both", expand=True)
    log_scroll.config(command=log_box.yview)

    def log(msg: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    # ── Run Both + Stop ───────────────────────────────────────────────────────
    _stop_event = threading.Event()
    _active_workers = [0]   # mutable counter shared across closures

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(4, 10))

    run_both_btn = tk.Button(
        btn_frame, text="Run Both", width=12, bg="#6a0dad", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    run_both_btn.pack(side="left", padx=6)

    stop_btn = tk.Button(
        btn_frame, text="Stop", width=12, bg="#555555", fg="white",
        font=("Segoe UI", 10, "bold"), state="disabled",
    )
    stop_btn.pack(side="left", padx=6)

    def on_stop():
        _stop_event.set()
        stop_btn.configure(state="disabled", text="Stopping…")

    stop_btn.configure(command=on_stop)

    # ── All run buttons list (for bulk disable/enable) ────────────────────────
    _all_run_btns = [tab1_run_btn, tab2_run_btn, run_both_btn]

    def _set_busy():
        _stop_event.clear()
        stop_btn.configure(state="normal", text="Stop")
        for b in _all_run_btns:
            b.configure(state="disabled")
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")

    def _on_worker_done():
        _active_workers[0] -= 1
        if _active_workers[0] <= 0:
            _active_workers[0] = 0
            for b in _all_run_btns:
                b.configure(state="normal")
            stop_btn.configure(state="disabled", text="Stop")

    def _get_input_dirs() -> list[str] | None:
        raw = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        if not dirs:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return None
        return dirs

    def _get_transform_params() -> dict | None:
        try:
            angle = float(xfm_angle_var.get())
            dx    = float(xfm_dx_var.get())
            dy    = float(xfm_dy_var.get())
            dz    = float(xfm_dz_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Transform parameters must be numeric.")
            return None
        try:
            mult = float(xfm_mult_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Multiplication factor must be a number.")
            return None
        exp_geom  = xfm_export_geom.get()
        exp_area  = xfm_export_area.get()
        exp_power = xfm_export_power.get()
        exp_pload = xfm_export_pload.get()
        if not any([exp_geom, exp_area, exp_power, exp_pload]):
            messagebox.showwarning("Nothing selected",
                                   "Select at least one property to export.")
            return None
        unit = xfm_unit_var.get()
        output_unit = xfm_out_unit_var.get()
        # Scale factor: convert from input_unit to output_unit
        unit_to_m = {"m": 1.0, "mm": 0.001}
        coord_scale = unit_to_m[unit] / unit_to_m[output_unit]
        return {
            "angle_deg":    angle,
            "dx": dx, "dy": dy, "dz": dz,
            "unit":         unit,
            "output_unit":  output_unit,
            "coord_scale":  coord_scale,
            "pattern":      xfm_pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":  xfm_filter_var.get().strip(),
            "export_geom":  exp_geom,
            "export_area":  exp_area,
            "export_power": exp_power,
            "export_pload": exp_pload,
            "mult":         mult,
            "ignore_zeros": xfm_ignore_zeros.get(),
        }

    def _launch_processing(cfg: dict) -> None:
        def worker():
            try:
                run_processing(cfg, log, _stop_event)
            finally:
                root.after(0, _on_worker_done)
        _active_workers[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _launch_transform(input_dirs: list, xfm_params: dict, out_folder: str) -> None:
        def worker():
            try:
                run_transform(
                    input_dirs=input_dirs,
                    xfm_params=xfm_params,
                    output_folder=out_folder,
                    log=log,
                    stop_event=_stop_event,
                )
            finally:
                root.after(0, _on_worker_done)
        _active_workers[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def on_run_processing():
        cfg = _current_cfg()
        if not cfg["input_dirs"]:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return
        save_settings(cfg)
        _set_busy()
        _launch_processing(cfg)

    def on_run_transform():
        input_dirs = _get_input_dirs()
        if input_dirs is None:
            return
        xfm_params = _get_transform_params()
        if xfm_params is None:
            return
        cfg = _current_cfg()
        save_settings(cfg)
        _set_busy()
        _launch_transform(input_dirs, xfm_params, output_folder_var.get())

    def on_run_both():
        input_dirs = _get_input_dirs()
        if input_dirs is None:
            return
        xfm_params = _get_transform_params()
        if xfm_params is None:
            return
        cfg = _current_cfg()
        if not cfg["input_dirs"]:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return
        save_settings(cfg)
        _set_busy()
        _launch_processing(cfg)
        _launch_transform(input_dirs, xfm_params, output_folder_var.get())

    tab1_run_btn.configure(command=on_run_processing)
    tab2_run_btn.configure(command=on_run_transform)
    run_both_btn.configure(command=on_run_both)
    root.mainloop()



# ── Main ──────────────────────────────────────────────────────────────────────
def run_processing(cfg: dict, log, stop_event: threading.Event | None = None) -> None:
    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()
    input_dirs       = cfg["input_dirs"]
    pattern          = cfg["pattern"]
    name_filter      = cfg.get("name_filter", "")
    smooth_iterations = int(cfg.get("smooth_iterations", 1))
    save_snapshots   = cfg.get("save_snapshots", False)

    # Expand any OUTPUT_* folder into its immediate subfolders
    expanded_dirs = []
    for d in input_dirs:
        p = Path(d)
        if p.is_dir() and p.name.upper().startswith("OUTPUT_"):
            subfolders = [s for s in sorted(p.iterdir()) if s.is_dir()]
            if subfolders:
                log(f"Expanding {p.name} into {len(subfolders)} subfolder(s).")
                expanded_dirs.extend(subfolders)
            else:
                log(f"[WARN] {p.name} folder is empty: {p}")
        else:
            expanded_dirs.append(p)
    input_dirs = expanded_dirs

    # Output folder: user choice, or fall back to script's own output/ folder
    script_dir = Path(__file__).resolve().parent
    out_dir = Path(cfg["output_folder"]) if cfg["output_folder"] else script_dir / "output"
    os.makedirs(out_dir, exist_ok=True)

    snap_dir = out_dir / "snapshots"
    if save_snapshots:
        snap_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "max_comparison_batch.csv"

    total_files = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "case", "scenario", "filename",
            "max_before", "max_after",
            "delta", "discrepancy",
        ])

        # ── Collect all matching files across every folder first ──────────
        all_files: list[tuple[Path, str, str, str]] = []  # (path, output_name, case, scenario)
        for input_folder in input_dirs:
            if stopped():
                break
            input_path = Path(input_folder)
            output_name, folder_case, _ = extract_case_scenario(str(input_path))

            files = sorted(input_path.rglob(pattern))
            if name_filter:
                terms = [t.strip().lower() for t in name_filter.split(",") if t.strip()]
                files = [f for f in files if any(t in f.stem.lower() for t in terms)]
            if not files:
                filter_note = f" containing '{name_filter}'" if name_filter else ""
                log(f"[SKIP] No files matching '{pattern}'{filter_note} in (or below):\n  {input_path}")
                continue

            log(f"  {folder_case}: {len(files)} file(s) found")
            for filepath in files:
                _, case, scenario = extract_case_scenario(str(filepath.parent))
                all_files.append((filepath, output_name, case, scenario))

        if not all_files:
            log("No files found.")
            return

        log(f"\nTotal: {len(all_files)} file(s) to process")
        log("=" * 80)

        snapshot_only = (smooth_iterations == 0)

        # ── Stage 1: Load VTP files (parallel I/O) ───────────────────────
        # ThreadPool.imap_unordered feeds one item per idle worker — at most
        # n_workers files live in memory at once.  I/O-bound work benefits
        # from a pool larger than cpu_count; cap at 12 to stay reasonable.
        n_all     = len(all_files)
        n_workers = min(os.cpu_count() or 4, n_all, 10)
        log(f"\n[1/3] Loading {n_all} VTP file(s) ({n_workers} workers)...")
        loaded: list[tuple] = []
        t0_load = time.perf_counter()

        with ThreadPool(processes=n_workers) as pool:
            for completed, result in enumerate(
                    pool.imap_unordered(_load_one_file, all_files), 1):
                if stopped():
                    pool.terminate()
                    break
                elapsed = time.perf_counter() - t0_load
                if isinstance(result, Exception):
                    log(f"  [{completed}/{n_all}] ERROR: {result}")
                    continue
                filepath, output_name, case, scenario, polydata, max_val = result
                if max_val is None:
                    log(f"  [{completed}/{n_all}] SKIP  {filepath.name}  (no '{ARRAY_NAME}')")
                    continue
                loaded.append((filepath, output_name, case, scenario, polydata, max_val))
                log(f"  [{completed}/{n_all}] {filepath.name}  (elapsed: {elapsed:.1f}s)")

        log(f"  Loading done: {len(loaded)} file(s) in {time.perf_counter()-t0_load:.1f}s")

        # ── Stage 2: Smoothing (skipped when smooth_iterations == 0) ─────
        processed: list[tuple] = []  # (..., polydata_orig, max_before, polydata_smooth, max_after)
        if snapshot_only:
            log("\n[2/3] Smoothing skipped (iterations = 0)")
            for item in loaded:
                filepath, output_name, case, scenario, polydata, max_val = item
                processed.append((filepath, output_name, case, scenario,
                                   polydata, max_val, polydata, max_val))
        else:
            n_loaded = len(loaded)
            log(f"\n[2/3] Smoothing {n_loaded} file(s) ({smooth_iterations} iteration(s) each)...")
            t0_smooth = time.perf_counter()
            for i, item in enumerate(loaded, 1):
                if stopped():
                    break
                filepath, output_name, case, scenario, polydata, max_before = item
                t_file = time.perf_counter()
                smoothed  = apply_edge_smooth(polydata, n_iter=smooth_iterations)
                max_after = find_max(smoothed, ARRAY_NAME)
                elapsed_file = time.perf_counter() - t_file
                processed.append((filepath, output_name, case, scenario,
                                   polydata, max_before, smoothed, max_after))
                log(f"  [{i}/{n_loaded}] {filepath.name}  ({elapsed_file:.1f}s)  "
                    f"before={max_before:.4g}  after={max_after:.4g}")
            log(f"  Smoothing done in {time.perf_counter()-t0_smooth:.1f}s")

        # ── Write CSV rows ────────────────────────────────────────────────
        for item in processed:
            filepath, output_name, case, scenario, _, max_before, _, max_after = item
            delta       = abs(max_after - max_before)
            discrepancy = "YES" if delta > 0.0 else "NO"
            writer.writerow([
                case, scenario, filepath.name,
                f"{max_before:.6g}", f"{max_after:.6g}",
                f"{delta:.6g}", discrepancy,
            ])
            total_files += 1

            if not snapshot_only:
                log(f"  {filepath.name}: before={max_before:.6g}  after={max_after:.6g}  delta={abs(max_after - max_before):.6g}")

        # ── Stage 3: Snapshots (parallel rendering) ─────────────────────────
        if save_snapshots:
            n_proc = len(processed)
            # Rendering is single-threaded: multiple vtkRenderWindow (WGL) contexts
            # competing for the same GPU command queue starve the Tkinter compositor
            # and freeze the GUI.  Per-file time is already ~6s (MSAA disabled) so
            # sequential rendering finishes in acceptable time without GUI instability.
            n_snap_workers = 1
            log(f"\n[3/3] Saving {n_proc} snapshot(s) ({n_snap_workers} workers)...")
            t0_snap = time.perf_counter()
            snap_args = [
                (filepath, output_name, case, scenario,
                 polydata_orig, polydata_smooth, snap_dir, snapshot_only)
                for filepath, output_name, case, scenario,
                    polydata_orig, _, polydata_smooth, _ in processed
            ]
            with ThreadPool(processes=n_snap_workers) as pool:
                for completed, result in enumerate(
                        pool.imap_unordered(_save_one_snapshot, snap_args), 1):
                    if stopped():
                        pool.terminate()
                        break
                    elapsed = time.perf_counter() - t0_snap
                    if isinstance(result, Exception):
                        log(f"  [{completed}/{n_proc}] ERROR: {result}")
                        continue
                    label, file_elapsed = result
                    log(f"  [{completed}/{n_proc}] {label}  ({file_elapsed:.1f}s)")
            log(f"  Snapshots done in {time.perf_counter()-t0_snap:.1f}s")
        else:
            log("\n[3/3] Snapshots disabled")

    log("\n" + "=" * 80)
    if stopped():
        log(f"STOPPED by user after {total_files} file(s).")
    else:
        log(f"Processed {total_files} file(s) across {len(input_dirs)} folder(s).")
    log(f"CSV log saved to:\n  {csv_path}")


def run_transform(
    input_dirs: list,
    xfm_params: dict,
    output_folder: str,
    log,
    stop_event: threading.Event | None = None,
) -> None:
    """
    For each matching .vtp file:
      1. Extract per-cell data (X, Y, Z, Area, Deposited_Power_W,
         Power_Density_W_m2) to an intermediate CSV via generate_report.
      2. Transform the X/Y/Z coordinates using transform_reference_frame
         and write the final CSV to the output folder.
    """

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    pattern       = xfm_params["pattern"]
    name_filter   = xfm_params["name_filter"]
    angle_deg     = xfm_params["angle_deg"]
    dx            = xfm_params["dx"]
    dy            = xfm_params["dy"]
    dz            = xfm_params["dz"]
    export_geom   = xfm_params.get("export_geom",  True)
    export_area   = xfm_params.get("export_area",  True)
    export_power  = xfm_params.get("export_power", True)
    export_pload  = xfm_params.get("export_pload", True)
    mult          = float(xfm_params.get("mult", 1.0))
    ignore_zeros  = xfm_params.get("ignore_zeros", False)
    coord_scale   = float(xfm_params.get("coord_scale", 1.0))

    # Expand OUTPUT_* dirs
    expanded = []
    for d in input_dirs:
        p = Path(d)
        if p.is_dir() and p.name.upper().startswith("OUTPUT_"):
            subs = [s for s in sorted(p.iterdir()) if s.is_dir()]
            if subs:
                log(f"Expanding {p.name} into {len(subs)} subfolder(s).")
                expanded.extend(subs)
            else:
                log(f"[WARN] {p.name} is empty: {p}")
        else:
            expanded.append(p)

    script_dir = Path(__file__).resolve().parent
    out_root = Path(output_folder) if output_folder else script_dir / "output"

    total = 0
    for folder in expanded:
        if stopped():
            break
        folder = Path(folder)
        output_name, folder_case, folder_scenario = extract_case_scenario(str(folder))

        files = sorted(folder.rglob(pattern))
        if name_filter:
            terms = [t.strip().lower() for t in name_filter.split(",") if t.strip()]
            files = [f for f in files if any(t in f.stem.lower() for t in terms)]
        if not files:
            note = f" containing '{name_filter}'" if name_filter else ""
            log(f"[SKIP] No files matching '{pattern}'{note} in:\n  {folder}")
            continue

        log(f"\nCase     : {folder_case}")
        log(f"Scenario : {folder_scenario}")
        log(f"Files    : {len(files)}")
        log("=" * 80)

        for filepath in files:
            if stopped():
                break
            # Derive case and scenario from each file's parent path so that
            # intermediate grouping folders (e.g. DNB_ALL) are skipped.
            _, case, scenario = extract_case_scenario(str(filepath.parent))
            dest_dir = out_root / "transformed" / output_name / case
            dest_dir.mkdir(parents=True, exist_ok=True)
            csv_stem = filepath.stem + ".csv"
            out_path = dest_dir / csv_stem
            log(f"\n[{filepath.name}]")
            try:
                # Step 1: extract per-cell data from .vtp → intermediate CSV
                tmp_csv = out_path.with_suffix(".tmp.csv")
                extract_cells_to_csv(
                    str(filepath), str(tmp_csv),
                    export_geom=export_geom,
                    export_area=export_area,
                    export_power=export_power,
                    export_pload=export_pload,
                    mult=mult,
                    ignore_zeros=ignore_zeros,
                )
                log(f"  Extracted cells → {tmp_csv.name}")

                # Step 2: apply coordinate transform → final CSV
                _trf.process_file(
                    input_path=tmp_csv,
                    output_path=out_path,
                    x_col=None,
                    y_col=None,
                    z_col=None,
                    angle_deg=angle_deg,
                    dx=dx,
                    dy=dy,
                    dz=dz,
                    coord_scale=coord_scale,
                )
                tmp_csv.unlink(missing_ok=True)
                log(f"  Transformed CSV → {out_path}")
                total += 1
            except Exception as exc:
                log(f"  [ERROR] {exc}")

    log("\n" + "=" * 80)
    if stopped():
        log(f"STOPPED after {total} file(s) transformed.")
    else:
        log(f"Transformed {total} file(s).")
    log(f"Output root: {out_root / 'transformed'}")


def main():
    run_gui()



# ── Path helpers ──────────────────────────────────────────────────────────────
def _looks_like_scenario(name: str) -> bool:
    """Return True if name looks like a scenario code (e.g. 'dnb_3_+10_+2').
    Scenarios contain a sign followed immediately by a digit.
    """
    return bool(re.search(r'[+\-]\d', name))


def extract_case_scenario(folder):
    """
    Find the OUTPUT_* ancestor and return (output_name, case, scenario).

    Handles any depth of nesting below OUTPUT_* with a bottom-up rule:
      - 0 sub-levels  → case = scenario = output_suffix
      - 1 sub-level   → case = suffix if it looks like a scenario, else sub[0];
                         scenario = sub[0]
      - 2 sub-levels  → case = sub[0] unless sub[0] looks like a scenario
                         (in which case case = output_suffix); scenario = sub[0]
      - 3+ sub-levels → case = sub[-2], scenario = sub[-1]
                         (ignores intermediate grouping folders like 'DNB_ALL')

    Falls back to ("snapshots", parts[-2], parts[-1]) if no OUTPUT_* is found.
    """
    parts = Path(folder).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.upper().startswith("OUTPUT_"))
        output_name   = parts[idx]
        output_suffix = output_name[len("OUTPUT_"):]
        sub = parts[idx + 1:]   # everything after OUTPUT_*

        if len(sub) == 0:
            case = output_suffix or "unknown"
            scenario = case
        elif len(sub) == 1:
            if _looks_like_scenario(sub[0]):
                case = output_suffix or sub[0]
            else:
                case = sub[0]
            scenario = sub[0]
        elif len(sub) == 2:
            if _looks_like_scenario(sub[0]):
                # OUTPUT_FFTC / dnb_3_+10_+2 / ...
                case     = output_suffix or sub[0]
                scenario = sub[0]
            else:
                case     = sub[0]
                scenario = sub[1]
        else:
            # 3+ levels: skip intermediate grouping folders; use last two
            case     = sub[-2]
            scenario = sub[-1]
    except StopIteration:
        output_name = "snapshots"
        case        = parts[-2] if len(parts) >= 2 else "unknown"
        scenario    = parts[-1] if len(parts) >= 1 else "unknown"
    return output_name, case, scenario


# ── VTK I/O ───────────────────────────────────────────────────────────────────
def read_vtp(filepath):
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(filepath)
    reader.Update()
    return reader.GetOutput()


def _load_one_file(args: tuple) -> tuple:
    """Load one VTP file and extract the max scalar value.  Called from a thread pool.
    Returns the result tuple, or the Exception on failure (never raises — keeps the pool alive)."""
    try:
        filepath, output_name, case, scenario = args
        polydata = read_vtp(str(filepath))
        max_val  = find_max(polydata, ARRAY_NAME)
        return filepath, output_name, case, scenario, polydata, max_val
    except Exception as exc:
        return exc


def _save_one_snapshot(args: tuple) -> tuple:
    """Render and save snapshot(s) for one processed item.  Called from a thread pool.

    THREAD SAFETY: DeepCopy is performed FIRST to move polydata ownership into this
    worker thread.  ALL subsequent VTK operations (precompute + render) run on the
    local copy, so no cross-thread VTK object access occurs after the initial copy.
    Returns (label, elapsed_s) or an Exception."""
    try:
        (filepath, output_name, case, scenario,
         polydata_orig, polydata_smooth, snap_dir, snapshot_only) = args

        # ── Step 1: DeepCopy into this thread first (minimal cross-thread touch) ──
        # vtkCellLocator.SetDataSet() increments the ref-count on its polydata arg,
        # which is a write — not safe to do on cross-thread VTK objects.  DeepCopy is
        # the only cross-thread VTK call and is safe as a single read operation.
        pd_orig = vtk.vtkPolyData()
        pd_orig.DeepCopy(polydata_orig)

        # ── Step 2: Pre-compute on the local copy (CPU, no GPU, single thread) ──
        pre_orig = precompute_snapshot(pd_orig, ARRAY_NAME)

        stem = filepath.stem
        case_snap_dir = snap_dir / output_name / case
        case_snap_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()

        # ── Step 3: Render from the local copy ───────────────────────────────────
        if snapshot_only:
            png_snap = case_snap_dir / f"{scenario}__{stem}.png"
            save_max_snapshot(pd_orig, ARRAY_NAME, png_snap, precomputed=pre_orig)
            return png_snap.name, time.perf_counter() - t0
        else:
            pd_smooth = vtk.vtkPolyData()
            pd_smooth.DeepCopy(polydata_smooth)
            pre_smooth = precompute_snapshot(pd_smooth, ARRAY_NAME)
            png_before = case_snap_dir / f"{scenario}__{stem}__before.png"
            save_max_snapshot(pd_orig, ARRAY_NAME, png_before, precomputed=pre_orig)
            png_after  = case_snap_dir / f"{scenario}__{stem}__after.png"
            save_max_snapshot(pd_smooth, ARRAY_NAME, png_after, precomputed=pre_smooth)
            return stem, time.perf_counter() - t0
    except Exception as exc:
        return exc


def find_max(polydata, array_name):
    """Return the maximum value of *array_name* in cell data, or None."""
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return None
    n = arr.GetNumberOfTuples()
    if n == 0:
        return None
    return float(vtk_to_numpy(arr).max())


# ── Smoothing (mirrors Smart_Smooth_EDGE.py) ──────────────────────────────────
def apply_edge_smooth(src, n_iter: int = 1):
    """
    Iterative neighbour-mean smoothing restricted to cells that touch
    boundary or feature edges (angle >= FEATURE_ANGLE degrees).

    Each iteration averages only the edge-ring cells (identified once before
    the loop), so locality is preserved — interior cells are never touched.
    n_iter > 1 lets a very high spike diffuse across several rings of
    edge-adjacent cells without spreading into the bulk of the mesh.

    Returns a NEW vtkPolyData — src is never modified.
    """
    out = vtk.vtkPolyData()
    out.DeepCopy(src)                          # independent copy

    in_arr  = src.GetCellData().GetArray(ARRAY_NAME)
    out_arr = out.GetCellData().GetArray(ARRAY_NAME)
    if in_arr is None or out_arr is None:
        print(f"  [SKIP] Array '{ARRAY_NAME}' not found in cell data.")
        return out

    n_cells  = in_arr.GetNumberOfTuples()
    raw_vals = vtk_to_numpy(in_arr).astype(float, copy=True)

    # Step 1 — extract boundary + feature edges
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(src)
    fe.BoundaryEdgesOn()
    fe.FeatureEdgesOn()
    fe.SetFeatureAngle(FEATURE_ANGLE)
    fe.NonManifoldEdgesOff()
    fe.ManifoldEdgesOff()
    fe.ColoringOff()
    fe.Update()

    # Step 2 — build a coordinate key-set from edge-output points (numpy, no VTK loop)
    edge_pts  = fe.GetOutput().GetPoints()
    edge_keys: set = set()
    if edge_pts and edge_pts.GetNumberOfPoints() > 0:
        ep_np = vtk_to_numpy(edge_pts.GetData()).reshape(-1, 3)
        _sc   = np.int64(10 ** 10)
        for row in np.round(ep_np * _sc).astype(np.int64):
            edge_keys.add((int(row[0]), int(row[1]), int(row[2])))
    print(f"  Feature-edge points : {len(edge_keys)}")

    # Step 3 — map edge coordinates back to source point IDs (numpy, no GetPoint loop)
    src_pts = src.GetPoints()
    edge_pt_ids: set = set()
    if src_pts and edge_keys:
        sp_np   = vtk_to_numpy(src_pts.GetData()).reshape(-1, 3)
        _sc     = np.int64(10 ** 10)
        sp_keys = np.round(sp_np * _sc).astype(np.int64)
        for pid, row in enumerate(sp_keys):
            if (int(row[0]), int(row[1]), int(row[2])) in edge_keys:
                edge_pt_ids.add(pid)

    # Step 4 — flag cells that own at least one edge point
    cell_pts  = vtk.vtkIdList()
    edge_cells = set()
    for cid in range(n_cells):
        src.GetCellPoints(cid, cell_pts)
        for k in range(cell_pts.GetNumberOfIds()):
            if cell_pts.GetId(k) in edge_pt_ids:
                edge_cells.add(cid)
                break
    print(f"  Edge-ring cells     : {len(edge_cells)}")

    if not edge_cells:
        print("  Nothing to smooth.")
        return out

    # Step 5 — iterative mean of point-connected neighbours (edge-ring only)
    # The edge-ring cell set is fixed once; only their values change each pass.
    n_iter = max(1, int(n_iter))
    current_vals = np.copy(raw_vals)
    nbr_ids      = vtk.vtkIdList()
    for iteration in range(n_iter):
        next_vals = np.copy(current_vals)
        for cid in edge_cells:
            src.GetCellPoints(cid, cell_pts)
            nbr_vals = []
            for k in range(cell_pts.GetNumberOfIds()):
                src.GetPointCells(cell_pts.GetId(k), nbr_ids)
                for m in range(nbr_ids.GetNumberOfIds()):
                    ncid = nbr_ids.GetId(m)
                    if ncid != cid:
                        nbr_vals.append(current_vals[ncid])
            if nbr_vals:
                next_vals[cid] = float(np.mean(nbr_vals))
        current_vals = next_vals
        if n_iter > 1:
            print(f"  Smooth pass {iteration + 1}/{n_iter} done.")

    # Step 6 — write smoothed values back via numpy_to_vtk (avoids SetValue loop)
    new_arr = numpy_to_vtk(current_vals, deep=True, array_type=out_arr.GetDataType())
    new_arr.SetName(ARRAY_NAME)
    out.GetCellData().RemoveArray(ARRAY_NAME)
    out.GetCellData().AddArray(new_arr)

    return out


if __name__ == "__main__":
    main()
