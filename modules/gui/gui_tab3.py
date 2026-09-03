"""
modules/gui/gui_tab3.py
-----------------------
Builder for the Post Processing tab (Tab 3) of the main GUI.

Call build_post_processing_tab(tab3, settings) to populate the frame and
receive a state dict with all Tkinter variables needed by the run callbacks.

The tab lets the user:
  - Choose a source (original input folders or post-smoothed VTPs)
  - Filter by pattern / name
  - Browse and select scenario cases via a scrollable checkbox grid
  - Configure optional snapshots (power density / total power)
  - Set a multiplication factor (shown only when source = original)
  - Run the cell-wise max merge via run_post_processing()
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from modules.core.path_utils import extract_case_scenario


def build_post_processing_tab(tab3: tk.Frame, settings: dict) -> dict:
    """Populate *tab3* with all Post Processing widgets.

    Returns a dict of tk.Vars and helpers needed by run callbacks.
    """
    pp_s = settings.get("post_processing", {})

    # ── Injection hooks (filled by Data_handling.py after build) ─────────────
    _get_tab1_dirs:    list = [None]   # [0] = callable() → list[str]
    _get_output_folder: list = [None]  # [0] = callable() → str
    _get_mult_factor_p: list = [None]  # [0] = callable() → str  (processing mult)

    # ── Source + pattern/filter ───────────────────────────────────────────────
    src_lframe = tk.LabelFrame(tab3, text="Input source", padx=8, pady=6)
    src_lframe.pack(fill="x", padx=10, pady=(8, 4))

    pp_source_var = tk.StringVar(value=pp_s.get("pp_source", "original_smooth"))

    src_radio_frame = tk.Frame(src_lframe)
    src_radio_frame.pack(fill="x")
    tk.Radiobutton(src_radio_frame, text="Original input folders (RAW: results_*.vtp)",
                   variable=pp_source_var, value="original_raw").pack(side="left", padx=(0, 12))
    tk.Radiobutton(src_radio_frame, text="Original input folders (smoothed: smoothed_results_*.vtp)",
                   variable=pp_source_var, value="original_smooth").pack(side="left", padx=(0, 12))
    tk.Radiobutton(src_radio_frame,
                   text="Post-smooth VTPs from Processing (output/post_smoothed/)",
                   variable=pp_source_var, value="post_smooth").pack(side="left")

    # Pattern + name filter row
    opt_frame = tk.Frame(src_lframe)
    opt_frame.pack(fill="x", pady=(6, 0))
    tk.Label(opt_frame, text="Glob pattern:", anchor="w").grid(
        row=0, column=0, sticky="w")
    pp_pattern_var = tk.StringVar(
        value=pp_s.get("pattern", "smoothed_results_*.vtp"))
    tk.Entry(opt_frame, textvariable=pp_pattern_var, width=36).grid(
        row=0, column=1, sticky="w", padx=(6, 20))
    tk.Label(opt_frame, text="Name filter (comma-separated):", anchor="w").grid(
        row=0, column=2, sticky="w")
    pp_filter_var = tk.StringVar(value=pp_s.get("name_filter", ""))
    tk.Entry(opt_frame, textvariable=pp_filter_var, width=36).grid(
        row=0, column=3, sticky="w", padx=6)

    # ── Case browser ──────────────────────────────────────────────────────────
    case_lframe = tk.LabelFrame(tab3, text="Cases", padx=8, pady=4)
    case_lframe.pack(fill="both", expand=True, padx=10, pady=(4, 4))

    load_btn_frame = tk.Frame(case_lframe)
    load_btn_frame.pack(fill="x", pady=(0, 2))
    load_case_btn = tk.Button(
        load_btn_frame, text="Load Cases", width=14,
        bg="#005f73", fg="white", font=("Segoe UI", 10, "bold"),
    )
    load_case_btn.pack(side="left")
    load_case_status = tk.StringVar(
        value="  (uses Tab 1 directories — click to populate)")
    tk.Label(load_btn_frame, textvariable=load_case_status,
             fg="#555555", anchor="w").pack(side="left", padx=6)

    selall_frame = tk.Frame(case_lframe)
    selall_frame.pack(fill="x", pady=(0, 2))

    # ── Three-panel layout: Scenarios | Components | Snapshot preview ──────────
    case_content = tk.PanedWindow(case_lframe, orient="horizontal",
                                   sashwidth=5, sashrelief="raised", bg="#d0d0d0")
    case_content.pack(fill="both", expand=True)

    # ── Left pane: scrollable scenario list (one row per scenario folder) ─────
    scen_outer = tk.Frame(case_content)
    case_content.add(scen_outer, stretch="always", minsize=200, width=300)
    tk.Label(scen_outer, text="Scenarios  (click to assign colour group)",
             font=("Segoe UI", 8, "bold"), fg="#444444", anchor="w").pack(
        fill="x", padx=4, pady=(2, 0))
    scen_canvas = tk.Canvas(scen_outer, bg="white", highlightthickness=0)
    scen_vscroll = tk.Scrollbar(scen_outer, orient="vertical",
                                command=scen_canvas.yview)
    scen_canvas.configure(yscrollcommand=scen_vscroll.set)
    scen_vscroll.pack(side="right", fill="y")
    scen_canvas.pack(side="left", fill="both", expand=True)
    scen_inner = tk.Frame(scen_canvas, bg="white")
    _scen_win = scen_canvas.create_window((0, 0), window=scen_inner, anchor="nw")
    scen_inner.bind("<Configure>",
                    lambda e: scen_canvas.configure(
                        scrollregion=scen_canvas.bbox("all")))
    scen_canvas.bind("<Configure>",
                     lambda e: scen_canvas.itemconfig(_scen_win, width=e.width))

    def _on_scen_scroll(event):
        scen_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    scen_canvas.bind("<Enter>",
                     lambda _: scen_canvas.bind_all("<MouseWheel>", _on_scen_scroll))
    scen_canvas.bind("<Leave>",
                     lambda _: scen_canvas.unbind_all("<MouseWheel>"))

    # ── Middle pane: component list (click to filter snapshot preview) ────────
    comp_outer = tk.Frame(case_content)
    case_content.add(comp_outer, stretch="never", minsize=120, width=160)
    comp_hdr_lbl = tk.Label(comp_outer, text="Components (0)",
                            font=("Segoe UI", 8, "bold"), fg="#444444", anchor="w")
    comp_hdr_lbl.pack(fill="x", padx=4, pady=(2, 0))
    comp_list_frame = tk.Frame(comp_outer)
    comp_list_frame.pack(fill="both", expand=True, padx=(4, 0), pady=(2, 4))
    comp_listbox = tk.Listbox(comp_list_frame, selectmode="single",
                               font=("Segoe UI", 8), activestyle="none",
                               selectbackground="#005f73", selectforeground="white",
                               bg="white", bd=0, highlightthickness=1,
                               highlightcolor="#aaaaaa")
    comp_vscroll = tk.Scrollbar(comp_list_frame, orient="vertical",
                                command=comp_listbox.yview)
    comp_listbox.configure(yscrollcommand=comp_vscroll.set)
    comp_vscroll.pack(side="right", fill="y")
    comp_listbox.pack(side="left", fill="both", expand=True)
    _all_comp_names: list[str] = []   # populated by on_load_cases

    def _get_selected_comp() -> str:
        sel = comp_listbox.curselection()
        return _all_comp_names[sel[0]] if sel and _all_comp_names else ""

    # ── Right pane: snapshot preview ──────────────────────────────────────────
    _PREVIEW_INIT_W = 280
    preview_outer = tk.Frame(case_content, bg="#f5f5f5", relief="sunken", bd=1)
    case_content.add(preview_outer, stretch="never", minsize=120,
                     width=_PREVIEW_INIT_W)
    tk.Label(preview_outer, text="Snapshot preview", bg="#f5f5f5",
             font=("Segoe UI", 8, "italic"), fg="#999999").pack(pady=(4, 0))
    preview_img_lbl  = tk.Label(preview_outer, bg="#f5f5f5",
                                text="(select a scenario)", fg="#bbbbbb",
                                font=("Segoe UI", 8), cursor="hand2")
    preview_img_lbl.pack(expand=True)
    preview_name_lbl = tk.Label(preview_outer, bg="#f5f5f5",
                                text="", fg="#666666",
                                font=("Segoe UI", 7), wraplength=0)
    preview_name_lbl.pack(pady=(2, 4))

    zoom_var = tk.DoubleVar(value=1.0)
    _zoom_frame = tk.Frame(preview_outer, bg="#f5f5f5")
    _zoom_frame.pack(fill="x", padx=6, pady=(0, 2))
    tk.Label(_zoom_frame, text="Zoom:", bg="#f5f5f5",
             font=("Segoe UI", 7), fg="#777777").pack(side="left")
    _zoom_val_lbl = tk.Label(_zoom_frame, text="1.0×", bg="#f5f5f5",
                             font=("Segoe UI", 7, "bold"), fg="#444444", width=4)
    _zoom_val_lbl.pack(side="right")
    tk.Scale(_zoom_frame, from_=1.0, to=8.0, resolution=0.1,
             orient="horizontal", variable=zoom_var,
             bg="#f5f5f5", highlightthickness=0, showvalue=False,
             length=1).pack(side="left", fill="x", expand=True, padx=(4, 2))

    _preview_ref  = [None, None]   # [0]=PhotoImage (GC guard), [1]=last snap path
    _pan_offset   = [0.0, 0.0]
    _drag_start   = [None, None]
    _drag_pan_st  = [0.0, 0.0]
    _img_orig_sz  = [None]

    def _find_snapshot_pp(sf_path: str) -> str | None:
        """Return best matching snapshot PNG for scenario folder sf_path."""
        source = pp_source_var.get()
        try:
            output_name, case, scenario = extract_case_scenario(sf_path)
        except Exception:
            return None

        if source == "post_smooth":
            derived   = Path(sf_path).parents[3]
            snap_base = derived / "snapshots" / output_name / case / scenario
        else:
            get_out = _get_output_folder[0]
            if get_out is None:
                return None
            out_raw = get_out().strip()
            if not out_raw:
                return None
            snap_base = Path(out_raw) / "snapshots" / output_name / case / scenario

        if not snap_base.exists():
            return None
        pngs = sorted(snap_base.glob("*.png"))
        if not pngs:
            return None

        comp_filter = _get_selected_comp()

        def _apply_comp_filter(lst):
            if comp_filter:
                f = [p for p in lst if comp_filter.lower() in p.stem.lower()]
                return f if f else lst
            return lst

        if source in ("original", "original_smooth"):
            cands = _apply_comp_filter([p for p in pngs if "__RAW_smoothed" in p.stem])
            if cands:
                return str(cands[0])
        elif source == "original_raw":
            cands = _apply_comp_filter(
                [p for p in pngs if "__RAW" in p.stem and "__RAW_smoothed" not in p.stem])
            if cands:
                return str(cands[0])
        else:
            cands = _apply_comp_filter([p for p in pngs if "__post_smooth" in p.stem])
            if cands:
                return str(cands[0])
        return None

    def _render_preview_pp(snap: str) -> None:
        """Load and display *snap* with pan/zoom applied."""
        w    = max(80, preview_outer.winfo_width() - 8)
        h    = max(60, preview_outer.winfo_height() - 80)
        zoom = zoom_var.get()
        try:
            try:
                from PIL import Image as _PI, ImageTk as _PIT
                img = _PI.open(snap)
                iw, ih = img.size
                _img_orig_sz[0] = (iw, ih)
                if zoom > 1.0:
                    cx_def = iw * 0.375   # mesh viewport center (left 75%)
                    cy_def = ih * 0.5
                    cw, ch = iw / zoom, ih / zoom
                    cx = cx_def + _pan_offset[0]
                    cy = cy_def + _pan_offset[1]
                    # Clamp crop box inside image
                    if cx - cw / 2 < 0:  cx = cw / 2
                    if cx + cw / 2 > iw: cx = iw - cw / 2
                    if cy - ch / 2 < 0:  cy = ch / 2
                    if cy + ch / 2 > ih: cy = ih - ch / 2
                    _pan_offset[0] = cx - cx_def
                    _pan_offset[1] = cy - cy_def
                    img = img.crop((
                        max(0, int(cx - cw / 2)),
                        max(0, int(cy - ch / 2)),
                        min(iw, int(cx + cw / 2)),
                        min(ih, int(cy + ch / 2)),
                    ))
                cw2, ch2 = img.size
                ratio = min(w / max(cw2, 1), h / max(ch2, 1))
                img   = img.resize((max(1, int(cw2 * ratio)),
                                    max(1, int(ch2 * ratio))),
                                   _PI.LANCZOS)
                photo = _PIT.PhotoImage(img)
            except ImportError:
                photo = tk.PhotoImage(file=snap)
                factor = max(1, max(photo.width(), photo.height()) // max(w, h))
                if factor > 1:
                    photo = photo.subsample(factor, factor)
            _preview_ref[0] = photo
            preview_img_lbl.configure(image=photo, text="")
            preview_name_lbl.configure(text=Path(snap).name,
                                       wraplength=max(40, w - 4))
        except Exception as exc:
            preview_img_lbl.configure(image="", text=f"[error: {exc}]", fg="#cc0000")
            _preview_ref[0] = None

    def _on_zoom_pp(*_):
        _pan_offset[0] = 0.0
        _pan_offset[1] = 0.0
        _zoom_val_lbl.configure(text=f"{zoom_var.get():.1f}×")
        if _preview_ref[1]:
            _render_preview_pp(_preview_ref[1])

    zoom_var.trace_add("write", _on_zoom_pp)

    def _start_drag_pp(event):
        _drag_start[0]  = event.x
        _drag_start[1]  = event.y
        _drag_pan_st[0] = _pan_offset[0]
        _drag_pan_st[1] = _pan_offset[1]

    def _do_drag_pp(event):
        if _drag_start[0] is None or _img_orig_sz[0] is None:
            return
        if zoom_var.get() <= 1.0:
            return
        iw, ih  = _img_orig_sz[0]
        dw = max(1, preview_outer.winfo_width() - 8)
        dh = max(1, preview_outer.winfo_height() - 80)
        z  = zoom_var.get()
        _pan_offset[0] = _drag_pan_st[0] - (event.x - _drag_start[0]) * (iw / z) / dw
        _pan_offset[1] = _drag_pan_st[1] - (event.y - _drag_start[1]) * (ih / z) / dh
        if _preview_ref[1]:
            _render_preview_pp(_preview_ref[1])

    preview_img_lbl.bind("<ButtonPress-1>", _start_drag_pp)
    preview_img_lbl.bind("<B1-Motion>",     _do_drag_pp)

    def _update_preview_pp(sf_path: str) -> None:
        snap = _find_snapshot_pp(sf_path)
        _preview_ref[1] = snap
        if snap is None:
            preview_img_lbl.configure(image="", text="(no snapshot found)", fg="#bbbbbb")
            preview_name_lbl.configure(text="")
            _preview_ref[0] = None
            return
        _render_preview_pp(snap)

    def _on_preview_resize_pp(event=None):
        if _preview_ref[1]:
            _render_preview_pp(_preview_ref[1])

    preview_outer.bind("<Configure>", _on_preview_resize_pp)

    # Component listbox → refresh preview when selection changes
    def _on_comp_select(event=None):
        if _last_click_path[0]:
            _update_preview_pp(_last_click_path[0])

    comp_listbox.bind("<<ListboxSelect>>", _on_comp_select)

    # ── Merge group definitions ───────────────────────────────────────────────
    MERGE_GROUPS = [
        ("blue",   "#2563eb"),
        ("red",    "#dc2626"),
        ("green",  "#16a34a"),
        ("orange", "#ea580c"),
        ("purple", "#9333ea"),
    ]
    _GROUP_COLORS       = {name: color for name, color in MERGE_GROUPS}
    _saved_labels: dict[str, str] = pp_s.get("group_labels", {})
    _group_labels: dict[str, str] = {k: _saved_labels.get(k, k) for k, _ in MERGE_GROUPS}
    active_group_var    = tk.StringVar(value=MERGE_GROUPS[0][0])
    case_group:         dict[str, str]      = {}   # {scenario_path: group_name}
    _scenario_indicators: dict[str, tk.Label] = {}  # {scenario_path: indicator_label}
    _saved_case_groups: dict[str, str]      = dict(pp_s.get("case_groups", {}))
    _baked_factor:       list[float]        = [1.0]
    _cases_loaded_for_source: list[str]     = [""]
    _last_click_path:    list               = [None]
    _ordered_scenarios:  list[str]          = []

    def _refresh_scenario_ind(sp: str) -> None:
        ind = _scenario_indicators.get(sp)
        if ind is None:
            return
        grp = case_group.get(sp)
        if grp:
            lbl_ch = _group_labels.get(grp, grp)
            ind.configure(bg=_GROUP_COLORS.get(grp, "#e5e7eb"),
                          text=lbl_ch[0].upper(), fg="white")
        else:
            ind.configure(bg="#e5e7eb", text="", fg="#e5e7eb")

    def _assign_scenario(sp: str) -> None:
        active = active_group_var.get()
        if active == "_clear":
            case_group.pop(sp, None)
        elif case_group.get(sp) == active:
            case_group.pop(sp, None)
        else:
            case_group[sp] = active
        _refresh_scenario_ind(sp)
        _last_click_path[0] = sp
        _update_preview_pp(sp)

    def _assign_scenario_range(sp: str) -> None:
        last   = _last_click_path[0]
        active = active_group_var.get()
        if last and last in _ordered_scenarios and sp in _ordered_scenarios:
            lo = min(_ordered_scenarios.index(last), _ordered_scenarios.index(sp))
            hi = max(_ordered_scenarios.index(last), _ordered_scenarios.index(sp))
            for _item in _ordered_scenarios[lo:hi + 1]:
                if active == "_clear":
                    case_group.pop(_item, None)
                else:
                    case_group[_item] = active
                _refresh_scenario_ind(_item)
        else:
            _assign_scenario(sp)
        _last_click_path[0] = sp
        _update_preview_pp(sp)

    def _build_scenario_list(scenarios_by_output: dict[str, list[str]]) -> None:
        """Populate the scenario pane; one row per scenario folder."""
        for w in scen_inner.winfo_children():
            w.destroy()
        case_group.clear()
        _scenario_indicators.clear()
        _ordered_scenarios.clear()

        if not scenarios_by_output:
            tk.Label(scen_inner, text="(no scenarios found)",
                     fg="#aaaaaa", bg="white").pack(anchor="w", padx=6, pady=4)
            scen_canvas.configure(scrollregion=scen_canvas.bbox("all"))
            return

        for output_name, scen_paths in sorted(scenarios_by_output.items()):
            hdr_frame = tk.Frame(scen_inner, bg="#e8f4f8")
            hdr_frame.pack(fill="x", pady=(8, 0))
            tk.Label(hdr_frame, text=output_name, bg="#e8f4f8", fg="#005f73",
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(
                fill="x", padx=6, pady=3)
            tk.Frame(scen_inner, bg="#94c7d8", height=1).pack(fill="x")

            for sp in sorted(scen_paths):
                if sp in _saved_case_groups:
                    case_group[sp] = _saved_case_groups[sp]
                grp = case_group.get(sp)
                _ordered_scenarios.append(sp)

                row = tk.Frame(scen_inner, bg="white", cursor="hand2")
                row.pack(fill="x", padx=2, pady=1)

                grp_lbl_ch = _group_labels.get(grp, grp) if grp else ""
                ind = tk.Label(row,
                               text=grp_lbl_ch[0].upper() if grp_lbl_ch else "",
                               width=2, font=("Segoe UI", 7, "bold"),
                               bg=_GROUP_COLORS.get(grp, "#e5e7eb") if grp else "#e5e7eb",
                               fg="white", relief="flat")
                ind.pack(side="left", padx=(4, 6), pady=2)

                name_lbl = tk.Label(row, text=Path(sp).name,
                                    anchor="w", bg="white", font=("Segoe UI", 8))
                name_lbl.pack(side="left", fill="x", expand=True, pady=2)

                _scenario_indicators[sp] = ind

                for widget in (row, ind, name_lbl):
                    widget.bind("<Button-1>",
                                lambda _e, p=sp: _assign_scenario(p))
                    widget.bind("<Shift-Button-1>",
                                lambda _e, p=sp: _assign_scenario_range(p))

        scen_inner.update_idletasks()
        scen_canvas.configure(scrollregion=scen_canvas.bbox("all"))

    def on_load_cases():
        source = pp_source_var.get()
        dirs: list[str] = []

        if source == "post_smooth":
            get_out = _get_output_folder[0]
            if get_out is None:
                messagebox.showwarning("Not ready", "Output folder not yet available.")
                return
            out_raw = get_out().strip()
            smooth_root = (Path(out_raw) / "post_smoothed") if out_raw else None
            if not smooth_root or not smooth_root.exists():
                load_case_status.set("  Post-smoothed folder not found.")
                messagebox.showwarning(
                    "Post-smoothed folder not found",
                    f"The post-smoothed VTP folder does not exist yet:\n\n"
                    f"  {smooth_root}\n\n"
                    "Run Processing with 'Save post-smooth VTP' enabled first.",
                )
                return
            for output_dir in sorted(smooth_root.iterdir()):
                if output_dir.is_dir():
                    for case_dir in sorted(output_dir.iterdir()):
                        if case_dir.is_dir():
                            for scenario_dir in sorted(case_dir.iterdir()):
                                if scenario_dir.is_dir():
                                    dirs.append(str(scenario_dir))
            if not dirs:
                load_case_status.set("  Post-smoothed folder exists but is empty.")
                return
        else:
            get_dirs = _get_tab1_dirs[0]
            if get_dirs is None:
                messagebox.showwarning("Not ready",
                                       "Tab 1 not yet initialised. Please wait.")
                return
            raw_dirs = get_dirs()
            if not raw_dirs:
                messagebox.showwarning(
                    "No directories",
                    "Please add at least one folder in the Tab 1 directory list.")
                return
            for d in raw_dirs:
                p = Path(d)
                if not p.is_dir():
                    continue
                if p.name.upper().startswith("OUTPUT_"):
                    for sub in sorted(p.iterdir()):
                        if sub.is_dir():
                            dirs.append(str(sub))
                else:
                    dirs.append(d)

        pat        = pp_pattern_var.get() or "smoothed_results_*.vtp"
        raw_filter = pp_filter_var.get().strip()
        terms      = ([t.strip().lower() for t in raw_filter.split(",") if t.strip()]
                      if raw_filter else [])

        # Build: {output_name: [scenario_path, ...]} and unique component names
        scenarios_by_output: dict[str, list[str]] = {}
        _all_comp_names.clear()
        comp_set: set[str] = set()
        _comp_files.clear()   # {comp: {scenario_path: file_path}} — for Assembly Merge panel
        n_files = 0

        for d in dirs:
            p = Path(d)
            if not p.is_dir():
                continue
            output_name, _, _ = extract_case_scenario(str(p))
            scan_dir = (p / "SMOOTHED"
                        if source == "original_smooth" and (p / "SMOOTHED").is_dir()
                        else p)
            files = sorted(scan_dir.rglob(pat))
            if terms:
                files = [f for f in files
                         if any(t in f.stem.lower() for t in terms)]
            if not files:
                continue
            sp = str(p)
            scenarios_by_output.setdefault(output_name, [])
            if sp not in scenarios_by_output[output_name]:
                scenarios_by_output[output_name].append(sp)
            n_files += len(files)
            for f in files:
                comp = f.stem
                for pfx in ("smoothed_results_", "results_",
                            "post_smooth_results_", "merged_results_"):
                    if comp.lower().startswith(pfx):
                        comp = comp[len(pfx):]
                        break
                comp_set.add(comp)
                _comp_files.setdefault(comp, {}).setdefault(sp, str(f))

        if source == "post_smooth":
            _ff: set[float] = set()
            for _d in dirs:
                _mf = Path(_d) / "_mult_factor.txt"
                if _mf.exists():
                    try: _ff.add(float(_mf.read_text().strip()))
                    except Exception: pass
            _baked_factor[0] = (
                _ff.pop() if len(_ff) == 1 else
                0.0 if len(_ff) > 1 else 1.0)
        else:
            _baked_factor[0] = 1.0
        _cases_loaded_for_source[0] = source

        # Populate component listbox
        _all_comp_names.extend(sorted(comp_set))
        comp_listbox.delete(0, "end")
        for cn in _all_comp_names:
            comp_listbox.insert("end", cn)
        comp_hdr_lbl.configure(text=f"Components ({len(_all_comp_names)})")
        if _all_comp_names:
            comp_listbox.selection_set(0)

        n_scenarios = sum(len(v) for v in scenarios_by_output.values())
        _update_pp_mult_state()
        _build_scenario_list(scenarios_by_output)
        _build_assembly_rows()
        if n_scenarios:
            src_label = "post-smoothed" if source == "post_smooth" else "input"
            load_case_status.set(
                f"  {len(scenarios_by_output)} output(s), "
                f"{n_scenarios} scenario(s), {n_files} file(s) found  [{src_label}]")
        else:
            load_case_status.set("  No matching files found.")
        _mark_load_fresh()

    load_case_btn.configure(command=on_load_cases)

    def _sel_all():
        """Assign all visible scenarios to the active group."""
        active = active_group_var.get()
        if active == "_clear":
            for sp in list(_scenario_indicators):
                case_group.pop(sp, None)
                _refresh_scenario_ind(sp)
        else:
            for sp in _scenario_indicators:
                case_group[sp] = active
                _refresh_scenario_ind(sp)

    def _desel_all():
        """Remove all scenarios from all groups."""
        for sp in list(_scenario_indicators):
            case_group.pop(sp, None)
            _refresh_scenario_ind(sp)

    # ── Group toolbar ────────────────────────────────────────────────────────
    # Toolbar lives in selall_frame: active tool buttons + assign-all + clear-all
    tk.Label(selall_frame, text="Active tool:",
             font=("Segoe UI", 8), fg="#555555").pack(side="left", padx=(0, 4))
    _tool_btns: dict[str, tk.Button] = {}

    def _select_tool(name: str) -> None:
        active_group_var.set(name)
        for n, btn in _tool_btns.items():
            is_active = (n == name)
            relief = "sunken" if is_active else "raised"
            bd     = 3 if is_active else 1
            bg     = _GROUP_COLORS.get(n, btn.cget("bg"))
            btn.configure(relief=relief, bd=bd, bg=bg)

    def _rename_tool(key: str) -> None:
        """Prompt user to rename tool *key*; update button text and indicators."""
        from tkinter import simpledialog

        _base_dir = Path(__file__).resolve().parent.parent.parent
        _ico = _base_dir / "Beam.ico"
        _png = _base_dir / "Beam.png"

        class _IconQueryString(simpledialog._QueryString):
            def body(self, master):
                widget = super().body(master)
                # Set the app icon now that the Toplevel window exists
                try:
                    if _ico.exists() and sys.platform.startswith("win"):
                        self.iconbitmap(str(_ico))
                    elif _png.exists():
                        self._dlg_icon = tk.PhotoImage(file=str(_png))
                        self.iconphoto(False, self._dlg_icon)
                except Exception:
                    pass
                return widget

        current = _group_labels.get(key, key)
        new_name = _IconQueryString(
            "Rename group",
            f"Enter a new name for the '{current}' tool:\n"
            "(used as VTP filename prefix)",
            initialvalue=current,
            parent=tab3,
        ).result
        if not new_name or not new_name.strip():
            return
        new_name = new_name.strip()
        _group_labels[key] = new_name
        # Update tool button label and re-apply bg (Windows may reset it after dialog focus)
        btn = _tool_btns.get(key)
        if btn:
            btn.configure(text=new_name, bg=_GROUP_COLORS.get(key, btn.cget("bg")))
        # Update all indicator squares that currently carry this group
        for sp, ind in _scenario_indicators.items():
            if case_group.get(sp) == key:
                ind.configure(text=new_name[0].upper())
        # Re-assert active tool styling (dialog focus loss can visually reset buttons)
        _select_tool(active_group_var.get())

    for _gname, _gcol in MERGE_GROUPS:
        _btn = tk.Button(
            selall_frame,
            text=_group_labels.get(_gname, _gname),
            bg=_gcol, fg="white",
            font=("Segoe UI", 8, "bold"),
            relief="raised", bd=1, padx=6, pady=1,
            cursor="hand2",
            command=lambda n=_gname: _select_tool(n),
        )
        _btn.pack(side="left", padx=(0, 3))
        _btn.bind("<Double-Button-1>", lambda _e, n=_gname: _rename_tool(n))
        _tool_btns[_gname] = _btn

    # Separator + utility buttons
    tk.Label(selall_frame, text="|", fg="#cccccc").pack(side="left", padx=4)
    tk.Button(selall_frame, text="Assign all", width=9,
              command=_sel_all).pack(side="left", padx=(0, 3))
    tk.Button(selall_frame, text="Clear all",  width=9,
              command=_desel_all).pack(side="left", padx=(0, 3))

    # Activate first tool by default
    _select_tool(MERGE_GROUPS[0][0])

    # ── Tool-specific settings, organised as sub-tabs ─────────────────────────
    # Input source, Cases (scenario browser + colour-group assignment) above,
    # and "Merge arrays" below are shared by both merge tools. Each tool's OWN
    # settings + its own Run button live in a separate sub-tab so the two
    # workflows are visually separated instead of being interleaved.
    pp_tools_nb = ttk.Notebook(tab3)
    tab_cg  = tk.Frame(pp_tools_nb)   # colour-group (cell-wise max) tool
    tab_asm = tk.Frame(pp_tools_nb)   # worst-case assembly tool
    pp_tools_nb.add(tab_cg,  text="Colour-Group Merge  (cell-wise max)")
    pp_tools_nb.add(tab_asm, text="Worst-Case Assembly Merge")

    # ── Multiplication factor ─────────────────────────────────────────────────
    mult_lframe = tk.LabelFrame(tab_cg, text="Snapshot factor (visual only)", padx=8, pady=4)
    _mult_frame_row = tk.Frame(mult_lframe)
    _mult_frame_row.pack(fill="x")
    pp_mult_var = tk.StringVar(value=str(pp_s.get("mult_factor_pp", "1.0")))
    pp_mult_label = tk.Label(_mult_frame_row, text="Snapshot factor:",
                             font=("Segoe UI", 9, "bold"))
    pp_mult_label.pack(side="left")
    pp_mult_entry = tk.Entry(_mult_frame_row, textvariable=pp_mult_var, width=10)
    pp_mult_entry.pack(side="left", padx=(8, 0))
    pp_mult_desc = tk.Label(_mult_frame_row,
                            text="(applied to snapshots only — VTPs always store raw values)",
                            fg="#64748b")
    pp_mult_desc.pack(side="left", padx=(8, 4))
    pp_mult_note = tk.Label(_mult_frame_row, text="", fg="#b45309",
                            font=("Segoe UI", 8, "italic"))
    pp_mult_note.pack(side="left")
    # Always visible — enable/disable controlled by source toggle below
    mult_lframe.pack(fill="x", padx=10, pady=(4, 2))

    # ── Merge arrays ───────────────────────────────────────────────────────────
    merge_lframe = tk.LabelFrame(tab3, text="Merge arrays", padx=8, pady=4)
    merge_lframe.pack(fill="x", padx=10, pady=(4, 2))
    merge_row = tk.Frame(merge_lframe)
    merge_row.pack(fill="x")
    pp_merge_pd_var  = tk.BooleanVar(value=bool(pp_s.get("merge_pd",  True)))
    pp_merge_pwr_var = tk.BooleanVar(value=bool(pp_s.get("merge_pwr", True)))
    pp_merge_pd_chk  = tk.Checkbutton(merge_row, text="Power Density  (Power_Density_W_m2)",
                                      variable=pp_merge_pd_var)
    pp_merge_pd_chk.pack(side="left", padx=(0, 20))
    pp_merge_pwr_chk = tk.Checkbutton(merge_row, text="Total Power  (Deposited_Power_W)",
                                      variable=pp_merge_pwr_var)
    pp_merge_pwr_chk.pack(side="left")
    tk.Label(merge_row, text="  (at least one required)",
             fg="#888888", font=("Segoe UI", 8, "italic")).pack(side="left", padx=(12, 0))

    # ── Worst-Case Assembly Merge (per-component scenario picker) ────────────
    # This is a SEPARATE tool from the colour-group merge above:
    #   - Colour-group tool: cell-wise MAX-merges multiple scenario files of the
    #     SAME component (one output file per component).
    #   - Assembly tool: picks exactly ONE scenario file per component (the
    #     user's chosen "worst case" for that component) and appends all of
    #     them together (vtkAppendPolyData) into a SINGLE combined VTK file.
    #     The same scenario may be picked for more than one component.
    # It shares "Merge arrays" (pp_merge_pd_var/pp_merge_pwr_var) above but has
    # its own data, its own button and does not touch case_group/_saved_case_groups.
    assembly_lframe = tk.Frame(tab_asm, padx=8, pady=6)
    tk.Label(
        assembly_lframe,
        text="Pick one scenario per component below, then merge all selections "
             "into a single VTK file. The same scenario may be reused for "
             "multiple components.",
        fg="#444444", anchor="w", justify="left", wraplength=760,
    ).pack(fill="x", pady=(0, 6))

    _comp_files: dict[str, dict[str, str]] = {}       # {comp: {scenario_path: file_path}}
    _comp_selected_scenario: dict[str, str] = {}      # {comp: scenario_path}
    _comp_label_maps: dict[str, dict[str, str]] = {}  # {comp: {display_label: scenario_path}}
    _saved_assembly_selection: dict[str, str] = dict(pp_s.get("assembly_selection", {}))

    # Metric read from the Processing-tab CSV (output/csv_file_comparison/
    # max_comparison_batch_*.csv) that "Auto-pick worst per component" ranks by.
    _ASSEMBLY_METRIC_OPTIONS = ["main_area_max", "max_after", "max_before",
                               "total_power_after", "total_power_before"]
    assembly_metric_var = tk.StringVar(
        value=pp_s.get("assembly_metric", "main_area_max"))

    # Which Processing CSV(s) the ranking reads. The sentinel below keeps the
    # original behaviour (read every CSV, newest wins on duplicate keys); any
    # other value pins the ranking to that single run's CSV.
    _ASSEMBLY_CSV_ALL = "(all runs — newest wins)"
    _ASSEMBLY_CSV_PREFIX = "max_comparison_batch_"
    # {display_label: Path} for the CSVs currently on disk, rebuilt on demand.
    _assembly_csv_map: dict[str, Path] = {}
    assembly_csv_var = tk.StringVar(value=_ASSEMBLY_CSV_ALL)
    # Filename remembered from settings/config; resolved to a label once the
    # output folder is known and the folder has been scanned.
    _saved_assembly_csv: list = [str(pp_s.get("assembly_csv", "") or "")]

    def _scenario_label(sp: str) -> str:
        try:
            output_name, case, scenario = extract_case_scenario(sp)
        except Exception:
            return Path(sp).name
        base = f"{output_name} / {case} / {scenario}"
        leaf = Path(sp).name
        return base if leaf in (case, scenario) else f"{base}  [{leaf}]"

    def _unique_labels(sps: list) -> dict:
        """Map each scenario path to a unique display label (dedup collisions)."""
        labels: dict[str, str] = {}
        counts: dict[str, int] = {}
        for sp in sps:
            lbl = _scenario_label(sp)
            if lbl in labels:
                counts[lbl] = counts.get(lbl, 1) + 1
                lbl = f"{lbl} ({counts[lbl]})"
            labels[lbl] = sp
        return labels

    assembly_top = tk.Frame(assembly_lframe)
    assembly_top.pack(fill="x", pady=(0, 4))
    tk.Label(assembly_top, text="Assembly name:").pack(side="left")
    assembly_name_var = tk.StringVar(value=pp_s.get("assembly_name", "assembly"))
    tk.Entry(assembly_top, textvariable=assembly_name_var, width=24).pack(
        side="left", padx=(6, 16))
    tk.Label(assembly_top, text="Rank by:").pack(side="left")
    assembly_metric_cb = ttk.Combobox(
        assembly_top, textvariable=assembly_metric_var,
        values=_ASSEMBLY_METRIC_OPTIONS, state="readonly", width=18)
    assembly_metric_cb.pack(side="left", padx=(6, 16))

    def _scan_processing_csvs() -> list:
        """Every Processing-tab CSV on disk (oldest → newest), regardless of
        the user's pick. Located at {output_folder}/csv_file_comparison/."""
        get_out = _get_output_folder[0]
        if get_out is None:
            return []
        out_raw = get_out().strip()
        if not out_raw:
            return []
        csv_dir = Path(out_raw) / "csv_file_comparison"
        if not csv_dir.is_dir():
            return []
        # Filenames carry a %Y-%m-%d_%H-%M-%S stamp, so lexical == chronological.
        return sorted(csv_dir.glob(f"{_ASSEMBLY_CSV_PREFIX}*.csv"))

    def _csv_label(fp: Path) -> str:
        """Display label for a CSV: just its run timestamp. Kept bijective with
        the filename so a pick can be persisted and restored reliably."""
        stem = fp.stem
        if stem.startswith(_ASSEMBLY_CSV_PREFIX):
            stem = stem[len(_ASSEMBLY_CSV_PREFIX):]
        return stem or fp.name

    def _rebuild_csv_map() -> None:
        """Rescan the CSV folder into {label: Path}, chronological. No UI side
        effects, so it is safe to call while resolving a pick."""
        _assembly_csv_map.clear()
        for fp in _scan_processing_csvs():
            _assembly_csv_map[_csv_label(fp)] = fp

    def _refresh_csv_choices() -> None:
        """Rescan the CSV folder and repopulate the picker (newest first).

        Wired to the combobox's *postcommand* so the list is always current when
        the user opens it — the output folder can change and the Processing tab
        writes a new CSV on every run."""
        _rebuild_csv_map()
        # Newest first: most recent run is the usual pick.
        labels = list(reversed(list(_assembly_csv_map.keys())))
        assembly_csv_cb.configure(values=[_ASSEMBLY_CSV_ALL] + labels)

        # Restore a pick remembered from settings/config once it shows up.
        want = _saved_assembly_csv[0]
        if want:
            match = next((lbl for lbl, fp in _assembly_csv_map.items()
                          if fp.name == want), None)
            if match is not None:
                assembly_csv_var.set(match)
                _saved_assembly_csv[0] = ""
        # A pinned CSV that has since been deleted/renamed falls back to "all"
        # rather than silently ranking against nothing.
        if (assembly_csv_var.get() != _ASSEMBLY_CSV_ALL
                and assembly_csv_var.get() not in _assembly_csv_map):
            assembly_csv_var.set(_ASSEMBLY_CSV_ALL)

    def _selected_csv_filename() -> str:
        """The pinned CSV's filename for persistence, or "" for "all runs".
        Falls back to the not-yet-resolved saved name so a pin survives a
        session in which the output folder was never populated."""
        chosen = assembly_csv_var.get()
        if not chosen or chosen == _ASSEMBLY_CSV_ALL:
            return _saved_assembly_csv[0]
        if chosen not in _assembly_csv_map:
            _rebuild_csv_map()
        fp = _assembly_csv_map.get(chosen)
        return fp.name if fp is not None else _saved_assembly_csv[0]

    def _find_processing_csvs() -> list:
        """The CSV(s) the ranking should read, oldest → newest so the newest
        wins on duplicate keys. Honours the user's pick in the CSV combobox:
        a single pinned file, or every CSV when "all runs" is selected."""
        # A pin restored from settings can still be pending here, because the
        # output folder is injected by the caller after this tab is built.
        if _saved_assembly_csv[0]:
            _refresh_csv_choices()
        chosen = assembly_csv_var.get()
        if chosen and chosen != _ASSEMBLY_CSV_ALL:
            # The map is only rebuilt when the dropdown opens, so it can be
            # stale (or empty) here — rescan before concluding the pick is gone.
            if chosen not in _assembly_csv_map:
                _rebuild_csv_map()
            fp = _assembly_csv_map.get(chosen)
            if fp is not None and fp.is_file():
                return [fp]
            return []
        return _scan_processing_csvs()

    assembly_top2 = tk.Frame(assembly_lframe)
    assembly_top2.pack(fill="x", pady=(0, 6))
    tk.Label(assembly_top2, text="from Processing CSV:").pack(side="left")
    assembly_csv_cb = ttk.Combobox(
        assembly_top2, textvariable=assembly_csv_var, state="readonly", width=26)
    assembly_csv_cb.pack(side="left", padx=(6, 8))
    assembly_csv_cb.configure(postcommand=_refresh_csv_choices)
    tk.Label(assembly_top2,
             text="(newest first — pick a single Processing run to rank against, "
                  "or use all runs)",
             fg="#888888", font=("Segoe UI", 8, "italic")).pack(side="left")
    _refresh_csv_choices()

    def _csv_row_component(filename: str) -> str:
        comp = Path(filename).stem
        for pfx in ("smoothed_results_", "results_",
                    "post_smooth_results_", "merged_results_"):
            if comp.lower().startswith(pfx):
                return comp[len(pfx):]
        return comp

    def _build_csv_metric_index(metric_col: str) -> dict:
        """Return {(case, scenario, comp): metric_value} parsed straight out of
        the Processing-tab CSV(s) — no VTP files are opened, so this is fast
        even with many components/scenarios. Matches by (case, scenario,
        component) rather than file path, since Tab 3 may browse a different
        source (e.g. post-smoothed VTPs) than the original files the CSV was
        written for."""
        index: dict = {}
        for csv_fp in _find_processing_csvs():
            try:
                with open(csv_fp, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        filename = (row.get("filename") or "").strip()
                        if not filename:
                            continue
                        val_raw = (row.get(metric_col) or "").strip()
                        if not val_raw:
                            continue
                        try:
                            val = float(val_raw)
                        except ValueError:
                            continue
                        case     = (row.get("case") or "").strip()
                        scenario = (row.get("scenario") or "").strip()
                        comp     = _csv_row_component(filename)
                        index[(case, scenario, comp)] = val
            except Exception:
                continue
        return index

    def _auto_pick_worst() -> None:
        """Pre-select, for each component, the scenario whose Processing-tab
        CSV row has the highest value in the chosen metric column. Reads the
        CSV(s) already written by the Processing tab instead of re-opening
        every candidate VTP file (much faster). Purely a starting point —
        every pick can still be changed manually in the dropdowns below."""
        if not _all_comp_names:
            messagebox.showinfo("No data", "Click Load Cases first.")
            return
        metric_col = assembly_metric_var.get() or "main_area_max"
        csvs = _find_processing_csvs()
        if not csvs:
            if assembly_csv_var.get() != _ASSEMBLY_CSV_ALL:
                messagebox.showwarning(
                    "Selected CSV not available",
                    f"The pinned Processing CSV '{assembly_csv_var.get()}' could "
                    "not be read — it may have been moved or deleted, or the "
                    "output folder has changed.\n\n"
                    "Re-open the 'from Processing CSV' dropdown to pick an "
                    "available run.")
            else:
                messagebox.showwarning(
                    "No Processing CSV found",
                    "No 'max_comparison_batch_*.csv' file was found in:\n\n"
                    "  {output_folder}\\csv_file_comparison\\\n\n"
                    "Run the Processing tab at least once first — auto-pick reads "
                    "its per-file metrics instead of re-opening every VTP.")
            return
        metric_index = _build_csv_metric_index(metric_col)
        n_done, n_skipped = 0, 0
        for comp in _all_comp_names:
            scen_map = _comp_files.get(comp, {})
            if not scen_map:
                continue
            best_sp, best_val = None, None
            for sp in scen_map:
                try:
                    _, case, scenario = extract_case_scenario(sp)
                except Exception:
                    continue
                val = metric_index.get((case, scenario, comp))
                if val is None:
                    continue
                if best_val is None or val > best_val:
                    best_val, best_sp = val, sp
            if best_sp is not None:
                _comp_selected_scenario[comp] = best_sp
                n_done += 1
            else:
                n_skipped += 1
        _build_assembly_rows()
        src_desc = (f"all {len(csvs)} Processing CSV(s)"
                    if assembly_csv_var.get() == _ASSEMBLY_CSV_ALL
                    else f"Processing CSV '{csvs[0].name}'")
        msg = (f"Ranked by '{metric_col}' from {src_desc}.\n\n"
               f"Selected the highest-value scenario for {n_done} component(s).")
        if n_skipped:
            msg += (f"\n{n_skipped} component(s) had no matching CSV data and "
                    "were left unchanged.")
        msg += "\n\nReview and adjust any picks below as needed."
        messagebox.showinfo("Auto-pick complete", msg)

    def _clear_assembly_selections() -> None:
        _comp_selected_scenario.clear()
        _build_assembly_rows()

    tk.Button(assembly_top, text="Auto-pick worst per component", width=26,
              command=_auto_pick_worst).pack(side="left", padx=(0, 8))
    tk.Button(assembly_top, text="Clear selections", width=14,
              command=_clear_assembly_selections).pack(side="left")

    assembly_body = tk.Frame(assembly_lframe)
    assembly_body.pack(fill="both", expand=True)
    assembly_canvas = tk.Canvas(assembly_body, bg="white", height=220,
                                highlightthickness=0)
    assembly_vscroll = tk.Scrollbar(assembly_body, orient="vertical",
                                    command=assembly_canvas.yview)
    assembly_canvas.configure(yscrollcommand=assembly_vscroll.set)
    assembly_vscroll.pack(side="right", fill="y")
    assembly_canvas.pack(side="left", fill="both", expand=True)
    assembly_inner = tk.Frame(assembly_canvas, bg="white")
    _assembly_win = assembly_canvas.create_window((0, 0), window=assembly_inner, anchor="nw")
    assembly_inner.bind("<Configure>",
                        lambda e: assembly_canvas.configure(
                            scrollregion=assembly_canvas.bbox("all")))
    assembly_canvas.bind("<Configure>",
                         lambda e: assembly_canvas.itemconfig(_assembly_win, width=e.width))

    def _on_assembly_scroll(event):
        assembly_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    assembly_canvas.bind("<Enter>",
                         lambda _: assembly_canvas.bind_all("<MouseWheel>", _on_assembly_scroll))
    assembly_canvas.bind("<Leave>",
                         lambda _: assembly_canvas.unbind_all("<MouseWheel>"))

    assembly_status_var = tk.StringVar(value="  (click Load Cases above to populate)")
    tk.Label(assembly_lframe, textvariable=assembly_status_var, fg="#555555",
             anchor="w").pack(fill="x", pady=(2, 4))

    assembly_run_btn = tk.Button(
        assembly_lframe, text="Merge Selected \u2192 Single VTK", width=26,
        bg="#7c3aed", fg="white", font=("Segoe UI", 10, "bold"))
    assembly_run_btn.pack(anchor="w")

    def _build_assembly_rows() -> None:
        for w in assembly_inner.winfo_children():
            w.destroy()
        _comp_label_maps.clear()

        if not _all_comp_names:
            tk.Label(assembly_inner, text="(no components loaded \u2014 click Load Cases)",
                     fg="#aaaaaa", bg="white").pack(anchor="w", padx=6, pady=4)
            assembly_status_var.set("  (no components loaded)")
            return

        n_with_options = 0
        for comp in _all_comp_names:
            scen_map = _comp_files.get(comp, {})
            # Restore a persisted pick on first build after Load Cases, if still valid
            if (comp not in _comp_selected_scenario
                    and comp in _saved_assembly_selection
                    and _saved_assembly_selection[comp] in scen_map):
                _comp_selected_scenario[comp] = _saved_assembly_selection[comp]

            label_map = _unique_labels(sorted(scen_map.keys()))
            _comp_label_maps[comp] = label_map

            row = tk.Frame(assembly_inner, bg="white")
            row.pack(fill="x", padx=2, pady=1)
            tk.Label(row, text=comp, width=22, anchor="w", bg="white",
                     font=("Segoe UI", 8)).pack(side="left", padx=(4, 6))

            if not label_map:
                tk.Label(row, text="(no matching files)", bg="white",
                         fg="#bbbbbb", font=("Segoe UI", 8, "italic")).pack(
                    side="left", fill="x", expand=True)
                continue

            n_with_options += 1
            options = ["(select)"] + list(label_map.keys())
            prev_sp  = _comp_selected_scenario.get(comp)
            prev_lbl = next((lbl for lbl, sp in label_map.items() if sp == prev_sp),
                            "(select)")
            var = tk.StringVar(value=prev_lbl)
            cb = ttk.Combobox(row, textvariable=var, values=options,
                              state="readonly", width=48)
            cb.pack(side="left", fill="x", expand=True, padx=(0, 4))

            def _on_pick(_e=None, c=comp, v=var, lm=label_map):
                chosen = v.get()
                if chosen == "(select)":
                    _comp_selected_scenario.pop(c, None)
                else:
                    _comp_selected_scenario[c] = lm.get(chosen)
                assembly_status_var.set(
                    f"  {len(_comp_selected_scenario)}/{len(_all_comp_names)} "
                    "component(s) selected")

            cb.bind("<<ComboboxSelected>>", _on_pick)

        assembly_inner.update_idletasks()
        assembly_canvas.configure(scrollregion=assembly_canvas.bbox("all"))
        assembly_status_var.set(
            f"  {n_with_options}/{len(_all_comp_names)} component(s) have available "
            f"scenarios  \u2014  {len(_comp_selected_scenario)} selected")

    def get_assembly_cfg() -> dict | None:
        """Validate and return the Assembly Merge run cfg, or None on invalid input."""
        if not _all_comp_names:
            messagebox.showwarning("No data", "Click Load Cases first.")
            return None
        selection: dict[str, str] = {}
        missing: list[str] = []
        for comp in _all_comp_names:
            sp = _comp_selected_scenario.get(comp)
            if sp is None:
                missing.append(comp)
                continue
            fp = _comp_files.get(comp, {}).get(sp)
            if fp:
                selection[comp] = fp
        if missing:
            if not messagebox.askyesno(
                    "Missing selections",
                    f"{len(missing)} component(s) have no scenario selected and "
                    "will be excluded from the merge:\n\n" + ", ".join(sorted(missing)) +
                    "\n\nContinue anyway?"):
                return None
        if not selection:
            messagebox.showwarning("Nothing selected",
                                   "Select a scenario for at least one component.")
            return None
        if not pp_merge_pd_var.get() and not pp_merge_pwr_var.get():
            messagebox.showwarning(
                "No merge array selected",
                "Select at least one array to merge (Power Density or Total Power).")
            return None
        return {
            "component_selection": selection,
            "assembly_name": assembly_name_var.get().strip() or "assembly",
            "merge_pd":  pp_merge_pd_var.get(),
            "merge_pwr": pp_merge_pwr_var.get(),
        }

    assembly_lframe.pack(fill="both", expand=True, padx=4, pady=4)

    # ── Snapshots ─────────────────────────────────────────────────────────────
    snap_lframe = tk.LabelFrame(tab_cg, text="Snapshots", padx=8, pady=4)
    snap_row = tk.Frame(snap_lframe)
    snap_row.pack(fill="x")
    pp_save_snaps_var = tk.BooleanVar(value=bool(pp_s.get("save_snapshots", False)))
    pp_snap_pd_var    = tk.BooleanVar(value=bool(pp_s.get("snap_pwr_density", True)))
    pp_snap_tp_var    = tk.BooleanVar(value=bool(pp_s.get("snap_total_pwr",   False)))

    pp_snaps_chk = tk.Checkbutton(snap_row, text="Save snapshots",
                                  variable=pp_save_snaps_var)
    pp_snaps_chk.pack(side="left", padx=(0, 16))
    pp_snap_pd_chk = tk.Checkbutton(snap_row, text="Power density",
                                    variable=pp_snap_pd_var)
    pp_snap_pd_chk.pack(side="left", padx=(0, 8))
    pp_snap_tp_chk = tk.Checkbutton(snap_row, text="Total power",
                                    variable=pp_snap_tp_var)
    pp_snap_tp_chk.pack(side="left")
    tk.Label(snap_row, text="  (saved to output/post_processed_snapshots/)",
             fg="#888888").pack(side="left", padx=(12, 0))

    def _update_snap_state(*_):
        en = "normal" if pp_save_snaps_var.get() else "disabled"
        pp_snap_pd_chk.configure(state=en)
        pp_snap_tp_chk.configure(state=en)

    pp_save_snaps_var.trace_add("write", _update_snap_state)
    _update_snap_state()

    # ── Run button ────────────────────────────────────────────────────────────
    btn_frame = tk.Frame(tab_cg)

    tab3_run_btn = tk.Button(
        btn_frame, text="Run Post Processing", width=20,
        bg="#0060c0", fg="white", font=("Segoe UI", 10, "bold"),
    )
    tab3_run_btn.pack(side="left", padx=6)

    # ── Pack order: cases → merge arrays (shared) → tool sub-tabs ────────────
    snap_lframe.pack(fill="x", padx=10, pady=(4, 2))
    btn_frame.pack(pady=(4, 10))
    pp_tools_nb.pack(fill="both", expand=True, padx=10, pady=(4, 8))

    # src values where mult factor is editable (all original sources)
    _PP_ORIGINAL_SRCS = {"original", "original_raw", "original_smooth"}

    def _update_pp_mult_state(*_):
        src   = pp_source_var.get()
        baked = _baked_factor[0]
        # Snapshot factor is always editable — VTPs always store raw values.
        pp_mult_entry.configure(state="normal")
        pp_mult_label.configure(fg="black")
        pp_mult_desc.configure(fg="#64748b")
        get_p = _get_mult_factor_p[0]
        if src in _PP_ORIGINAL_SRCS:
            _baked_factor[0] = 1.0
            _cases_loaded_for_source[0] = src
            if get_p is not None:
                pp_mult_var.set(get_p())
            else:
                pp_mult_var.set("1.0")
            pp_mult_note.configure(text="(\u2190 pre-filled from Processing tab)")
        elif baked == 0.0:
            # Mixed snapshot factors across selected dirs
            if get_p is not None:
                pp_mult_var.set(get_p())
            pp_mult_note.configure(text="(\u26a0 mixed factors across cases \u2014 verify manually)")
        elif baked != 1.0:
            # Pre-fill from the snapshot factor recorded by Processing
            pp_mult_var.set(str(baked))
            pp_mult_note.configure(text="(\u2190 pre-filled from Processing snapshot factor)")
        else:
            # baked == 1.0: pre-fill from Processing tab if available
            if get_p is not None:
                pp_mult_var.set(get_p())
            pp_mult_note.configure(text="(\u2190 pre-filled from Processing tab)")

    pp_source_var.trace_add("write", _update_pp_mult_state)
    _update_pp_mult_state()   # apply on first render

    # ── Load Cases button highlight when source changes ───────────────────────
    _BTN_NORMAL  = {"bg": "#005f73", "fg": "white",   "relief": "raised", "bd": 1}
    _BTN_STALE   = {"bg": "#e85d04", "fg": "white",   "relief": "raised", "bd": 3}

    def _mark_load_stale(*_):
        load_case_btn.configure(**_BTN_STALE)

    def _mark_load_fresh():
        load_case_btn.configure(**_BTN_NORMAL)

    pp_source_var.trace_add("write", _mark_load_stale)

    # ── Auto-update pattern when source changes ───────────────────────────────
    _PATTERNS = {
        "original_raw":    "results_*.vtp",
        "original_smooth": "smoothed_results_*.vtp",
        "post_smooth":     "post_smooth*.vtp",   # matches post_smooth_results_* and post_smooth__results_*
        # backward compat
        "original":        "smoothed_results_*.vtp",
    }

    def _update_pattern_default(*_):
        src     = pp_source_var.get()
        default = _PATTERNS.get(src, "smoothed_results_*.vtp")
        pp_pattern_var.set(default)

    pp_source_var.trace_add("write", _update_pattern_default)

    # ── Config helpers ────────────────────────────────────────────────────────
    def get_pp_cfg() -> dict | None:
        """Validate and return run cfg dict, or None on invalid input."""
        # Build groups dict: {group_name: [dir_paths]}
        groups: dict[str, list[str]] = {}
        for sp, grp in case_group.items():
            groups.setdefault(grp, []).append(sp)
        if not groups:
            messagebox.showwarning(
                "No cases assigned",
                "Assign at least one case to a colour group using the tools above.")
            return None
        if not pp_merge_pd_var.get() and not pp_merge_pwr_var.get():
            messagebox.showwarning(
                "No merge array selected",
                "Select at least one array to merge (Power Density or Total Power).")
            return None
        try:
            mult = float(pp_mult_var.get())
        except ValueError:
            messagebox.showerror("Invalid input",
                                 "Multiplication factor must be a number.")
            return None
        source = pp_source_var.get()
        # VTPs always store raw values; apply the user-entered factor directly.
        effective_mult = mult
        return {
            "groups":          groups,
            "group_labels":    dict(_group_labels),
            "pattern":         pp_pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":     pp_filter_var.get().strip(),
            "mult_factor":     effective_mult,
            "apply_mult":      True,
            "merge_pd":        pp_merge_pd_var.get(),
            "merge_pwr":       pp_merge_pwr_var.get(),
            "save_snapshots":  pp_save_snaps_var.get(),
            "snap_pwr_density": pp_snap_pd_var.get(),
            "snap_total_pwr":  pp_snap_tp_var.get(),
            "pp_source":       source if source in {"original_raw", "original_smooth", "post_smooth"} else "original_smooth",
        }

    def get_pp_cfg_dict() -> dict:
        """Serialisable config for settings persistence."""
        src = pp_source_var.get()
        return {
            "pp_source":        src,
            "pattern":          pp_pattern_var.get(),
            "name_filter":      pp_filter_var.get(),
            "mult_factor_pp":   pp_mult_var.get() if src in _PP_ORIGINAL_SRCS else "1.0",
            "merge_pd":         pp_merge_pd_var.get(),
            "merge_pwr":        pp_merge_pwr_var.get(),
            "save_snapshots":   pp_save_snaps_var.get(),
            "snap_pwr_density": pp_snap_pd_var.get(),
            "snap_total_pwr":   pp_snap_tp_var.get(),
            "case_groups":      dict(case_group),
            "group_labels":     dict(_group_labels),
            "assembly_name":       assembly_name_var.get(),
            "assembly_metric":     assembly_metric_var.get(),
            "assembly_csv":        _selected_csv_filename(),
            "assembly_selection":  dict(_comp_selected_scenario),
        }

    def apply_pp_cfg(pp: dict) -> None:
        if not pp:
            return
        # Backward compat: remap old "original" to "original_smooth"
        _src = pp.get("pp_source", "original_smooth")
        if _src == "original":
            _src = "original_smooth"
        pp_source_var.set(_src)
        _saved_pat = pp.get("pattern", "")
        # Upgrade legacy exact patterns that no longer match the files on disk
        if _saved_pat == "post_smooth_results_*.vtp":
            _saved_pat = "post_smooth*.vtp"
        pp_pattern_var.set(_saved_pat or _PATTERNS.get(_src, "smoothed_results_*.vtp"))
        pp_filter_var.set(pp.get("name_filter", ""))
        pp_mult_var.set(str(pp.get("mult_factor_pp", "1.0")))
        # Re-trigger state to populate from live comp_widgets if non-original
        _update_pp_mult_state()
        pp_save_snaps_var.set(bool(pp.get("save_snapshots", False)))
        pp_merge_pd_var.set(bool(pp.get("merge_pd",  True)))
        pp_merge_pwr_var.set(bool(pp.get("merge_pwr", True)))
        pp_snap_pd_var.set(bool(pp.get("snap_pwr_density", True)))
        pp_snap_tp_var.set(bool(pp.get("snap_total_pwr", False)))
        _saved_case_groups.clear()
        _saved_case_groups.update(pp.get("case_groups", {}))
        # Restore custom group labels
        _group_labels.update(pp.get("group_labels", {}))
        for key, btn in _tool_btns.items():
            if key in _group_labels:
                btn.configure(text=_group_labels[key])
        # Re-apply saved assignments to already-loaded indicator widgets
        for sp, ind in _scenario_indicators.items():
            grp = _saved_case_groups.get(sp)
            if grp:
                case_group[sp] = grp
                ind.configure(bg=_GROUP_COLORS.get(grp, "#e5e7eb"),
                              text=grp[0].upper(), fg="white")
            else:
                case_group.pop(sp, None)
                ind.configure(bg="#e5e7eb", text="", fg="#e5e7eb")
        # Restore Assembly Merge name + selections (applied to already-built rows, if any)
        assembly_name_var.set(pp.get("assembly_name", assembly_name_var.get()))
        assembly_metric_var.set(pp.get("assembly_metric", assembly_metric_var.get()))
        # Pinned Processing CSV: remembered by filename and resolved against
        # whatever is on disk for the current output folder.
        _saved_assembly_csv[0] = str(pp.get("assembly_csv", "") or "")
        if not _saved_assembly_csv[0]:
            assembly_csv_var.set(_ASSEMBLY_CSV_ALL)
        _refresh_csv_choices()
        _saved_assembly_selection.clear()
        _saved_assembly_selection.update(pp.get("assembly_selection", {}))
        if _comp_label_maps:
            _comp_selected_scenario.clear()
            _build_assembly_rows()

    return {
        "tab3_run_btn":       tab3_run_btn,
        "get_pp_cfg":         get_pp_cfg,
        "get_pp_cfg_dict":    get_pp_cfg_dict,
        "apply_pp_cfg":       apply_pp_cfg,
        "_get_tab1_dirs":     _get_tab1_dirs,
        "_get_output_folder": _get_output_folder,
        "_get_mult_factor_p": _get_mult_factor_p,
        "_on_load_cases":     on_load_cases,
        "case_group":         case_group,
        "pp_source_var":      pp_source_var,
        "assembly_run_btn":   assembly_run_btn,
        "get_assembly_cfg":   get_assembly_cfg,
    }
