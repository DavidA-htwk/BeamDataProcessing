"""
Data_handling.py
----------------
Entry point.  Launches the GUI and wires together the modules.

All heavy logic lives in the modules/ package:
  settings.py      - constants & JSON config persistence
  vtk_io.py        - VTP read/write, find_max, find_total, scaling
  path_utils.py    - extract_case_scenario
  smoothing.py     - precompute_smooth_geometry, apply_edge_smooth
  workers.py       - thread/process worker functions
  pipeline.py      - run_processing, run_transform
  gui_tab1.py      - Processing tab builder
  gui_tab2.py      - Coordinate Transform tab builder
  snapshot_max.py  - offscreen VTK snapshot rendering
  generate_report  - per-cell CSV extraction
  transform_reference_frame - coordinate transforms

Run with:
    python  Data_handling.py
"""

import sys
import threading
import time
import ctypes
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import vtk          # noqa: F401 - verify VTK is available before GUI opens
    import numpy        # noqa: F401
except ImportError:
    sys.exit(
        "ERROR: vtk and numpy are required.\n"
        "  pip install vtk numpy"
    )

from modules.core.settings import (
    load_settings, save_settings, _safe_float, SMOOTH_PROXIMITY_RADIUS,
)
from modules.processing.pipeline import run_processing, run_transform
from modules.processing.post_pipeline import run_post_processing
from modules.gui.gui_tab1 import build_processing_tab
from modules.gui.gui_tab2 import build_transform_tab
from modules.gui.gui_tab3 import build_post_processing_tab


# -- GUI -----------------------------------------------------------------------

def run_gui() -> None:
    settings = load_settings()

    # Must be called BEFORE tk.Tk() so Windows associates the correct
    # AppUserModelID with the process for taskbar icon grouping.
    base_dir = Path(__file__).resolve().parent
    png_icon = base_dir / "Beam.png"
    ico_icon = base_dir / "Beam.ico"
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ValueExtract.DataHandling"
            )
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()   # hide main window while splash is shown
    root.title("Beam Data Handling")
    root.resizable(True, True)

    # -- Splash screen ---------------------------------------------------------
    splash_path = base_dir / "Startup.png"
    if splash_path.exists():
        splash = tk.Toplevel(root)
        splash.overrideredirect(True)   # borderless
        splash.attributes("-topmost", True)
        try:
            splash_img = tk.PhotoImage(file=str(splash_path))
            img_w, img_h = splash_img.width(), splash_img.height()
            sw = splash.winfo_screenwidth()
            sh = splash.winfo_screenheight()
            x  = (sw - img_w) // 2
            y  = (sh - img_h) // 2
            splash.geometry(f"{img_w}x{img_h}+{x}+{y}")
            tk.Label(splash, image=splash_img, bd=0).pack()
            splash._img_ref = splash_img  # prevent GC
        except Exception:
            splash.destroy()
            splash = None

        def _close_splash():
            if splash and splash.winfo_exists():
                splash.destroy()
            root.deiconify()

        if splash:
            splash.bind("<Button-1>", lambda _e: _close_splash())
            root.after(2500, _close_splash)
    else:
        root.deiconify()

    # Apply custom app icon. On Windows use ICO (title bar + taskbar).
    # On other platforms fall back to PNG via iconphoto.
    try:
        if ico_icon.exists() and sys.platform.startswith("win"):
            root.iconbitmap(str(ico_icon))
        elif png_icon.exists():
            app_icon = tk.PhotoImage(file=str(png_icon))
            root.iconphoto(True, app_icon)
            root._app_icon_ref = app_icon  # prevent Tk image GC
    except Exception:
        # Keep GUI launch robust even if icon loading fails.
        pass

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=False, padx=10, pady=(10, 0))
    tab1 = tk.Frame(notebook)
    tab2 = tk.Frame(notebook)
    notebook.add(tab1, text="Processing")
    tab3 = tk.Frame(notebook)
    notebook.add(tab3, text="Post Processing")
    notebook.add(tab2, text="Coordinate Transform")

    # -- Log area (created early so log_fn can be passed to tab builders) ------
    log_outer = tk.Frame(root)
    log_outer.pack(fill="both", expand=True, padx=10, pady=(6, 4))

    # Three log panes, each with an inline stop button in the header
    def _make_log_pane(parent: tk.Frame, label: str) -> tk.Text:
        """Simple pane without stop button (used before stop events exist)."""
        col = tk.Frame(parent)
        col.pack(side="left", fill="both", expand=True, padx=(0, 4))
        tk.Label(col, text=label, anchor="w",
                 font=("Segoe UI", 8, "bold"), fg="#888888").pack(fill="x")
        inner = tk.Frame(col)
        inner.pack(fill="both", expand=True)
        sb_y = tk.Scrollbar(inner, orient="vertical")
        sb_y.pack(side="right", fill="y")
        sb_x = tk.Scrollbar(inner, orient="horizontal")
        sb_x.pack(side="bottom", fill="x")
        box = tk.Text(
            inner, width=55, height=10, state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 8),
            yscrollcommand=sb_y.set, xscrollcommand=sb_x.set, wrap="none",
        )
        box.pack(side="left", fill="both", expand=True)
        sb_y.config(command=box.yview)
        sb_x.config(command=box.xview)
        return box

    log_box_proc = _make_log_pane(log_outer, "Processing log")
    log_box_pp   = _make_log_pane(log_outer, "Post Processing log")
    log_box_xfm  = _make_log_pane(log_outer, "Transform log")

    _log_lines_proc: list[str] = []
    _log_lines_pp:   list[str] = []
    _log_lines_xfm:  list[str] = []
    _run_start_time: list = [None]

    def _append_to(box: tk.Text, lines: list, msg: str) -> None:
        box.configure(state="normal")
        box.insert("end", msg + "\n")
        box.see("end")
        box.configure(state="disabled")
        lines.append(msg)
        root.update_idletasks()

    def log_proc(msg: str) -> None:
        _append_to(log_box_proc, _log_lines_proc, msg)

    def log_pp(msg: str) -> None:
        _append_to(log_box_pp, _log_lines_pp, msg)

    def log_xfm(msg: str) -> None:
        _append_to(log_box_xfm, _log_lines_xfm, msg)

    # Unified log for backward-compat (used by tab builders etc.)
    def log(msg: str) -> None:
        log_proc(msg)

    # -- Build tabs ------------------------------------------------------------
    t1 = build_processing_tab(tab1, settings, log)
    t3 = build_post_processing_tab(tab3, settings)
    t2 = build_transform_tab(tab2, settings)

    # Unpack frequently-used widgets from tab-state dicts
    text_box          = t1["text_box"]
    output_folder_var = t1["output_folder_var"]
    comp_widgets      = t1["comp_widgets"]
    pending_comp_cfg  = t1["pending_comp_cfg"]
    proximity_var     = t1["proximity_var"]
    pattern_var       = t1["pattern_var"]
    filter_var        = t1["filter_var"]
    cfg_path_var      = t1["cfg_path_var"]
    tab1_run_btn      = t1["tab1_run_btn"]

    tab2_run_btn           = t2["tab2_run_btn"]
    get_transform_params   = t2["get_transform_params"]
    get_xfm_cfg_dict       = t2["get_xfm_cfg_dict"]
    apply_xfm_cfg          = t2["apply_xfm_cfg"]

    tab3_run_btn           = t3["tab3_run_btn"]
    get_pp_cfg             = t3["get_pp_cfg"]
    get_pp_cfg_dict        = t3["get_pp_cfg_dict"]
    apply_pp_cfg           = t3["apply_pp_cfg"]

    # Wire Tab 1 directory reader into Tab 2 and Tab 3 Load Cases buttons
    def _get_tab1_dirs_for_t2() -> list[str]:
        raw  = text_box.get("1.0", "end").strip()
        return [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]

    t2["_get_tab1_dirs"][0] = _get_tab1_dirs_for_t2
    t2["_get_output_folder"][0] = lambda: output_folder_var.get().strip()
    t3["_get_tab1_dirs"][0] = _get_tab1_dirs_for_t2
    t3["_get_output_folder"][0] = lambda: output_folder_var.get().strip()

    def _get_processing_mult_factor() -> str:
        """Return the mult_factor_p: the factor set in named Processing components.
        If all named components share the same value, return it.
        Falls back to transform.mult_factor_t then '1.0'."""
        comp_dict = t1["_get_comp_dict"]()
        named = {
            str(cfg.get("mult_factor", 1.0))
            for name, cfg in comp_dict.items()
            if name != "(all)"
        }
        if len(named) == 1:
            return named.pop()
        # Fall back to transform factor
        try:
            return str(float(t2["xfm_mult_var"].get()))
        except Exception:
            return "1.0"

    t2["_get_mult_factor_p"][0] = _get_processing_mult_factor
    t3["_get_mult_factor_p"][0] = _get_processing_mult_factor
    # Auto-populate cases on startup if saved selection exists
    if settings.get("transform", {}).get("case_selection"):
        t2["_on_load_cases"]()

    # -- Full-config helpers (used by config save/load in tab1) ----------------
    def _current_cfg() -> dict:
        raw  = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        return {
            "input_dirs":      dirs,
            "output_folder":   output_folder_var.get(),
            "pattern":         pattern_var.get() or "smoothed_results_*.vtp",
            "name_filter":     filter_var.get().strip(),
            "proximity_radius": _safe_float(proximity_var.get(), SMOOTH_PROXIMITY_RADIUS),
            "components":      t1["_get_comp_dict"](),
            "transform":       get_xfm_cfg_dict(),
            "post_processing": get_pp_cfg_dict(),
        }

    def _apply_cfg(loaded: dict) -> None:
        text_box.delete("1.0", "end")
        if loaded.get("input_dirs"):
            text_box.insert("1.0", "\n".join(loaded["input_dirs"]))
        pattern_var.set(loaded.get("pattern", "smoothed_results_*.vtp"))
        filter_var.set(loaded.get("name_filter", ""))
        proximity_var.set(str(loaded.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS)))
        out = loaded.get("output_folder", "")
        output_folder_var.set(out)
        t1["output_label_var"].set(out or "(script output/ folder)")
        pending_comp_cfg.clear()
        pending_comp_cfg.update(loaded.get("components", {}))
        # Also update any currently-displayed component rows so the checkboxes
        # reflect the loaded config immediately without needing "Load Geometry".
        for _cname, _w in comp_widgets.items():
            _cc = pending_comp_cfg.get(_cname, {})
            if not _cc:
                continue
            _w["snap_pd_var"].set(bool(_cc.get("save_power_density", True)))
            _w["snap_tp_var"].set(bool(_cc.get("save_total_power", False)))
            _w["save_vtp_var"].set(bool(_cc.get("save_smooth_vtp", False)))
            _w["min_pwr_var"].set(str(_cc.get("min_power_W", 0.0)))
            try:
                _w["smooth_var"].set(int(_cc.get("smooth_iterations", 1)))
            except Exception:
                pass
            _w["mult_var"].set(str(_cc.get("mult_factor", 1.0)))
        apply_xfm_cfg(loaded.get("transform", {}))
        apply_pp_cfg(loaded.get("post_processing", {}))

    # Wire config callbacks into tab1 so Save/Load buttons work
    t1["_xfm_cfg_fn"][0] = _current_cfg   # used by save button
    # Reuse slot for apply (load button calls _xfm_cfg_fn[0](loaded))
    # -> swap to apply when called with a dict arg
    _orig_get = _current_cfg

    def _cfg_dispatch(arg=None):
        if arg is None:
            return _orig_get()
        _apply_cfg(arg)

    t1["_xfm_cfg_fn"][0] = _cfg_dispatch

    # -- Independent stop events and counters ---------------------------------
    _stop_proc  = threading.Event()
    _stop_pp    = threading.Event()
    _stop_xfm   = threading.Event()
    _active_proc = [0]
    _active_pp   = [0]
    _active_xfm  = [0]

    # Stop buttons live inline in the log column headers
    def _add_stop_btn(log_box: tk.Text, stop_event: threading.Event) -> tk.Button:
        """Insert a stop button into the header row above log_box."""
        col_frame = log_box.master.master  # Text → inner Frame → col Frame
        hdr = tk.Frame(col_frame)
        hdr.pack(before=log_box.master, fill="x")
        # Move the existing label into the new header frame
        for child in list(col_frame.children.values()):
            if isinstance(child, tk.Label):
                child.pack_forget()
                child.pack(in_=hdr, side="left", fill="x", expand=True)
                break
        btn = tk.Button(
            hdr, text="■ Stop", width=7, state="disabled",
            bg="#b91c1c", fg="white", font=("Segoe UI", 8, "bold"),
            padx=2, pady=0,
        )
        btn.configure(command=lambda e=stop_event, b=btn: (
            e.set(), b.configure(state="disabled", text="Stopping…")))
        btn.pack(side="right")
        return btn

    stop_btn_proc = _add_stop_btn(log_box_proc, _stop_proc)
    stop_btn_pp   = _add_stop_btn(log_box_pp,   _stop_pp)
    stop_btn_xfm  = _add_stop_btn(log_box_xfm,  _stop_xfm)

    _all_run_btns = [tab1_run_btn, tab3_run_btn, tab2_run_btn]

    def _set_busy(mode: str = "proc") -> None:
        """mode: 'proc' | 'pp' | 'xfm' — clear the pane and arm stop button."""
        _run_start_time[0] = time.strftime("%Y-%m-%d_%H-%M-%S")
        if mode == "proc":
            _stop_proc.clear()
            tab1_run_btn.configure(state="disabled")
            stop_btn_proc.configure(state="normal", text="■ Stop")
            _log_lines_proc.clear()
            log_box_proc.configure(state="normal")
            log_box_proc.delete("1.0", "end")
            log_box_proc.configure(state="disabled")
        elif mode == "pp":
            _stop_pp.clear()
            tab3_run_btn.configure(state="disabled")
            stop_btn_pp.configure(state="normal", text="■ Stop")
            _log_lines_pp.clear()
            log_box_pp.configure(state="normal")
            log_box_pp.delete("1.0", "end")
            log_box_pp.configure(state="disabled")
        elif mode == "xfm":
            _stop_xfm.clear()
            tab2_run_btn.configure(state="disabled")
            stop_btn_xfm.configure(state="normal", text="■ Stop")
            _log_lines_xfm.clear()
            log_box_xfm.configure(state="normal")
            log_box_xfm.delete("1.0", "end")
            log_box_xfm.configure(state="disabled")

    def _on_proc_done() -> None:
        _active_proc[0] = max(0, _active_proc[0] - 1)
        if _active_proc[0] == 0:
            tab1_run_btn.configure(state="normal")
            stop_btn_proc.configure(state="disabled", text="■ Stop")
        _finish_if_idle()

    def _on_pp_done() -> None:
        _active_pp[0] = max(0, _active_pp[0] - 1)
        if _active_pp[0] == 0:
            tab3_run_btn.configure(state="normal")
            stop_btn_pp.configure(state="disabled", text="■ Stop")
        _finish_if_idle()

    def _on_xfm_done() -> None:
        _active_xfm[0] = max(0, _active_xfm[0] - 1)
        if _active_xfm[0] == 0:
            tab2_run_btn.configure(state="normal")
            stop_btn_xfm.configure(state="disabled", text="■ Stop")
        _finish_if_idle()

    def _finish_if_idle() -> None:
        if _active_proc[0] > 0 or _active_pp[0] > 0 or _active_xfm[0] > 0:
            return
        out_folder = output_folder_var.get().strip() or str(
            Path(__file__).resolve().parent / "output")
        try:
            out_path = Path(out_folder)
            log_dir  = out_path / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts    = _run_start_time[0] or time.strftime("%Y-%m-%d_%H-%M-%S")
            fname = f"{out_path.name}_{ts}.log"
            all_lines = (["=== Processing ==="] + _log_lines_proc +
                         ["", "=== Post Processing ==="] + _log_lines_pp +
                         ["", "=== Transform ==="] + _log_lines_xfm)
            with open(log_dir / fname, "w", encoding="utf-8") as fh:
                fh.write("\n".join(all_lines))
            log_proc(f"Log saved to:\n  {log_dir / fname}")
        except Exception as e:
            log_proc(f"[WARN] Could not save log: {e}")

    # -- Launch helpers --------------------------------------------------------
    def _launch_processing(cfg: dict) -> None:
        def worker():
            try:
                run_processing(cfg, log_proc, _stop_proc)
            finally:
                root.after(0, _on_proc_done)
        _active_proc[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _launch_post_processing(cfg: dict) -> None:
        def worker():
            try:
                run_post_processing(cfg, log_pp, _stop_pp)
            finally:
                root.after(0, _on_pp_done)
        _active_pp[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _launch_transform(input_dirs: list, xfm_params: dict, out_folder: str) -> None:
        def worker():
            try:
                run_transform(input_dirs=input_dirs, xfm_params=xfm_params,
                              output_folder=out_folder, log=log_xfm, stop_event=_stop_xfm)
            finally:
                root.after(0, _on_xfm_done)
        _active_xfm[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _get_input_dirs() -> list[str] | None:
        raw  = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        if not dirs:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return None
        return dirs

    # -- Run callbacks ---------------------------------------------------------
    def _resolve_out_path() -> Path:
        """Resolve the output folder the same way the pipeline does."""
        raw = output_folder_var.get().strip()
        return Path(raw) if raw else Path(__file__).resolve().parent / "output"

    def _confirm_overwrite() -> bool:
        """Return True (proceed) or False (cancel).

        Shows a yes/no dialog if the output folder already contains files so
        the user can cancel before anything is overwritten.
        """
        out = _resolve_out_path()
        if not out.exists():
            return True
        try:
            has_content = any(out.iterdir())
        except OSError:
            return True
        if not has_content:
            return True
        return messagebox.askyesno(
            "Output folder not empty",
            f"The output folder already contains files:\n\n  {out}\n\n"
            "Existing files may be overwritten.  Continue anyway?",
            icon="warning",
        )

    def on_run_processing():
        cfg = _current_cfg()
        if not cfg["input_dirs"]:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return
        if not comp_widgets:
            log('[!] No geometry loaded - please click "Load Geometry" before running.')
            return
        if not _confirm_overwrite():
            return
        save_settings(cfg)
        _set_busy("proc")
        _launch_processing(cfg)

    def on_run_post_processing():
        pp_run_cfg = get_pp_cfg()
        if pp_run_cfg is None:
            return
        pp_run_cfg["output_folder"] = output_folder_var.get()
        # Pass mult_factor_p so the CSV can record what factor was used upstream
        get_p = t3["_get_mult_factor_p"][0]
        if get_p is not None:
            pp_run_cfg["mult_factor_p"] = get_p()
        if not _confirm_overwrite():
            return
        save_settings(_current_cfg())
        _set_busy("pp")
        _launch_post_processing(pp_run_cfg)

    def on_run_transform():
        xfm_params = get_transform_params()
        if xfm_params is None:
            return
        input_dirs = xfm_params.pop("_selected_dirs", [])
        if not input_dirs:
            messagebox.showwarning("No cases selected",
                                   "Select at least one case in the Cases section.")
            return
        if not _confirm_overwrite():
            return
        save_settings(_current_cfg())
        _set_busy("xfm")
        _launch_transform(input_dirs, xfm_params, output_folder_var.get())

    tab1_run_btn.configure(command=on_run_processing)
    tab3_run_btn.configure(command=on_run_post_processing)
    tab2_run_btn.configure(command=on_run_transform)
    root.mainloop()


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()

