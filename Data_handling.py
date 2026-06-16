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
    log_outer = tk.Frame(root)
    log_outer.pack(fill="both", expand=True, padx=10, pady=(6, 4))

    # Two side-by-side log panes — one per pipeline
    def _make_log_pane(parent: tk.Frame, label: str) -> tk.Text:
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
    log_box_xfm  = _make_log_pane(log_outer, "Transform log")

    _log_lines_proc: list[str] = []
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

    def log_xfm(msg: str) -> None:
        _append_to(log_box_xfm, _log_lines_xfm, msg)

    # Unified log for backward-compat (used by tab builders etc.)
    def log(msg: str) -> None:
        log_proc(msg)

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

    # Wire Tab 1 directory reader into Tab 2's Load Cases button
    def _get_tab1_dirs_for_t2() -> list[str]:
        raw  = text_box.get("1.0", "end").strip()
        return [ln.strip().strip('"').strip("'") for ln in raw.splitlines() if ln.strip()]

    t2["_get_tab1_dirs"][0] = _get_tab1_dirs_for_t2
    t2["_get_output_folder"][0] = lambda: output_folder_var.get().strip()
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
    _stop_event       = threading.Event()
    _active_proc      = [0]   # processing workers running
    _active_xfm       = [0]   # transform workers running

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

    def _set_busy(mode: str = "both") -> None:
        """mode: 'proc' | 'xfm' | 'both' — which pane(s) to clear."""
        _stop_event.clear()
        _run_start_time[0] = time.strftime("%Y-%m-%d_%H-%M-%S")
        stop_btn.configure(state="normal", text="Stop")
        # Disable only the button(s) being started; Run Both always disabled while anything runs
        run_both_btn.configure(state="disabled")
        if mode in ("proc", "both"):
            tab1_run_btn.configure(state="disabled")
            _log_lines_proc.clear()
            log_box_proc.configure(state="normal")
            log_box_proc.delete("1.0", "end")
            log_box_proc.configure(state="disabled")
        if mode in ("xfm", "both"):
            tab2_run_btn.configure(state="disabled")
            _log_lines_xfm.clear()
            log_box_xfm.configure(state="normal")
            log_box_xfm.delete("1.0", "end")
            log_box_xfm.configure(state="disabled")

    def _on_proc_done() -> None:
        _active_proc[0] = max(0, _active_proc[0] - 1)
        if _active_proc[0] == 0:
            tab1_run_btn.configure(state="normal")
        _finish_if_idle()

    def _on_xfm_done() -> None:
        _active_xfm[0] = max(0, _active_xfm[0] - 1)
        if _active_xfm[0] == 0:
            tab2_run_btn.configure(state="normal")
        _finish_if_idle()

    def _finish_if_idle() -> None:
        if _active_proc[0] > 0 or _active_xfm[0] > 0:
            return
        run_both_btn.configure(state="normal")
        stop_btn.configure(state="disabled", text="Stop")
        out_folder = output_folder_var.get().strip() or str(
            Path(__file__).resolve().parent / "output")
        try:
            out_path = Path(out_folder)
            out_path.mkdir(parents=True, exist_ok=True)
            ts    = _run_start_time[0] or time.strftime("%Y-%m-%d_%H-%M-%S")
            fname = f"{out_path.name}_{ts}.log"
            all_lines = (["=== Processing ==="] + _log_lines_proc +
                         ["", "=== Transform ==="] + _log_lines_xfm)
            with open(out_path / fname, "w", encoding="utf-8") as fh:
                fh.write("\n".join(all_lines))
            log_proc(f"Log saved to:\n  {out_path / fname}")
        except Exception as e:
            log_proc(f"[WARN] Could not save log: {e}")

    # -- Launch helpers --------------------------------------------------------
    def _launch_processing(cfg: dict) -> None:
        def worker():
            try:
                run_processing(cfg, log_proc, _stop_event)
            finally:
                root.after(0, _on_proc_done)
        _active_proc[0] += 1
        threading.Thread(target=worker, daemon=True).start()

    def _launch_transform(input_dirs: list, xfm_params: dict, out_folder: str) -> None:
        def worker():
            try:
                run_transform(input_dirs=input_dirs, xfm_params=xfm_params,
                              output_folder=out_folder, log=log_xfm, stop_event=_stop_event)
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
            has_files = any(p.is_file() for p in out.iterdir())
        except OSError:
            return True
        if not has_files:
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

    def on_run_both():
        xfm_params = get_transform_params()
        if xfm_params is None:
            return
        xfm_input_dirs = xfm_params.pop("_selected_dirs", [])
        if not xfm_input_dirs:
            messagebox.showwarning("No cases selected",
                                   "Select at least one case in the Cases section.")
            return
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
        _set_busy("both")
        _launch_processing(cfg)
        _launch_transform(xfm_input_dirs, xfm_params, output_folder_var.get())

    tab1_run_btn.configure(command=on_run_processing)
    tab2_run_btn.configure(command=on_run_transform)
    run_both_btn.configure(command=on_run_both)
    root.mainloop()


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()

