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

from modules.core.settings import SMOOTH_PROXIMITY_RADIUS, SPIKE_SIGMA, SPIKE_RATIO, SETTINGS_FILE, _safe_float, remember_cfg_path


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

    # ── Proximity radius (hidden StringVar kept for config file compatibility) ─
    proximity_var = tk.StringVar(
        value=str(settings.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS)))

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

    # Single grid for header row + all data rows so columns align automatically.
    comp_grid = tk.Frame(comp_lframe)
    comp_grid.pack(fill="x")

    _HDR = ["Component", "Files", "Iter", "Mode", "Sigma",
            "Prox (edge)", "Mult", "Spk", "Pwr density", "Total pwr", "Save post-smooth VTP"]
    for _c, _txt in enumerate(_HDR):
        tk.Label(comp_grid, text=_txt, anchor="w",
                 fg="#444444", font=("Segoe UI", 8, "bold"),
                 padx=3).grid(row=0, column=_c, sticky="w")

    _placeholder_lbl = tk.Label(comp_grid,
                                text="(click Load Geometry to populate)",
                                fg="#aaaaaa")
    _placeholder_lbl.grid(row=1, column=0, columnspan=len(_HDR), sticky="w", pady=4)

    _next_row = [1]   # mutable row counter for data rows

    comp_widgets:     dict = {}
    pending_comp_cfg: dict = dict(settings.get("components", {}))

    def _build_comp_row(name: str, count: int) -> None:
        if name in comp_widgets:
            comp_widgets[name]["count_var"].set(str(count))
            return
        saved            = pending_comp_cfg.get(name, {})
        smooth_var       = tk.IntVar(value=int(saved.get("smooth_iterations", 1)))
        smooth_mode_var  = tk.StringVar(value=str(saved.get("smooth_mode", "auto")))
        spike_sigma_var  = tk.StringVar(value=str(saved.get("spike_sigma", SPIKE_SIGMA)))
        prox_var         = tk.StringVar(value=str(saved.get(
            "proximity_radius", settings.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS))))
        mult_var         = tk.StringVar(value=str(saved.get("mult_factor", 1.0)))
        smooth_spikes_var = tk.BooleanVar(value=bool(saved.get("smooth_spikes", False)))
        snap_pd_var      = tk.BooleanVar(value=bool(saved.get("save_power_density", True)))
        snap_tp_var      = tk.BooleanVar(value=bool(saved.get("save_total_power", False)))
        count_var        = tk.StringVar(value=str(count))

        r = _next_row[0]
        _next_row[0] += 1

        tk.Label(comp_grid, text=name, anchor="w").grid(
            row=r, column=0, sticky="w", padx=(0, 8))
        tk.Label(comp_grid, textvariable=count_var, anchor="w",
                 fg="#666666").grid(row=r, column=1, sticky="w", padx=(0, 6))
        tk.Spinbox(comp_grid, from_=0, to=20, width=4,
                   textvariable=smooth_var).grid(row=r, column=2, sticky="w", padx=(0, 4))
        mode_menu = tk.OptionMenu(comp_grid, smooth_mode_var, "edge", "auto")
        mode_menu.config(width=5)
        mode_menu.grid(row=r, column=3, sticky="w", padx=(0, 2))
        sigma_entry = tk.Entry(comp_grid, textvariable=spike_sigma_var, width=5)
        sigma_entry.grid(row=r, column=4, sticky="w", padx=(0, 4))
        prox_entry_row = tk.Entry(comp_grid, textvariable=prox_var, width=6)
        prox_entry_row.grid(row=r, column=5, sticky="w", padx=(0, 4))
        tk.Entry(comp_grid, textvariable=mult_var, width=6).grid(
            row=r, column=6, sticky="w", padx=(0, 4))
        spk_chk = tk.Checkbutton(comp_grid, text="", variable=smooth_spikes_var)
        spk_chk.grid(row=r, column=7, sticky="w")
        tk.Checkbutton(comp_grid, text="Pwr density",
                       variable=snap_pd_var).grid(row=r, column=8, sticky="w")
        tk.Checkbutton(comp_grid, text="Total pwr",
                       variable=snap_tp_var).grid(row=r, column=9, sticky="w")
        save_vtp_var = tk.BooleanVar(value=bool(saved.get("save_smooth_vtp", False)))
        save_vtp_chk = tk.Checkbutton(comp_grid, text="Save post-smooth VTP",
                                      variable=save_vtp_var)
        save_vtp_chk.grid(row=r, column=10, sticky="w")

        def _update_mode_state(*_,
                               _menu=mode_menu, _sig=sigma_entry,
                               _prx=prox_entry_row, _spk=spk_chk,
                               _svp=save_vtp_chk,
                               _sv=smooth_var, _mv=smooth_mode_var):
            n_iter = 0
            try:
                n_iter = int(_sv.get())
            except Exception:
                pass
            if n_iter == 0:
                _menu.configure(state="disabled")
                _sig.configure(state="disabled")
                _prx.configure(state="disabled")
                _spk.configure(state="disabled")
                _svp.configure(state="disabled")
            else:
                mode = _mv.get()
                _menu.configure(state="normal")
                _sig.configure(state="normal" if mode == "auto" else "disabled")
                _prx.configure(state="normal" if mode == "edge" else "disabled")
                _spk.configure(state="normal" if mode == "auto" else "disabled")
                _svp.configure(state="normal")

        _update_mode_state()
        smooth_var.trace_add("write", _update_mode_state)
        smooth_mode_var.trace_add("write", _update_mode_state)

        comp_widgets[name] = {
            "smooth_var":        smooth_var,        "smooth_mode_var":  smooth_mode_var,
            "spike_sigma_var":   spike_sigma_var,   "prox_var":         prox_var,
            "smooth_spikes_var": smooth_spikes_var, "mult_var":         mult_var,
            "snap_pd_var":       snap_pd_var,        "snap_tp_var":      snap_tp_var,
            "save_vtp_var":      save_vtp_var,       "count_var":        count_var,
        }

    def on_load_geometry():
        for name, w in comp_widgets.items():
            pending_comp_cfg[name] = {
                "smooth_iterations":  w["smooth_var"].get(),
                "smooth_mode":        w["smooth_mode_var"].get(),
                "spike_sigma":        _safe_float(w["spike_sigma_var"].get(), SPIKE_SIGMA),
                "proximity_radius":   _safe_float(w["prox_var"].get(), SMOOTH_PROXIMITY_RADIUS),
                "smooth_spikes":      w["smooth_spikes_var"].get(),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
                "save_smooth_vtp":    w["save_vtp_var"].get(),
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

        for widget in comp_grid.winfo_children():
            info = widget.grid_info()
            if info and int(info.get("row", 0)) >= 1:
                widget.destroy()
        comp_widgets.clear()
        _next_row[0] = 1

        total = 0
        for name, count in counts.items():
            if count > 0:
                _build_comp_row(name, count)
                total += count
        load_geo_status.set(f"  {len(comp_widgets)} component(s), {total} file(s)")
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
                "smooth_mode":        w["smooth_mode_var"].get(),
                "spike_sigma":        _safe_float(w["spike_sigma_var"].get(), SPIKE_SIGMA),
                "proximity_radius":   _safe_float(w["prox_var"].get(), SMOOTH_PROXIMITY_RADIUS),
                "smooth_spikes":      w["smooth_spikes_var"].get(),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
                "save_smooth_vtp":    w["save_vtp_var"].get(),
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
        tab1_btn_frame, text="Run Processing", width=16, bg="#0060c0", fg="white",
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
        "comp_widgets":      comp_widgets,
        "pending_comp_cfg":  pending_comp_cfg,
        "load_geo_status":   load_geo_status,
        "cfg_path_var":      cfg_path_var,
        "tab1_run_btn":      tab1_run_btn,
        "_xfm_cfg_fn":       _xfm_cfg_fn,
        "_get_comp_dict":    _get_comp_dict,
    }
