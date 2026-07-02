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

import sys
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

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

    # ── Multiplication factor ─────────────────────────────────────────────────
    mult_lframe = tk.LabelFrame(tab3, text="Snapshot factor (visual only)", padx=8, pady=4)
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

    # ── Snapshots ─────────────────────────────────────────────────────────────
    snap_lframe = tk.LabelFrame(tab3, text="Snapshots", padx=8, pady=4)
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
    btn_frame = tk.Frame(tab3)

    tab3_run_btn = tk.Button(
        btn_frame, text="Run Post Processing", width=20,
        bg="#0060c0", fg="white", font=("Segoe UI", 10, "bold"),
    )
    tab3_run_btn.pack(side="left", padx=6)

    # ── Pack order: cases → mult → merge → snaps → run ───────────────────────
    snap_lframe.pack(fill="x", padx=10, pady=(4, 2))
    btn_frame.pack(pady=(4, 10))

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
    }
