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

    # ── Source + pattern/filter ───────────────────────────────────────────────
    src_lframe = tk.LabelFrame(tab3, text="Input source", padx=8, pady=6)
    src_lframe.pack(fill="x", padx=10, pady=(8, 4))

    pp_source_var = tk.StringVar(value=pp_s.get("pp_source", "original"))

    src_radio_frame = tk.Frame(src_lframe)
    src_radio_frame.pack(fill="x")
    tk.Radiobutton(src_radio_frame, text="Original input folders",
                   variable=pp_source_var, value="original").pack(side="left", padx=(0, 16))
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

    # Scrollable checkbox area + snapshot preview pane
    case_content = tk.PanedWindow(case_lframe, orient="horizontal",
                                    sashwidth=5, sashrelief="raised", bg="#d0d0d0")
    case_content.pack(fill="both", expand=True)

    chk_canvas_frame = tk.Frame(case_content)
    case_content.add(chk_canvas_frame, stretch="always", minsize=200)
    chk_canvas = tk.Canvas(chk_canvas_frame, height=150, bg="white",
                            highlightthickness=0)
    chk_vscroll = tk.Scrollbar(chk_canvas_frame, orient="vertical",
                                command=chk_canvas.yview)
    chk_hscroll = tk.Scrollbar(chk_canvas_frame, orient="horizontal",
                                command=chk_canvas.xview)
    chk_canvas.configure(yscrollcommand=chk_vscroll.set,
                         xscrollcommand=chk_hscroll.set)
    chk_vscroll.pack(side="right", fill="y")
    chk_hscroll.pack(side="bottom", fill="x")
    chk_canvas.pack(side="left", fill="both", expand=True)
    chk_inner = tk.Frame(chk_canvas, bg="white")
    chk_canvas.create_window((0, 0), window=chk_inner, anchor="nw")

    def _on_chk_resize(event):
        chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))

    chk_inner.bind("<Configure>", _on_chk_resize)

    # ── Snapshot preview pane (resizable via sash) ────────────────────────────
    _PREVIEW_INIT_W = 280
    preview_outer = tk.Frame(case_content, bg="#f5f5f5", relief="sunken", bd=1)
    case_content.add(preview_outer, stretch="never", minsize=120,
                     width=_PREVIEW_INIT_W)
    tk.Label(preview_outer, text="Snapshot preview", bg="#f5f5f5",
             font=("Segoe UI", 8, "italic"), fg="#999999").pack(pady=(4, 0))
    preview_img_lbl  = tk.Label(preview_outer, bg="#f5f5f5",
                                text="(select a case)", fg="#bbbbbb",
                                font=("Segoe UI", 8))
    preview_img_lbl.pack(expand=True)
    preview_name_lbl = tk.Label(preview_outer, bg="#f5f5f5",
                                text="", fg="#666666",
                                font=("Segoe UI", 7), wraplength=0)
    preview_name_lbl.pack(pady=(2, 4))
    _preview_ref = [None, None]   # [0]=PhotoImage (GC guard), [1]=last snap path

    def _find_snapshot_pp(sf_path: str) -> str | None:
        """Return best matching processing snapshot PNG for sf_path."""
        get_out = _get_output_folder[0]
        if get_out is None:
            return None
        out_raw = get_out().strip()
        if not out_raw:
            return None
        out_dir = Path(out_raw)
        source  = pp_source_var.get()
        try:
            output_name, case, scenario = extract_case_scenario(sf_path)
        except Exception:
            return None
        snap_base = out_dir / "snapshots" / output_name / case / scenario
        if not snap_base.exists():
            return None
        pngs = sorted(snap_base.glob("*.png"))
        if not pngs:
            return None
        if source == "original":
            for suffix in ("__before", "__pwr_density"):
                cands = [p for p in pngs if suffix in p.stem and "after" not in p.stem]
                if cands:
                    return str(cands[0])
        else:
            cands = [p for p in pngs if "__after" in p.stem]
            if cands:
                return str(cands[0])
        return str(pngs[0])

    def _render_preview_pp(snap: str) -> None:
        """Load and display *snap* at the current pane width."""
        w = max(80, preview_outer.winfo_width() - 8)
        h = max(60, preview_outer.winfo_height() - 40)
        try:
            try:
                from PIL import Image as _PI, ImageTk as _PIT
                img = _PI.open(snap)
                img.thumbnail((w, h))
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

    # ── Merge group definitions ───────────────────────────────────────────
    MERGE_GROUPS = [
        ("blue",   "#2563eb"),  # output prefix: blue__merged__...
        ("red",    "#dc2626"),
        ("green",  "#16a34a"),
        ("orange", "#ea580c"),
        ("purple", "#9333ea"),
    ]
    _GROUP_COLORS = {name: color for name, color in MERGE_GROUPS}
    active_group_var = tk.StringVar(value=MERGE_GROUPS[0][0])   # currently active tool
    # case_group: {sf_path: group_name} — unkeyed = not in any group
    case_group: dict[str, str] = {}
    # widgets: {sf_path: indicator_label}  — updated on click for immediate visual
    _indicator_widgets: dict[str, tk.Label] = {}
    # restore from saved config
    _saved_case_groups: dict[str, str] = dict(pp_s.get("case_groups", {}))

    def _assign_group(sf_path: str) -> None:
        """Assign sf_path to the active group, or clear it if already that group."""
        active = active_group_var.get()
        if active == "_clear":
            case_group.pop(sf_path, None)
        elif case_group.get(sf_path) == active:
            case_group.pop(sf_path, None)  # toggle off
        else:
            case_group[sf_path] = active
        # Update indicator widget color
        ind = _indicator_widgets.get(sf_path)
        if ind is not None:
            grp = case_group.get(sf_path)
            ind.configure(bg=_GROUP_COLORS.get(grp, "#e5e7eb") if grp else "#e5e7eb",
                          text=grp[0].upper() if grp else "",
                          fg="white" if grp else "#e5e7eb")
        _update_preview_pp(sf_path)

    def _build_case_grid(cases_by_output: dict[str, list[Path]]) -> None:
        """One column per output_name: bold header row 0, cases stacked below."""
        for w in chk_inner.winfo_children():
            w.destroy()
        case_group.clear()
        _indicator_widgets.clear()
        if not cases_by_output:
            tk.Label(chk_inner, text="(no subfolders found)",
                     fg="#aaaaaa", bg="white").grid(
                row=0, column=0, sticky="w", padx=4)
            chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))
            return
        for col_idx, (output_name, subfolders) in enumerate(
                sorted(cases_by_output.items())):
            tk.Label(chk_inner, text=output_name, anchor="w", bg="white",
                     font=("Segoe UI", 9, "bold")).grid(
                row=0, column=col_idx, sticky="w", padx=(4, 12), pady=(2, 4))
            for row_idx, sf in enumerate(sorted(subfolders), start=1):
                sp = str(sf)
                # Restore saved assignment
                if sp in _saved_case_groups:
                    case_group[sp] = _saved_case_groups[sp]
                grp = case_group.get(sp)
                ind = tk.Label(
                    chk_inner,
                    text=grp[0].upper() if grp else "",
                    width=2, font=("Segoe UI", 7, "bold"),
                    bg=_GROUP_COLORS.get(grp, "#e5e7eb") if grp else "#e5e7eb",
                    fg="white",
                    relief="flat", cursor="hand2",
                )
                ind.grid(row=row_idx, column=col_idx,
                         sticky="w", padx=(4, 0), pady=1)
                name_lbl = tk.Label(
                    chk_inner, text=sf.name, anchor="w",
                    bg="white", cursor="hand2",
                )
                name_lbl.grid(row=row_idx, column=col_idx,
                              sticky="w", padx=(22, 12), pady=1)
                _indicator_widgets[sp] = ind
                for widget in (ind, name_lbl):
                    widget.bind("<Button-1>",
                                lambda _e, p=sp: _assign_group(p))
        chk_inner.update_idletasks()
        chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))

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
                            dirs.append(str(case_dir))
            if not dirs:
                load_case_status.set("  Post-smoothed folder exists but is empty.")
                return
        else:
            get_dirs = _get_tab1_dirs[0]
            if get_dirs is None:
                messagebox.showwarning("Not ready",
                                       "Tab 1 not yet initialised. Please wait.")
                return
            dirs = get_dirs()
            if not dirs:
                messagebox.showwarning(
                    "No directories",
                    "Please add at least one folder in the Tab 1 directory list.")
                return

        cases_by_output: dict[str, list[Path]] = {}
        n_total = 0
        for d in dirs:
            p = Path(d)
            if not p.is_dir():
                continue
            subs = sorted([s for s in p.iterdir() if s.is_dir()])
            if subs:
                cases_by_output[p.name] = subs
                n_total += len(subs)
        _build_case_grid(cases_by_output)
        if n_total:
            src_label = "post-smoothed" if source == "post_smooth" else "input"
            load_case_status.set(
                f"  {len(cases_by_output)} output(s), {n_total} case(s) found"
                f"  [{src_label}]")
        else:
            load_case_status.set("  No subfolders found in the given paths.")

    load_case_btn.configure(command=on_load_cases)

    def _sel_all():
        """Assign all visible cases to the active group."""
        active = active_group_var.get()
        if active == "_clear":
            for sp in list(_indicator_widgets):
                case_group.pop(sp, None)
                ind = _indicator_widgets.get(sp)
                if ind:
                    ind.configure(bg="#e5e7eb", text="", fg="#e5e7eb")
        else:
            for sp in _indicator_widgets:
                case_group[sp] = active
                ind = _indicator_widgets.get(sp)
                if ind:
                    ind.configure(bg=_GROUP_COLORS[active],
                                  text=active[0].upper(), fg="white")

    def _desel_all():
        """Remove all cases from all groups."""
        for sp in list(_indicator_widgets):
            case_group.pop(sp, None)
            ind = _indicator_widgets.get(sp)
            if ind:
                ind.configure(bg="#e5e7eb", text="", fg="#e5e7eb")

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
            btn.configure(relief=relief, bd=bd)

    for _gname, _gcol in MERGE_GROUPS:
        _btn = tk.Button(
            selall_frame,
            text=_gname.capitalize(),
            bg=_gcol, fg="white",
            font=("Segoe UI", 8, "bold"),
            relief="raised", bd=1, padx=6, pady=1,
            cursor="hand2",
            command=lambda n=_gname: _select_tool(n),
        )
        _btn.pack(side="left", padx=(0, 3))
        _tool_btns[_gname] = _btn

    # Separator + utility buttons
    tk.Label(selall_frame, text="|", fg="#cccccc").pack(side="left", padx=4)
    tk.Button(selall_frame, text="Assign all", width=9,
              command=_sel_all).pack(side="left", padx=(0, 3))
    tk.Button(selall_frame, text="Clear all",  width=9,
              command=_desel_all).pack(side="left", padx=(0, 3))
    _clear_btn = tk.Button(
        selall_frame, text="✕ Eraser", bg="#6b7280", fg="white",
        font=("Segoe UI", 8), relief="raised", bd=1, padx=6, pady=1,
        cursor="hand2",
        command=lambda: _select_tool("_clear"),
    )
    _clear_btn.pack(side="left", padx=(0, 3))
    _tool_btns["_clear"] = _clear_btn

    # Activate first tool by default
    _select_tool(MERGE_GROUPS[0][0])

    # ── Multiplication factor (only for source = original) ────────────────────
    mult_lframe = tk.LabelFrame(tab3, text="Multiplication factor", padx=8, pady=4)
    _mult_frame_row = tk.Frame(mult_lframe)
    _mult_frame_row.pack(fill="x")
    pp_mult_var = tk.StringVar(value=str(pp_s.get("mult_factor", "1.0")))
    pp_mult_label = tk.Label(_mult_frame_row, text="Factor:",
                             font=("Segoe UI", 9, "bold"))
    pp_mult_label.pack(side="left")
    pp_mult_entry = tk.Entry(_mult_frame_row, textvariable=pp_mult_var, width=10)
    pp_mult_entry.pack(side="left", padx=(8, 0))
    tk.Label(_mult_frame_row, text="(applied to power density and total power)",
             fg="#64748b").pack(side="left", padx=(8, 0))

    # ── Merge arrays ───────────────────────────────────────────────────
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

    # ── Pack order: src → cases → [mult] → snaps → run ───────────────────────
    # mult_lframe visibility is managed reactively below.
    snap_lframe.pack(fill="x", padx=10, pady=(4, 2))
    btn_frame.pack(pady=(4, 10))

    _mult_packed = [False]   # tracks current pack state

    def _update_mult_visibility(*_):
        is_orig = pp_source_var.get() == "original"
        if is_orig and not _mult_packed[0]:
            # Insert mult_lframe before snap_lframe: pack_forget later widgets,
            # pack mult, then re-pack later widgets in order.
            snap_lframe.pack_forget()
            btn_frame.pack_forget()
            mult_lframe.pack(fill="x", padx=10, pady=(0, 4))
            snap_lframe.pack(fill="x", padx=10, pady=(4, 2))
            btn_frame.pack(pady=(4, 10))
            _mult_packed[0] = True
        elif not is_orig and _mult_packed[0]:
            mult_lframe.pack_forget()
            _mult_packed[0] = False

    pp_source_var.trace_add("write", _update_mult_visibility)
    _update_mult_visibility()   # apply on first render

    # ── Also auto-update pattern default when source changes ──────────────────
    _PATTERNS = {
        "original":   "smoothed_results_*.vtp",
        "post_smooth": "post_smooth__*.vtp",
    }
    _last_auto_pattern: list[str] = [pp_pattern_var.get()]

    def _update_pattern_default(*_):
        src = pp_source_var.get()
        default = _PATTERNS.get(src, "smoothed_results_*.vtp")
        # Only auto-update if the entry still holds the other mode's default
        current = pp_pattern_var.get()
        if current in _PATTERNS.values():
            pp_pattern_var.set(default)
            _last_auto_pattern[0] = default

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
        return {
            "groups":          groups,
            "pattern":         pp_pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":     pp_filter_var.get().strip(),
            "mult_factor":     mult,
            "apply_mult":      (source == "original"),
            "merge_pd":        pp_merge_pd_var.get(),
            "merge_pwr":       pp_merge_pwr_var.get(),
            "save_snapshots":  pp_save_snaps_var.get(),
            "snap_pwr_density": pp_snap_pd_var.get(),
            "snap_total_pwr":  pp_snap_tp_var.get(),
            "pp_source":       source,
        }

    def get_pp_cfg_dict() -> dict:
        """Serialisable config for settings persistence."""
        return {
            "pp_source":        pp_source_var.get(),
            "pattern":          pp_pattern_var.get(),
            "name_filter":      pp_filter_var.get(),
            "mult_factor":      pp_mult_var.get(),
            "merge_pd":         pp_merge_pd_var.get(),
            "merge_pwr":        pp_merge_pwr_var.get(),
            "save_snapshots":   pp_save_snaps_var.get(),
            "snap_pwr_density": pp_snap_pd_var.get(),
            "snap_total_pwr":   pp_snap_tp_var.get(),
            "case_groups":      dict(case_group),   # {path: group_name}
        }

    def apply_pp_cfg(pp: dict) -> None:
        if not pp:
            return
        pp_source_var.set(pp.get("pp_source", "original"))
        pp_pattern_var.set(pp.get("pattern", "smoothed_results_*.vtp"))
        pp_filter_var.set(pp.get("name_filter", ""))
        pp_mult_var.set(str(pp.get("mult_factor", "1.0")))
        pp_save_snaps_var.set(bool(pp.get("save_snapshots", False)))
        pp_merge_pd_var.set(bool(pp.get("merge_pd",  True)))
        pp_merge_pwr_var.set(bool(pp.get("merge_pwr", True)))
        pp_snap_pd_var.set(bool(pp.get("snap_pwr_density", True)))
        pp_snap_tp_var.set(bool(pp.get("snap_total_pwr", False)))
        _saved_case_groups.clear()
        _saved_case_groups.update(pp.get("case_groups", {}))
        # Re-apply saved assignments to already-loaded indicator widgets
        for sp, ind in _indicator_widgets.items():
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
        "_on_load_cases":     on_load_cases,
        "case_group":         case_group,
        "pp_source_var":      pp_source_var,
    }
