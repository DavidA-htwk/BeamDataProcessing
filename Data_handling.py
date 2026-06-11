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
from modules.gui.gui_tab1 import build_processing_tab
from modules.gui.gui_tab2 import build_transform_tab


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
    notebook.add(tab2, text="Coordinate Transform")

    # -- Log area (created early so log_fn can be passed to tab builders) ------
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

    _log_lines: list[str] = []
    _run_start_time: list = [None]

    def log(msg: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        _log_lines.append(msg)
        root.update_idletasks()

    # -- Build tabs ------------------------------------------------------------
    t1 = build_processing_tab(tab1, settings, log)
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
        apply_xfm_cfg(loaded.get("transform", {}))

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

    # -- Run Both + Stop buttons -----------------------------------------------
    _stop_event    = threading.Event()
    _active_workers = [0]

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

    _all_run_btns = [tab1_run_btn, tab2_run_btn, run_both_btn]

    def on_stop():
        _stop_event.set()
        stop_btn.configure(state="disabled", text="Stopping...")

    stop_btn.configure(command=on_stop)

    def _set_busy():
        _stop_event.clear()
        _log_lines.clear()
        _run_start_time[0] = time.strftime("%Y-%m-%d_%H-%M-%S")
        stop_btn.configure(state="normal", text="Stop")
        for b in _all_run_btns:
            b.configure(state="disabled")
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")

    def _on_worker_done():
        _active_workers[0] -= 1
        if _active_workers[0] > 0:
            return
        _active_workers[0] = 0
        for b in _all_run_btns:
            b.configure(state="normal")
        stop_btn.configure(state="disabled", text="Stop")
        out_folder = output_folder_var.get().strip() or str(
            Path(__file__).resolve().parent / "output")
        try:
            out_path = Path(out_folder)
            out_path.mkdir(parents=True, exist_ok=True)
            ts    = _run_start_time[0] or time.strftime("%Y-%m-%d_%H-%M-%S")
            fname = f"{out_path.name}_{ts}.log"
            with open(out_path / fname, "w", encoding="utf-8") as fh:
                fh.write("\n".join(_log_lines))
            log(f"Log saved to:\n  {out_path / fname}")
        except Exception as e:
            log(f"[WARN] Could not save log: {e}")

    # -- Launch helpers --------------------------------------------------------
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
                run_transform(input_dirs=input_dirs, xfm_params=xfm_params,
                              output_folder=out_folder, log=log, stop_event=_stop_event)
            finally:
                root.after(0, _on_worker_done)
        _active_workers[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _get_input_dirs() -> list[str] | None:
        raw  = text_box.get("1.0", "end").strip()
        dirs = [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]
        if not dirs:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return None
        return dirs

    # -- Run callbacks ---------------------------------------------------------
    def on_run_processing():
        cfg = _current_cfg()
        if not cfg["input_dirs"]:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return
        if not comp_widgets:
            log('[!] No geometry loaded - please click "Load Geometry" before running.')
            return
        save_settings(cfg)
        _set_busy()
        _launch_processing(cfg)

    def on_run_transform():
        input_dirs = _get_input_dirs()
        if input_dirs is None:
            return
        xfm_params = get_transform_params()
        if xfm_params is None:
            return
        save_settings(_current_cfg())
        _set_busy()
        _launch_transform(input_dirs, xfm_params, output_folder_var.get())

    def on_run_both():
        input_dirs = _get_input_dirs()
        if input_dirs is None:
            return
        xfm_params = get_transform_params()
        if xfm_params is None:
            return
        cfg = _current_cfg()
        if not cfg["input_dirs"]:
            messagebox.showwarning("No directories", "Please paste at least one directory path.")
            return
        if not comp_widgets:
            log('[!] No geometry loaded - please click "Load Geometry" before running.')
            return
        save_settings(cfg)
        _set_busy()
        _launch_processing(cfg)
        _launch_transform(input_dirs, xfm_params, output_folder_var.get())

    tab1_run_btn.configure(command=on_run_processing)
    tab2_run_btn.configure(command=on_run_transform)
    run_both_btn.configure(command=on_run_both)
    root.mainloop()


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()

