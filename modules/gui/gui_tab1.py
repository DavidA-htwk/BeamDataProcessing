"""
modules/gui_tab1.py
-------------------
Builder for the Processing tab (Tab 1) of the main GUI.

Call build_processing_tab(tab1, settings, log_fn) to populate the frame and
receive a state dict with all Tkinter variables and the _comp_widgets reference
needed by the run callbacks in Data_handling.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

from modules.core.settings import SMOOTH_PROXIMITY_RADIUS, SETTINGS_FILE, _safe_float, remember_cfg_path


def build_processing_tab(tab1: tk.Frame, settings: dict, log_fn) -> dict:
    """Populate *tab1* with all Processing-tab widgets.

    Returns a dict with keys needed by run-callbacks in Data_handling.py:
        text_box, pattern_var, filter_var, output_folder_var, output_label_var,
        proximity_var, prox_entry, comp_widgets, pending_comp_cfg,
        load_geo_status, cfg_path_var, tab1_run_btn
    """
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
        row=0, column=1, sticky="w", padx=(6, 20))
    tk.Label(opt_frame, text="Name filter (comma-separated):", anchor="w").grid(
        row=0, column=2, sticky="w")
    filter_var = tk.StringVar(value=settings.get("name_filter", ""))
    tk.Entry(opt_frame, textvariable=filter_var, width=40).grid(
        row=0, column=3, sticky="w", padx=6)

    # ── Output folder ─────────────────────────────────────────────────────────
    out_frame = tk.Frame(tab1)
    out_frame.pack(fill="x", padx=10, pady=(6, 0))
    output_folder_var = tk.StringVar(value=settings.get("output_folder", ""))
    output_label_var  = tk.StringVar(
        value=settings.get("output_folder") or "(script output/ folder)")

    def choose_output():
        folder = filedialog.askdirectory(title="Select OUTPUT folder for CSV log")
        if folder:
            output_folder_var.set(folder)
            output_label_var.set(folder)

    tk.Button(out_frame, text="Choose output folder…", command=choose_output).pack(side="left")
    tk.Label(out_frame, textvariable=output_label_var, fg="grey", anchor="w").pack(
        side="left", padx=8)

    # ── Proximity radius ──────────────────────────────────────────────────────
    prox_frame = tk.Frame(tab1)
    prox_frame.pack(fill="x", padx=10, pady=(6, 0))
    tk.Label(prox_frame, text="Proximity radius:", anchor="w").pack(side="left")
    proximity_var = tk.StringVar(
        value=str(settings.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS)))
    prox_entry = tk.Entry(prox_frame, textvariable=proximity_var, width=8)
    prox_entry.pack(side="left", padx=(6, 0))
    prox_entry.configure(state="disabled")
    tk.Label(prox_frame,
             text="(mesh units; Smoothes around detected edges for that value; 0 = off)",
             fg="#888888").pack(side="left", padx=(8, 0))

    # ── Load Geometry ─────────────────────────────────────────────────────────
    load_geo_frame = tk.Frame(tab1)
    load_geo_frame.pack(fill="x", padx=10, pady=(8, 0))
    load_geo_btn = tk.Button(
        load_geo_frame, text="Load Geometry", width=16, bg="#005f73", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    load_geo_btn.pack(side="left")
    load_geo_status = tk.StringVar(value="  (scan folders to detect components)")
    tk.Label(load_geo_frame, textvariable=load_geo_status, fg="#555555", anchor="w").pack(
        side="left", padx=6)

    comp_lframe = tk.LabelFrame(tab1, text="Components", padx=8, pady=4)
    comp_lframe.pack(fill="x", padx=10, pady=(6, 0))

    _comp_hdr = tk.Frame(comp_lframe)
    _comp_hdr.pack(fill="x")
    for txt, w in [("Component", 24), ("Files", 8), ("Smooth iter", 10),
                   ("Mult factor", 10), ("Snap pwr dens", 14), ("Snap total pwr", 14)]:
        tk.Label(_comp_hdr, text=txt, width=w, anchor="w",
                 fg="#444444", font=("Segoe UI", 8, "bold")).pack(side="left")

    comp_rows_frame = tk.Frame(comp_lframe)
    comp_rows_frame.pack(fill="x")

    comp_widgets:     dict = {}
    pending_comp_cfg: dict = dict(settings.get("components", {}))

    tk.Label(comp_rows_frame, text="(click Load Geometry to populate)",
             fg="#aaaaaa").pack(anchor="w", pady=4)

    def _update_prox_state(*_):
        def _safe_get(var):
            try:
                return var.get()
            except Exception:
                return 0
        any_smooth = any(_safe_get(w["smooth_var"]) > 0 for w in comp_widgets.values())
        prox_entry.configure(state="normal" if any_smooth else "disabled")

    def _build_comp_row(name: str, count: int) -> None:
        if name in comp_widgets:
            comp_widgets[name]["count_var"].set(str(count))
            return
        saved       = pending_comp_cfg.get(name, {})
        smooth_var  = tk.IntVar(value=int(saved.get("smooth_iterations", 1)))
        smooth_var.trace_add("write", _update_prox_state)
        mult_var    = tk.StringVar(value=str(saved.get("mult_factor", 1.0)))
        snap_pd_var = tk.BooleanVar(value=bool(saved.get("save_power_density", True)))
        snap_tp_var = tk.BooleanVar(value=bool(saved.get("save_total_power", False)))
        count_var   = tk.StringVar(value=str(count))

        row = tk.Frame(comp_rows_frame)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=name,               width=24, anchor="w").pack(side="left")
        tk.Label(row, textvariable=count_var,  width=8,  anchor="w", fg="#666666").pack(side="left")
        tk.Spinbox(row, from_=0, to=20, width=5, textvariable=smooth_var).pack(side="left")
        tk.Label(row, text="", width=1).pack(side="left")
        tk.Entry(row, textvariable=mult_var, width=7).pack(side="left")
        tk.Label(row, text="", width=1).pack(side="left")
        tk.Checkbutton(row, text="Pwr density", variable=snap_pd_var).pack(side="left")
        tk.Checkbutton(row, text="Total pwr",   variable=snap_tp_var).pack(side="left")

        comp_widgets[name] = {
            "smooth_var":  smooth_var, "mult_var":    mult_var,
            "snap_pd_var": snap_pd_var, "snap_tp_var": snap_tp_var,
            "count_var":   count_var,
        }

    def on_load_geometry():
        for name, w in comp_widgets.items():
            pending_comp_cfg[name] = {
                "smooth_iterations":  w["smooth_var"].get(),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
            }
        raw  = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        if not dirs:
            messagebox.showwarning("No directories", "Please add at least one input directory.")
            return
        pat        = pattern_var.get() or "smoothed_results_*.vtp"
        raw_filter = filter_var.get().strip()
        terms      = [t.strip() for t in raw_filter.split(",") if t.strip()] if raw_filter else []

        counts: dict = {}
        log_fn("Load Geometry: scanning...")
        for d in dirs:
            p = Path(d)
            if not p.is_dir():
                log_fn(f"  [SKIP] not a directory: {d}"); continue
            folders = (
                [s for s in sorted(p.iterdir()) if s.is_dir()]
                if p.name.upper().startswith("OUTPUT_") else [p]
            )
            for folder in folders:
                files = sorted(folder.rglob(pat))
                if terms:
                    for t in terms:
                        matched = [f for f in files if t.lower() in f.stem.lower()]
                        if matched:
                            log_fn(f"  [{t}] {folder.name}: {len(matched)} file(s)")
                            for f in matched:
                                log_fn(f"    {f.name}")
                        counts[t] = counts.get(t, 0) + len(matched)
                else:
                    if files:
                        log_fn(f"  [(all)] {folder.name}: {len(files)} file(s)")
                        for f in files:
                            log_fn(f"    {f.name}")
                    counts["(all)"] = counts.get("(all)", 0) + len(files)

        if not any(v > 0 for v in counts.values()):
            load_geo_status.set("  No matching files found."); return

        for widget in comp_rows_frame.winfo_children():
            widget.destroy()
        comp_widgets.clear()

        total = 0
        for name, count in counts.items():
            if count > 0:
                _build_comp_row(name, count)
                total += count
        load_geo_status.set(f"  {len(comp_widgets)} component(s), {total} file(s)")
        _update_prox_state()
        log_fn(f"Load Geometry done: {len(comp_widgets)} component(s), {total} file(s).")

    load_geo_btn.configure(command=on_load_geometry)

    # ── Config save / load ────────────────────────────────────────────────────
    cfg_frame = tk.Frame(tab1)
    cfg_frame.pack(fill="x", padx=10, pady=(8, 8))
    tk.Label(cfg_frame, text="Config file:", anchor="w").pack(side="left")
    cfg_path_var = tk.StringVar(
        value=settings.get("last_config_path", str(SETTINGS_FILE)))
    tk.Entry(cfg_frame, textvariable=cfg_path_var, width=55).pack(side="left", padx=(6, 4))

    def _get_comp_dict() -> dict:
        return {
            name: {
                "smooth_iterations":  w["smooth_var"].get(),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
            }
            for name, w in comp_widgets.items()
        }

    # Config save/load callbacks reference _get_comp_dict (closure) and are
    # called only when the user presses a button — by that time the transform
    # tab vars exist in the caller's scope and are passed in via the returned
    # state dict.  We store them as a mutable list so the caller can inject them.
    _xfm_cfg_fn: list = [None]   # [0] set by Data_handling after both tabs built

    def on_save_cfg():
        path = cfg_path_var.get() or str(SETTINGS_FILE)
        cfg_path_var.set(path)
        cfg = _xfm_cfg_fn[0]() if _xfm_cfg_fn[0] else {}
        cfg["components"] = _get_comp_dict()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            remember_cfg_path(path)
            log_fn(f"Config saved: {path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_save_cfg_as():
        path = filedialog.asksaveasfilename(
            title="Save config as…",
            initialdir=str(Path(cfg_path_var.get()).parent),
            initialfile=Path(cfg_path_var.get()).name,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        cfg_path_var.set(path)
        remember_cfg_path(path)
        cfg = _xfm_cfg_fn[0]() if _xfm_cfg_fn[0] else {}
        cfg["components"] = _get_comp_dict()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            log_fn(f"Config saved: {path}")
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
        remember_cfg_path(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if _xfm_cfg_fn[0]:
                _xfm_cfg_fn[0](loaded)   # apply_cfg injected by caller
            log_fn(f"Config loaded: {path}")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    tk.Button(cfg_frame, text="Save config",     width=12, command=on_save_cfg   ).pack(side="left", padx=2)
    tk.Button(cfg_frame, text="Save config as…", width=14, command=on_save_cfg_as).pack(side="left", padx=2)
    tk.Button(cfg_frame, text="Load config",     width=12, command=on_load_cfg   ).pack(side="left", padx=2)

    # ── Run Processing button ─────────────────────────────────────────────────
    tab1_btn_frame = tk.Frame(tab1)
    tab1_btn_frame.pack(pady=(6, 10))
    tab1_run_btn = tk.Button(
        tab1_btn_frame, text="Run Processing", width=16, bg="#d4000e", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    tab1_run_btn.pack(side="left", padx=6)

    return {
        "text_box":          text_box,
        "pattern_var":       pattern_var,
        "filter_var":        filter_var,
        "output_folder_var": output_folder_var,
        "output_label_var":  output_label_var,
        "proximity_var":     proximity_var,
        "prox_entry":        prox_entry,
        "comp_widgets":      comp_widgets,
        "pending_comp_cfg":  pending_comp_cfg,
        "load_geo_status":   load_geo_status,
        "cfg_path_var":      cfg_path_var,
        "tab1_run_btn":      tab1_run_btn,
        "_xfm_cfg_fn":       _xfm_cfg_fn,   # caller injects get_full_cfg / apply_cfg
        "_get_comp_dict":    _get_comp_dict,
    }
