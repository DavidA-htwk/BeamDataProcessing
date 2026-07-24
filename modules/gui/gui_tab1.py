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

from modules.core.settings import SMOOTH_PROXIMITY_RADIUS, SPIKE_SIGMA, SPIKE_RATIO, MIN_POWER_W, SETTINGS_FILE, _safe_float, remember_cfg_path


class _Tooltip:
    """Lightweight hover tooltip for any Tkinter widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text   = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self._tip:
            return
        x = self._widget.winfo_rootx() + 16
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        tip = tk.Toplevel(self._widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.attributes("-topmost", True)
        lbl = tk.Label(
            tip, text=self._text, justify="left",
            background="#fffde7", foreground="#222222",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 8), wraplength=340, padx=6, pady=4,
        )
        lbl.pack()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


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

    # ── Scrollable canvas: header (row 0) + bulk-apply (row 1) + data rows ────
    _comp_outer = tk.Frame(comp_lframe)
    _comp_outer.pack(fill="x")
    _comp_canvas = tk.Canvas(_comp_outer, height=220, highlightthickness=0)
    _comp_ybar   = tk.Scrollbar(_comp_outer, orient="vertical",
                                command=_comp_canvas.yview)
    _comp_canvas.configure(yscrollcommand=_comp_ybar.set)
    _comp_ybar.pack(side="right", fill="y")
    _comp_canvas.pack(side="left", fill="both", expand=True)

    comp_grid = tk.Frame(_comp_canvas)
    _comp_win  = _comp_canvas.create_window((0, 0), window=comp_grid, anchor="nw")

    comp_grid.bind("<Configure>",
                   lambda e: _comp_canvas.configure(
                       scrollregion=_comp_canvas.bbox("all")))
    _comp_canvas.bind("<Configure>",
                      lambda e: _comp_canvas.itemconfig(_comp_win, width=e.width))

    def _canvas_scroll(event):
        _comp_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    _comp_canvas.bind("<Enter>",
                      lambda _: _comp_canvas.bind_all("<MouseWheel>", _canvas_scroll))
    _comp_canvas.bind("<Leave>",
                      lambda _: _comp_canvas.unbind_all("<MouseWheel>"))

    _HDR = ["Component", "Files", "1. Iter", "1. Mode", "2. Iter", "2. Mode", "Sigma",
            "Prox (edge)", "Snap factor", "Min pwr (W)", "Pwr density", "Total pwr", "Save post-smooth VTP"]
    _HDR_TIPS = [
        "Component name (matched against VTP filename).",
        "Number of VTP files found for this component.",
        "Stage 1 smoothing iterations (0 = no smoothing, only min-power sliver filter).\nApplied first.",
        "Stage 1 smoothing mode:\n  auto — local z-score spike detection + edge classification.\n  edge — smooth all cells that touch a feature/boundary edge.",
        "Stage 2 smoothing iterations (0 = disabled).\nApplied after stage 1, on stage 1's output — lets you combine e.g. auto then edge smoothing in one run.",
        "Stage 2 smoothing mode:\n  auto — local z-score spike detection + edge classification.\n  edge — smooth all cells that touch a feature/boundary edge.",
        "Sigma threshold (auto mode only).\nA cell is flagged if its value exceeds:\n  local_mean + sigma × local_std\nHigher = less aggressive (fewer cells flagged).\nDefault: 2.0\nUsed by whichever stage(s) are set to auto.",
        "Proximity radius in mesh units (edge mode only).\nCells within this distance of a feature-edge point are also flagged.\nSet to 0 to disable proximity expansion.\nUsed by whichever stage(s) are set to edge.",
        "Snapshot factor — multiplies power density values in rendered PNG snapshots only.\nNever applied to the VTP data itself.",
        "Min deposited power filter (W).\nCells with Deposited_Power_W below this threshold are treated as mesh artifacts (sliver cells with near-zero area).\nTheir power density is replaced with the area-weighted neighbour mean.\nRuns before all other smoothing. Set to 0 to disable.\nRecommended: 1.0",
        "Save a power-density snapshot PNG.",
        "Save a total-deposited-power snapshot PNG.",
        "Save the post-smoothed mesh as a VTP file (for use as input to Post Processing or Transform).",
    ]

    # ── Row 0: column headers ─────────────────────────────────────────────────
    for _c, (_txt, _tip) in enumerate(zip(_HDR, _HDR_TIPS)):
        _lbl = tk.Label(comp_grid, text=_txt, anchor="w",
                        fg="#444444", font=("Segoe UI", 8, "bold"),
                        padx=3, cursor="question_arrow")
        _lbl.grid(row=0, column=_c, sticky="w")
        _Tooltip(_lbl, _tip)

    # ── Row 1: "Apply to all ↓" bulk-edit row ─────────────────────────────────
    # Set these vars then click Apply ↓ to push all values to every component row.
    _bulk_iter_var   = tk.IntVar(value=1)
    _bulk_mode_var   = tk.StringVar(value="auto")
    _bulk_iter2_var  = tk.IntVar(value=0)
    _bulk_mode2_var  = tk.StringVar(value="edge")
    _bulk_sigma_var  = tk.StringVar(value=str(SPIKE_SIGMA))
    _bulk_prox_var   = tk.StringVar(value=str(SMOOTH_PROXIMITY_RADIUS))
    _bulk_mult_var   = tk.StringVar(value="1.0")
    _bulk_minpwr_var = tk.StringVar(value="0.0")
    _bulk_pd_var     = tk.BooleanVar(value=True)
    _bulk_tp_var     = tk.BooleanVar(value=False)
    _bulk_svtp_var   = tk.BooleanVar(value=False)

    def _apply_bulk_to_all(*_):
        for _w in comp_widgets.values():
            _w["smooth_var"].set(_bulk_iter_var.get())
            _w["smooth_mode_var"].set(_bulk_mode_var.get())
            _w["smooth_var2"].set(_bulk_iter2_var.get())
            _w["smooth_mode_var2"].set(_bulk_mode2_var.get())
            _w["spike_sigma_var"].set(_bulk_sigma_var.get())
            _w["prox_var"].set(_bulk_prox_var.get())
            _w["mult_var"].set(_bulk_mult_var.get())
            _w["min_pwr_var"].set(_bulk_minpwr_var.get())
            _w["snap_pd_var"].set(_bulk_pd_var.get())
            _w["snap_tp_var"].set(_bulk_tp_var.get())
            _w["save_vtp_var"].set(_bulk_svtp_var.get())

    _BR = 1
    tk.Label(comp_grid, text="↳ Apply to all", anchor="w", fg="#005f73",
             font=("Segoe UI", 8, "italic")).grid(
        row=_BR, column=0, sticky="w", padx=(0, 4))
    tk.Label(comp_grid, text="", width=4).grid(row=_BR, column=1)
    tk.Spinbox(comp_grid, from_=0, to=20, width=4,
               textvariable=_bulk_iter_var).grid(row=_BR, column=2, sticky="w", padx=(0, 4))
    _bk_mode = tk.OptionMenu(comp_grid, _bulk_mode_var, "edge", "auto")
    _bk_mode.config(width=5)
    _bk_mode.grid(row=_BR, column=3, sticky="w", padx=(0, 2))
    tk.Spinbox(comp_grid, from_=0, to=20, width=4,
               textvariable=_bulk_iter2_var).grid(row=_BR, column=4, sticky="w", padx=(0, 4))
    _bk_mode2 = tk.OptionMenu(comp_grid, _bulk_mode2_var, "edge", "auto")
    _bk_mode2.config(width=5)
    _bk_mode2.grid(row=_BR, column=5, sticky="w", padx=(0, 2))
    tk.Entry(comp_grid, textvariable=_bulk_sigma_var,  width=5).grid(
        row=_BR, column=6, sticky="w", padx=(0, 4))
    tk.Entry(comp_grid, textvariable=_bulk_prox_var,   width=6).grid(
        row=_BR, column=7, sticky="w", padx=(0, 4))
    tk.Entry(comp_grid, textvariable=_bulk_mult_var,   width=6).grid(
        row=_BR, column=8, sticky="w", padx=(0, 4))
    tk.Entry(comp_grid, textvariable=_bulk_minpwr_var, width=7).grid(
        row=_BR, column=9, sticky="w", padx=(0, 4))
    tk.Checkbutton(comp_grid, variable=_bulk_pd_var).grid(  row=_BR, column=10, sticky="w")
    tk.Checkbutton(comp_grid, variable=_bulk_tp_var).grid(  row=_BR, column=11, sticky="w")
    tk.Checkbutton(comp_grid, variable=_bulk_svtp_var).grid(row=_BR, column=12, sticky="w")
    tk.Button(comp_grid, text="Apply ↓", fg="#005f73", font=("Segoe UI", 8),
              command=_apply_bulk_to_all, padx=4).grid(
        row=_BR, column=13, sticky="w", padx=(8, 0))

    # ── Placeholder (row 2, removed once Load Geometry populates) ────────────
    _placeholder_lbl = tk.Label(comp_grid,
                                text="(click Load Geometry to populate)",
                                fg="#aaaaaa")
    _placeholder_lbl.grid(row=2, column=0, columnspan=len(_HDR), sticky="w", pady=4)

    _next_row = [2]   # rows 0 (header) and 1 (bulk-apply) are permanently reserved

    comp_widgets:     dict = {}
    pending_comp_cfg: dict = dict(settings.get("components", {}))

    def _build_comp_row(name: str, count: int) -> None:
        if name in comp_widgets:
            comp_widgets[name]["count_var"].set(str(count))
            return
        saved            = pending_comp_cfg.get(name, {})
        smooth_var       = tk.IntVar(value=int(saved.get("smooth_iterations", 1)))
        smooth_mode_var  = tk.StringVar(value=str(saved.get("smooth_mode", "auto")))
        smooth_var2      = tk.IntVar(value=int(saved.get("smooth_iterations_2", 0)))
        smooth_mode_var2 = tk.StringVar(value=str(saved.get("smooth_mode_2", "edge")))
        spike_sigma_var  = tk.StringVar(value=str(saved.get("spike_sigma", SPIKE_SIGMA)))
        prox_var         = tk.StringVar(value=str(saved.get(
            "proximity_radius", settings.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS))))
        mult_var         = tk.StringVar(value=str(saved.get("mult_factor", 1.0)))
        min_pwr_var      = tk.StringVar(value=str(saved.get("min_power_W", 0.0)))
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
        tk.Spinbox(comp_grid, from_=0, to=20, width=4,
                   textvariable=smooth_var2).grid(row=r, column=4, sticky="w", padx=(0, 4))
        mode_menu2 = tk.OptionMenu(comp_grid, smooth_mode_var2, "edge", "auto")
        mode_menu2.config(width=5)
        mode_menu2.grid(row=r, column=5, sticky="w", padx=(0, 2))
        sigma_entry = tk.Entry(comp_grid, textvariable=spike_sigma_var, width=5)
        sigma_entry.grid(row=r, column=6, sticky="w", padx=(0, 4))
        prox_entry_row = tk.Entry(comp_grid, textvariable=prox_var, width=6)
        prox_entry_row.grid(row=r, column=7, sticky="w", padx=(0, 4))
        tk.Entry(comp_grid, textvariable=mult_var, width=6).grid(
            row=r, column=8, sticky="w", padx=(0, 4))
        min_pwr_entry = tk.Entry(comp_grid, textvariable=min_pwr_var, width=7)
        min_pwr_entry.grid(row=r, column=9, sticky="w", padx=(0, 4))
        tk.Checkbutton(comp_grid, text="Pwr density",
                       variable=snap_pd_var).grid(row=r, column=10, sticky="w")
        tk.Checkbutton(comp_grid, text="Total pwr",
                       variable=snap_tp_var).grid(row=r, column=11, sticky="w")
        save_vtp_var = tk.BooleanVar(value=bool(saved.get("save_smooth_vtp", False)))
        save_vtp_chk = tk.Checkbutton(comp_grid, text="Save post-smooth VTP",
                                      variable=save_vtp_var)
        save_vtp_chk.grid(row=r, column=12, sticky="w")

        def _update_mode_state(*_,
                               _menu=mode_menu, _menu2=mode_menu2,
                               _sig=sigma_entry,
                               _prx=prox_entry_row,
                               _svp=save_vtp_chk,
                               _mpw=min_pwr_entry,
                               _sv=smooth_var, _mv=smooth_mode_var,
                               _sv2=smooth_var2, _mv2=smooth_mode_var2):
            def _as_int(var) -> int:
                try:
                    return int(var.get())
                except Exception:
                    return 0

            n_iter  = _as_int(_sv)
            n_iter2 = _as_int(_sv2)
            _menu.configure(state="normal" if n_iter > 0 else "disabled")
            _menu2.configure(state="normal" if n_iter2 > 0 else "disabled")

            uses_auto = (n_iter > 0 and _mv.get() == "auto") or (n_iter2 > 0 and _mv2.get() == "auto")
            uses_edge = (n_iter > 0 and _mv.get() == "edge") or (n_iter2 > 0 and _mv2.get() == "edge")
            _sig.configure(state="normal" if uses_auto else "disabled")
            _prx.configure(state="normal" if uses_edge else "disabled")
            _svp.configure(state="normal" if (n_iter > 0 or n_iter2 > 0) else "disabled")
            _mpw.configure(state="normal")

        _update_mode_state()
        smooth_var.trace_add("write", _update_mode_state)
        smooth_mode_var.trace_add("write", _update_mode_state)
        smooth_var2.trace_add("write", _update_mode_state)
        smooth_mode_var2.trace_add("write", _update_mode_state)

        comp_widgets[name] = {
            "smooth_var":        smooth_var,        "smooth_mode_var":  smooth_mode_var,
            "smooth_var2":       smooth_var2,        "smooth_mode_var2": smooth_mode_var2,
            "spike_sigma_var":   spike_sigma_var,   "prox_var":         prox_var,
            "mult_var":          mult_var,
            "snap_pd_var":       snap_pd_var,        "snap_tp_var":      snap_tp_var,
            "save_vtp_var":      save_vtp_var,       "count_var":        count_var,
            "min_pwr_var":       min_pwr_var,
        }

    def on_load_geometry():
        for name, w in comp_widgets.items():
            pending_comp_cfg[name] = {
                "smooth_iterations":  w["smooth_var"].get(),
                "smooth_mode":        w["smooth_mode_var"].get(),
                "smooth_iterations_2": w["smooth_var2"].get(),
                "smooth_mode_2":      w["smooth_mode_var2"].get(),
                "spike_sigma":        _safe_float(w["spike_sigma_var"].get(), SPIKE_SIGMA),
                "proximity_radius":   _safe_float(w["prox_var"].get(), SMOOTH_PROXIMITY_RADIUS),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
                "save_smooth_vtp":    w["save_vtp_var"].get(),
                "min_power_W":        _safe_float(w["min_pwr_var"].get(), 0.0),
            }
        raw  = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        if not dirs:
            messagebox.showwarning("No directories", "Please add at least one input directory.")
            return
        pat        = pattern_var.get() or "smoothed_results_*.vtp"
        raw_filter = filter_var.get().strip()
        terms      = [t.strip() for t in raw_filter.split(",") if t.strip()] if raw_filter else []

        def _stem_to_comp(stem: str) -> str:
            """Strip the standard VTP filename prefix to get the bare component name."""
            for pfx in ("smoothed_results_", "results_"):
                if stem.lower().startswith(pfx):
                    return stem[len(pfx):]
            return stem

        # Count unique files per actual component name (derived from file stem).
        # Using a seen-set prevents double-counting across folders or multiple
        # filter terms that can match the same file.
        comp_counts: dict[str, int] = {}
        seen_files:  set = set()
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
                files   = sorted(folder.rglob(pat))
                matched = (
                    [f for f in files
                     if any(t.lower() in f.stem.lower() for t in terms)]
                    if terms else list(files)
                )
                n_new = 0
                for f in matched:
                    if f in seen_files:
                        continue
                    seen_files.add(f)
                    comp_label = _stem_to_comp(f.stem)
                    comp_counts[comp_label] = comp_counts.get(comp_label, 0) + 1
                    n_new += 1
                if n_new:
                    log_fn(f"  {folder.name}: {n_new} file(s) matched")

        if not comp_counts:
            load_geo_status.set("  No matching files found."); return

        # Destroy only data rows (row >= 2); preserve header (0) and bulk-apply (1).
        for widget in comp_grid.winfo_children():
            info = widget.grid_info()
            if info and int(info.get("row", 0)) >= 2:
                widget.destroy()
        comp_widgets.clear()
        _next_row[0] = 2

        total = 0
        for name in sorted(comp_counts.keys()):
            _build_comp_row(name, comp_counts[name])
            total += comp_counts[name]
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
                "smooth_iterations_2": w["smooth_var2"].get(),
                "smooth_mode_2":      w["smooth_mode_var2"].get(),
                "spike_sigma":        _safe_float(w["spike_sigma_var"].get(), SPIKE_SIGMA),
                "proximity_radius":   _safe_float(w["prox_var"].get(), SMOOTH_PROXIMITY_RADIUS),
                "mult_factor":        _safe_float(w["mult_var"].get(), 1.0),
                "save_power_density": w["snap_pd_var"].get(),
                "save_total_power":   w["snap_tp_var"].get(),
                "save_smooth_vtp":    w["save_vtp_var"].get(),
                "min_power_W":        _safe_float(w["min_pwr_var"].get(), 0.0),
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
