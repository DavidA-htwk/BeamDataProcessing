"""
modules/gui_tab2.py
-------------------
Builder for the Coordinate Transform tab (Tab 2) of the main GUI.

Call build_transform_tab(tab2, settings) to populate the frame and receive a
state dict with all Tkinter variables needed by the run callbacks.
"""

from __future__ import annotations

from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from modules.core.path_utils import extract_case_scenario

from modules.transform import transform_reference_frame as _trf


def build_transform_tab(tab2: tk.Frame, settings: dict) -> dict:
    """Populate *tab2* with all Coordinate Transform widgets.

    Returns a dict of tk.Vars and helpers needed by run callbacks.
    """
    xfm_s = settings.get("transform", {})

    # ── Preset selector ───────────────────────────────────────────────────────
    _first_preset = list(_trf.TRANSFORM_PRESETS.keys())[0]
    _saved_preset = xfm_s.get("preset", _first_preset)
    if _saved_preset not in _trf.TRANSFORM_PRESETS:
        _saved_preset = _first_preset
    xfm_preset_var = tk.StringVar(value=_saved_preset)

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

    on_preset_selected()

    _presets_row0 = ["DNB → Tokamak",  "HNB1 → Tokamak", "HNB2 → Tokamak", "HNB3 → Tokamak"]
    _presets_row1 = ["Tokamak → DNB",  "Tokamak → HNB1", "Tokamak → HNB2", "Tokamak → HNB3"]
    _presets_row2 = ["No Transformation"]

    preset_lframe = tk.LabelFrame(tab2, text="Coordinate Transform Preset", padx=8, pady=6)
    preset_lframe.pack(fill="x", padx=10, pady=(8, 4))

    _radio_frame = tk.Frame(preset_lframe)
    _radio_frame.grid(row=0, column=0, sticky="nw", padx=(0, 8))

    for row_idx, (label, presets) in enumerate([
        ("→ Tokamak:", _presets_row0),
        ("Tokamak →:", _presets_row1),
        ("Other:",     _presets_row2),
    ]):
        tk.Label(_radio_frame, text=label, fg="#444444", width=10, anchor="e").grid(
            row=row_idx, column=0, sticky="e", padx=(0, 6))
        for col, name in enumerate(presets):
            tk.Radiobutton(_radio_frame, text=name, variable=xfm_preset_var, value=name,
                           command=on_preset_selected).grid(
                row=row_idx, column=col + 1, sticky="w", padx=(0, 10))

    tk.Label(_radio_frame, text="", width=10).grid(row=3, column=0)
    tk.Label(_radio_frame, textvariable=xfm_summary_var,
             fg="#555555", font=("Consolas", 8), anchor="w").grid(
        row=3, column=1, columnspan=4, sticky="w", pady=(4, 0))

    # Coordinate image
    _coord_img_path = str(Path(__file__).resolve().parent / "coordinates.png")
    _coord_photo    = None
    try:
        from PIL import Image as _PILImage, ImageTk as _PILImageTk
        _pil     = _PILImage.open(_coord_img_path)
        _th      = 70
        _tw      = int(_th * _pil.width / _pil.height)
        _pil     = _pil.resize((_tw, _th), _PILImage.LANCZOS)
        _coord_photo = _PILImageTk.PhotoImage(_pil)
    except Exception:
        try:
            _raw         = tk.PhotoImage(file=_coord_img_path)
            _factor      = max(1, _raw.height() // 70)
            _coord_photo = _raw.subsample(_factor, _factor)
        except Exception:
            _coord_photo = None

    if _coord_photo is not None:
        _img_lbl        = tk.Label(preset_lframe, image=_coord_photo)
        _img_lbl.image  = _coord_photo
        _img_lbl.grid(row=0, column=1, padx=(16, 4), pady=2, sticky="ns")

    # ── Unit selectors ────────────────────────────────────────────────────────
    for pack_frame, label_text, var, default in [
        (tab2, "Input coordinate unit:",  xfm_unit_var,
         xfm_s.get("unit", "m")),
    ]:
        uf = tk.Frame(pack_frame)
        uf.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(uf, text=label_text, anchor="w", width=22).pack(side="left")
        for ul in ("mm", "m"):
            tk.Radiobutton(uf, text=ul, variable=xfm_unit_var, value=ul).pack(side="left", padx=4)

    out_unit_frame  = tk.Frame(tab2)
    out_unit_frame.pack(fill="x", padx=10, pady=(2, 0))
    xfm_out_unit_var = tk.StringVar(value=xfm_s.get("output_unit", xfm_s.get("unit", "m")))
    tk.Label(out_unit_frame, text="Output coordinate unit:", anchor="w", width=22).pack(side="left")
    for ul in ("mm", "m"):
        tk.Radiobutton(out_unit_frame, text=ul, variable=xfm_out_unit_var, value=ul).pack(
            side="left", padx=4)
    tk.Label(out_unit_frame, text="(no conversion = same as input)",
             fg="#888888").pack(side="left", padx=(8, 0))

    # ── File selection ────────────────────────────────────────────────────────
    xfm_opt_frame = tk.Frame(tab2)
    xfm_opt_frame.pack(fill="x", padx=10, pady=(8, 0))
    tk.Label(xfm_opt_frame, text="Glob pattern:", anchor="w").grid(row=0, column=0, sticky="w")
    xfm_pattern_var = tk.StringVar(value=xfm_s.get("pattern", "smoothed_results_*.vtp"))
    tk.Entry(xfm_opt_frame, textvariable=xfm_pattern_var, width=40).grid(
        row=0, column=1, sticky="w", padx=(6, 20))
    tk.Label(xfm_opt_frame, text="Name filter (comma-separated):", anchor="w").grid(
        row=0, column=2, sticky="w")
    xfm_filter_var = tk.StringVar(value=xfm_s.get("name_filter", ""))
    tk.Entry(xfm_opt_frame, textvariable=xfm_filter_var, width=40).grid(
        row=0, column=3, sticky="w", padx=6)

    # ── Case browser ──────────────────────────────────────────────────────────
    case_lframe = tk.LabelFrame(tab2, text="Cases", padx=8, pady=4)
    case_lframe.pack(fill="both", expand=True, padx=10, pady=(8, 4))

    # Injected after build — set by Data_handling.py so Load Cases reads Tab 1.
    _get_tab1_dirs:    list = [None]   # [0] = callable() → list[str]
    _get_output_folder: list = [None]  # [0] = callable() → str  (smoothed-source mode)
    _get_mult_factor_p: list = [None]  # [0] = callable() → str  (processing mult)

    # Source toggle — original / post-smoothed / post-processed
    src_frame = tk.Frame(case_lframe)
    src_frame.pack(fill="x", pady=(0, 4))
    tk.Label(src_frame, text="Source:", anchor="w", width=8).pack(side="left")
    xfm_source_var = tk.StringVar(value=xfm_s.get("xfm_source", "original"))
    tk.Radiobutton(src_frame, text="Original input folders",
                   variable=xfm_source_var, value="original").pack(side="left", padx=(0, 10))
    tk.Radiobutton(src_frame,
                   text="Post-smooth VTPs (output/post_smoothed/)",
                   variable=xfm_source_var, value="smoothed").pack(side="left", padx=(0, 10))
    tk.Radiobutton(src_frame,
                   text="Post-processed VTPs (output/post_processed/)",
                   variable=xfm_source_var, value="post_processed").pack(side="left")

    # Auto-update pattern default when source changes
    _XFM_PATTERNS = {
        "original":       "smoothed_results_*.vtp",
        "smoothed":       "post_smooth__*.vtp",
        "post_processed": "merged__*.vtp",
    }

    def _update_xfm_pattern(*_):
        src = xfm_source_var.get()
        default = _XFM_PATTERNS.get(src)
        if default and xfm_pattern_var.get() in _XFM_PATTERNS.values():
            xfm_pattern_var.set(default)

    xfm_source_var.trace_add("write", _update_xfm_pattern)

    # Load Cases button + status
    load_case_btn_frame = tk.Frame(case_lframe)
    load_case_btn_frame.pack(fill="x", pady=(0, 2))
    load_case_btn = tk.Button(
        load_case_btn_frame, text="Load Cases", width=14,
        bg="#005f73", fg="white", font=("Segoe UI", 10, "bold"),
    )
    load_case_btn.pack(side="left")
    load_case_status = tk.StringVar(value="  (uses Tab 1 directories — click to populate)")
    tk.Label(load_case_btn_frame, textvariable=load_case_status,
             fg="#555555", anchor="w").pack(side="left", padx=6)

    # Select-all / deselect-all buttons
    selall_frame = tk.Frame(case_lframe)
    selall_frame.pack(fill="x", pady=(0, 2))

    # Scrollable checkbox area (horizontal + vertical) + snapshot preview pane
    case_content = tk.PanedWindow(case_lframe, orient="horizontal",
                                    sashwidth=5, sashrelief="raised", bg="#d0d0d0")
    case_content.pack(fill="both", expand=True)

    chk_canvas_frame = tk.Frame(case_content)
    case_content.add(chk_canvas_frame, stretch="always", minsize=200)
    chk_canvas = tk.Canvas(chk_canvas_frame, height=160, bg="white",
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
    _chk_window = chk_canvas.create_window((0, 0), window=chk_inner, anchor="nw")

    def _on_chk_resize(event):
        chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))

    chk_inner.bind("<Configure>", _on_chk_resize)

    def _on_mousewheel(event):
        chk_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    chk_canvas.bind("<MouseWheel>", _on_mousewheel)
    chk_inner.bind("<MouseWheel>",  _on_mousewheel)

    def _bind_mousewheel_to_children(widget):
        widget.bind("<MouseWheel>", _on_mousewheel)
        for child in widget.winfo_children():
            _bind_mousewheel_to_children(child)

    # ── Snapshot preview pane (resizable via sash) ────────────────────────────
    _PREVIEW_INIT_W = 280
    preview_outer = tk.Frame(case_content, bg="#f5f5f5", relief="sunken", bd=1)
    case_content.add(preview_outer, stretch="never", minsize=120,
                     width=_PREVIEW_INIT_W)
    tk.Label(preview_outer, text="Snapshot preview", bg="#f5f5f5",
             font=("Segoe UI", 8, "italic"), fg="#999999").pack(pady=(4, 0))
    preview_img_lbl  = tk.Label(preview_outer, bg="#f5f5f5",
                                text="(select a case)", fg="#bbbbbb",
                                font=("Segoe UI", 8), cursor="hand2")
    preview_img_lbl.pack(expand=True)
    preview_name_lbl = tk.Label(preview_outer, bg="#f5f5f5",
                                text="", fg="#666666",
                                font=("Segoe UI", 7), wraplength=0)
    preview_name_lbl.pack(pady=(2, 4))

    # ── Zoom slider ───────────────────────────────────────────────────
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
    _pan_offset   = [0.0, 0.0]     # [dx, dy] pan in original image pixels
    _drag_start   = [None, None]   # screen pos at ButtonPress-1
    _drag_pan_st  = [0.0, 0.0]    # _pan_offset snapshot at drag start
    _img_orig_sz  = [None]         # (iw, ih) of last loaded image

    def _find_snapshot_xfm(sf_path: str) -> str | None:
        """Return best matching snapshot PNG for sf_path given current source."""
        get_out = _get_output_folder[0]
        if get_out is None:
            return None
        out_raw = get_out().strip()
        if not out_raw:
            return None
        out_dir = Path(out_raw)
        source  = xfm_source_var.get()
        try:
            output_name, case, scenario = extract_case_scenario(sf_path)
        except Exception:
            return None
        if source == "post_processed":
            snap_base = out_dir / "post_processed_snapshots" / output_name
        else:
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

    def _render_preview_xfm(snap: str) -> None:
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

    def _on_zoom_xfm(*_):
        _pan_offset[0] = 0.0
        _pan_offset[1] = 0.0
        _zoom_val_lbl.configure(text=f"{zoom_var.get():.1f}×")
        if _preview_ref[1]:
            _render_preview_xfm(_preview_ref[1])

    zoom_var.trace_add("write", _on_zoom_xfm)

    def _start_drag_xfm(event):
        _drag_start[0]  = event.x
        _drag_start[1]  = event.y
        _drag_pan_st[0] = _pan_offset[0]
        _drag_pan_st[1] = _pan_offset[1]

    def _do_drag_xfm(event):
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
            _render_preview_xfm(_preview_ref[1])

    preview_img_lbl.bind("<ButtonPress-1>", _start_drag_xfm)
    preview_img_lbl.bind("<B1-Motion>",     _do_drag_xfm)

    def _update_preview_xfm(sf_path: str) -> None:
        snap = _find_snapshot_xfm(sf_path)
        _preview_ref[1] = snap
        if snap is None:
            preview_img_lbl.configure(image="", text="(no snapshot found)", fg="#bbbbbb")
            preview_name_lbl.configure(text="")
            _preview_ref[0] = None
            return
        _render_preview_xfm(snap)

    def _on_preview_resize_xfm(event=None):
        if _preview_ref[1]:
            _render_preview_xfm(_preview_ref[1])

    preview_outer.bind("<Configure>", _on_preview_resize_xfm)

    # case_checks: {subfolder_path_str: BooleanVar}
    case_checks: dict[str, tk.BooleanVar] = {}
    # Restore saved selection
    _saved_sel: set[str] = set(xfm_s.get("case_selection", []))
    _last_click_path: list[str | None] = [None]   # for Shift+click range select
    _ordered_paths:   list[str]        = []        # display order, rebuilt each load

    def _build_case_grid(cases_by_output: dict[str, list[Path]]) -> None:
        """Rebuild checkbox grid from cases_by_output = {output_name: [subfolder, ...]}.

        Layout: one column per output_name.
          Row 0     — bold header label (output_name)
          Row 1..N  — one checkbox per subfolder, sorted alphabetically
        """
        for w in chk_inner.winfo_children():
            w.destroy()
        case_checks.clear()
        _ordered_paths.clear()

        if not cases_by_output:
            tk.Label(chk_inner, text="(no subfolders found)",
                     fg="#aaaaaa", bg="white").grid(row=0, column=0, sticky="w", padx=4)
            chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))
            return

        for col_idx, (output_name, subfolders) in enumerate(
                sorted(cases_by_output.items())):
            # Header
            tk.Label(chk_inner, text=output_name, anchor="w", bg="white",
                     font=("Segoe UI", 9, "bold")).grid(
                row=0, column=col_idx, sticky="w", padx=(4, 12), pady=(2, 4))
            # One checkbox per subfolder stacked vertically
            for row_idx, sf in enumerate(sorted(subfolders), start=1):
                sp  = str(sf)
                var = tk.BooleanVar(value=(sp in _saved_sel))
                _ordered_paths.append(sp)
                def _cmd(p=sp, v=var):
                    _last_click_path[0] = p
                    if v.get():
                        _update_preview_xfm(p)
                def _shift_cmd(event, p=sp, v=var):
                    """Shift+click: check all cases between last click and this one."""
                    last = _last_click_path[0]
                    if last and last in case_checks and p in _ordered_paths and last in _ordered_paths:
                        lo = min(_ordered_paths.index(last), _ordered_paths.index(p))
                        hi = max(_ordered_paths.index(last), _ordered_paths.index(p))
                        for pp in _ordered_paths[lo:hi + 1]:
                            if pp in case_checks:
                                case_checks[pp].set(True)
                    else:
                        v.set(not v.get())
                    _last_click_path[0] = p
                    _update_preview_xfm(p)
                    return "break"  # suppress default Checkbutton toggle
                cb  = tk.Checkbutton(chk_inner, text=sf.name, variable=var,
                                     anchor="w", bg="white", command=_cmd)
                cb.bind("<Shift-Button-1>", _shift_cmd)
                cb.grid(row=row_idx, column=col_idx, sticky="w", padx=(4, 12), pady=1)
                case_checks[sp] = var

        chk_inner.update_idletasks()
        chk_canvas.configure(scrollregion=chk_canvas.bbox("all"))
        _bind_mousewheel_to_children(chk_inner)

    def on_load_cases():
        source = xfm_source_var.get()
        dirs: list[str] = []

        if source == "smoothed":
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
                    "Run Processing with 'Save VTP' enabled first.",
                )
                return
            # Enumerate 2nd-level subdirs: post_smoothed/{output_name}/{case_dir}
            for output_dir in sorted(smooth_root.iterdir()):
                if output_dir.is_dir():
                    for case_dir in sorted(output_dir.iterdir()):
                        if case_dir.is_dir():
                            dirs.append(str(case_dir))
            if not dirs:
                load_case_status.set("  Post-smoothed folder exists but is empty.")
                return
        elif source == "post_processed":
            get_out = _get_output_folder[0]
            if get_out is None:
                messagebox.showwarning("Not ready", "Output folder not yet available.")
                return
            out_raw = get_out().strip()
            pp_root = (Path(out_raw) / "post_processed") if out_raw else None
            if not pp_root or not pp_root.exists():
                load_case_status.set("  Post-processed folder not found.")
                messagebox.showwarning(
                    "Post-processed folder not found",
                    f"The post-processed VTP folder does not exist yet:\n\n"
                    f"  {pp_root}\n\n"
                    "Run Post Processing first.",
                )
                return
            for output_dir in sorted(pp_root.iterdir()):
                if output_dir.is_dir():
                    # Enumerate group subdirs: post_processed/{output_name}/{group_name}/
                    for group_dir in sorted(output_dir.iterdir()):
                        if group_dir.is_dir():
                            dirs.append(str(group_dir))
            if not dirs:
                load_case_status.set("  Post-processed folder exists but is empty.")
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
            name = p.name
            if source == "post_processed":
                # Structure: post_processed/{output_name}/{group_name}/merged__*.vtp
                # Use output_name as row label, group_name as checkbox.
                row_label = p.parent.name
                cases_by_output.setdefault(row_label, []).append(p)
                n_total += 1
            else:
                subs = sorted([s for s in p.iterdir() if s.is_dir()])
                if subs:
                    cases_by_output[name] = subs
                    n_total += len(subs)
        _build_case_grid(cases_by_output)
        if n_total:
            src_label = ("post-smoothed" if source == "smoothed"
                         else "post-processed" if source == "post_processed"
                         else "input")
            load_case_status.set(
                f"  {len(cases_by_output)} output(s), {n_total} case(s) found  [{src_label}]")
        else:
            load_case_status.set("  No subfolders found in the given paths.")

    load_case_btn.configure(command=on_load_cases)

    # Select-all / deselect-all
    def _sel_all():
        for var in case_checks.values():
            var.set(True)

    def _desel_all():
        for var in case_checks.values():
            var.set(False)

    tk.Button(selall_frame, text="Select all",   width=10,
              command=_sel_all).pack(side="left", padx=(0, 4))
    tk.Button(selall_frame, text="Deselect all", width=10,
              command=_desel_all).pack(side="left")

    # Auto-load on startup will happen after Tab 1 is wired in Data_handling.py
    _saved_sel: set[str] = set(xfm_s.get("case_selection", []))

    # ── Export options ────────────────────────────────────────────────────────
    export_lframe = tk.LabelFrame(tab2, text="Properties to export", padx=8, pady=4)
    export_lframe.pack(fill="x", padx=10, pady=(8, 4))
    _exp_row = tk.Frame(export_lframe)
    _exp_row.pack(fill="x")
    xfm_export_geom  = tk.BooleanVar(value=bool(xfm_s.get("export_geom",  True)))
    xfm_export_area  = tk.BooleanVar(value=bool(xfm_s.get("export_area",  True)))
    xfm_export_power = tk.BooleanVar(value=bool(xfm_s.get("export_power", True)))
    xfm_export_pload = tk.BooleanVar(value=bool(xfm_s.get("export_pload", True)))
    for text, var, pad in [
        ("Geometry (X, Y, Z)",           xfm_export_geom,  12),
        ("Cell area",                    xfm_export_area,  12),
        ("Power (Deposited_Power_W)",    xfm_export_power, 12),
        ("Power load (Power_Density_W_m2)", xfm_export_pload, 0),
    ]:
        tk.Checkbutton(_exp_row, text=text, variable=var).pack(side="left", padx=(0, pad))

    xfm_misc_frame = tk.Frame(tab2)
    xfm_misc_frame.pack(fill="x", padx=10, pady=(2, 4))
    xfm_mult_label = tk.Label(xfm_misc_frame, text="Multiplication factor:",
                              font=("Segoe UI", 9, "bold"))
    xfm_mult_label.pack(side="left")
    xfm_mult_var = tk.StringVar(value=str(xfm_s.get("mult_factor_t", xfm_s.get("mult", "1.0"))))
    xfm_mult_entry = tk.Entry(xfm_misc_frame, textvariable=xfm_mult_var, width=10)
    xfm_mult_entry.pack(side="left", padx=(8, 0))
    xfm_mult_desc = tk.Label(xfm_misc_frame, text="(applied to power & power load)",
                             fg="#64748b")
    xfm_mult_desc.pack(side="left", padx=(8, 4))
    xfm_mult_note = tk.Label(xfm_misc_frame, text="", fg="#b45309",
                             font=("Segoe UI", 8, "italic"))
    xfm_mult_note.pack(side="left", padx=(0, 12))
    xfm_ignore_zeros = tk.BooleanVar(value=bool(xfm_s.get("ignore_zeros", False)))
    tk.Checkbutton(xfm_misc_frame, text="Ignore zero-valued rows",
                   variable=xfm_ignore_zeros).pack(side="left")

    def _update_xfm_mult_state(*_):
        src = xfm_source_var.get()
        if src == "original":
            xfm_mult_var.set("1.0")
            xfm_mult_entry.configure(state="normal")
            xfm_mult_label.configure(fg="black")
            xfm_mult_desc.configure(fg="#64748b")
            xfm_mult_note.configure(text="")
        else:
            # Show the mult_factor_p (processing factor) from live comp_widgets
            get_p = _get_mult_factor_p[0]
            if get_p is not None:
                xfm_mult_var.set(get_p())
            xfm_mult_entry.configure(state="disabled")
            xfm_mult_label.configure(fg="#999999")
            xfm_mult_desc.configure(fg="#bbbbbb")
            xfm_mult_note.configure(
                text="(← mult_factor_p applied in Processing)")

    xfm_source_var.trace_add("write", _update_xfm_mult_state)
    _update_xfm_mult_state()

    # ── Run Transform button ──────────────────────────────────────────────────
    tab2_btn_frame = tk.Frame(tab2)
    tab2_btn_frame.pack(pady=(8, 10))
    tab2_run_btn = tk.Button(
        tab2_btn_frame, text="Run Transform", width=16, bg="#0060c0", fg="white",
        font=("Segoe UI", 10, "bold"),
    )
    tab2_run_btn.pack(side="left", padx=6)

    def get_selected_dirs() -> list[str]:
        """Return list of checked subfolder paths."""
        return [p for p, var in case_checks.items() if var.get()]

    def get_transform_params() -> dict | None:
        """Validate and collect transform params.  Returns None if invalid."""
        selected = get_selected_dirs()
        if not selected:
            messagebox.showwarning("No cases selected",
                                   "Select at least one case subfolder, or load cases first.")
            return None
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
        unit        = xfm_unit_var.get()
        output_unit = xfm_out_unit_var.get()
        unit_to_m   = {"m": 1.0, "mm": 0.001}
        coord_scale = unit_to_m[unit] / unit_to_m[output_unit]
        return {
            "angle_deg":    angle, "dx": dx, "dy": dy, "dz": dz,
            "unit":         unit,  "output_unit":  output_unit,
            "coord_scale":  coord_scale,
            "pattern":      xfm_pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":  xfm_filter_var.get().strip(),
            "export_geom":  exp_geom, "export_area":  exp_area,
            "export_power": exp_power, "export_pload": exp_pload,
            "mult":         mult, "ignore_zeros": xfm_ignore_zeros.get(),
            "_selected_dirs": selected,
        }

    def get_xfm_cfg_dict() -> dict:
        src = xfm_source_var.get()
        # Save mult_factor_t only when source=original (user-editable).
        # When non-original, save whatever is currently displayed (mult_factor_p).
        return {
            "xfm_source":     src,
            "preset":         xfm_preset_var.get(),
            "unit":           xfm_unit_var.get(),
            "output_unit":    xfm_out_unit_var.get(),
            "angle_deg":      xfm_angle_var.get(),
            "dx":             xfm_dx_var.get(),
            "dy":             xfm_dy_var.get(),
            "dz":             xfm_dz_var.get(),
            "pattern":        xfm_pattern_var.get(),
            "name_filter":    xfm_filter_var.get(),
            "export_geom":    xfm_export_geom.get(),
            "export_area":    xfm_export_area.get(),
            "export_power":   xfm_export_power.get(),
            "export_pload":   xfm_export_pload.get(),
            "mult_factor_t":  xfm_mult_var.get(),
            "mult":           xfm_mult_var.get(),  # backward compat
            "ignore_zeros":   xfm_ignore_zeros.get(),
            "case_selection": get_selected_dirs(),
        }

    def apply_xfm_cfg(xfm: dict) -> None:
        if not xfm:
            return
        xfm_source_var.set(xfm.get("xfm_source", "original"))
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
        xfm_export_geom.set(bool(xfm.get("export_geom",  True)))
        xfm_export_area.set(bool(xfm.get("export_area",  True)))
        xfm_export_power.set(bool(xfm.get("export_power", True)))
        xfm_export_pload.set(bool(xfm.get("export_pload", True)))
        xfm_mult_var.set(str(xfm.get("mult_factor_t", xfm.get("mult", "1.0"))))
        xfm_ignore_zeros.set(bool(xfm.get("ignore_zeros", False)))
        _saved_sel.clear()
        _saved_sel.update(xfm.get("case_selection", []))
        # Re-apply saved selection to already-loaded checkboxes (if any)
        for p, var in case_checks.items():
            var.set(p in _saved_sel)

    return {
        "tab2_run_btn":        tab2_run_btn,
        "xfm_preset_var":      xfm_preset_var,
        "xfm_unit_var":        xfm_unit_var,
        "xfm_out_unit_var":    xfm_out_unit_var,
        "xfm_angle_var":       xfm_angle_var,
        "xfm_dx_var":          xfm_dx_var,
        "xfm_dy_var":          xfm_dy_var,
        "xfm_dz_var":          xfm_dz_var,
        "xfm_pattern_var":     xfm_pattern_var,
        "xfm_filter_var":      xfm_filter_var,
        "xfm_export_geom":     xfm_export_geom,
        "xfm_export_area":     xfm_export_area,
        "xfm_export_power":    xfm_export_power,
        "xfm_export_pload":    xfm_export_pload,
        "xfm_mult_var":        xfm_mult_var,
        "xfm_ignore_zeros":    xfm_ignore_zeros,
        "get_transform_params": get_transform_params,
        "get_xfm_cfg_dict":    get_xfm_cfg_dict,
        "apply_xfm_cfg":       apply_xfm_cfg,
        "_get_tab1_dirs":      _get_tab1_dirs,
        "_get_output_folder":  _get_output_folder,
        "_get_mult_factor_p":  _get_mult_factor_p,
        "_on_load_cases":      on_load_cases,       # caller can trigger after wiring
        "_saved_sel":          _saved_sel,
        "case_checks":         case_checks,
        "xfm_source_var":      xfm_source_var,
    }
