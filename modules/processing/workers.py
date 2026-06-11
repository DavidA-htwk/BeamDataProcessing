"""
modules/workers.py
------------------
Thread-pool and process-pool worker functions for the batch pipeline.

All functions are designed to be picklable (used with multiprocessing.Pool)
or run in threads (ThreadPool).  No shared mutable state — each function
receives everything it needs via its args tuple.
"""

from __future__ import annotations

import time
from pathlib import Path

import vtk

from modules.core.settings import ARRAY_NAME, POWER_ARRAY
from modules.vtk.vtk_io import (
    read_vtp, find_max, find_total, _write_vtp, _scale_polydata_array,
)
from modules.vtk.smoothing import apply_edge_smooth
from modules.vtk.snapshot_max import save_max_snapshot, precompute_snapshot
from modules.transform.generate_report import extract_cells_to_csv
from modules.transform import transform_reference_frame as _trf


# ── Thread workers ────────────────────────────────────────────────────────────

def _load_one_file(args: tuple) -> tuple:
    """Load one VTP file and extract the max scalar value.

    Returns the result tuple, or the Exception on failure (never raises — keeps
    the pool alive).
    """
    try:
        filepath, output_name, case, scenario = args
        polydata  = read_vtp(str(filepath))
        max_val   = find_max(polydata, ARRAY_NAME)
        total_pwr = find_total(polydata, POWER_ARRAY)
        return filepath, output_name, case, scenario, polydata, max_val, total_pwr
    except Exception as exc:
        return exc


def _smooth_one_file(args: tuple) -> tuple:
    """Apply edge smoothing to one loaded file.

    Returns the full processed tuple, an Exception, or None if stopped.
    """
    try:
        filepath, output_name, case, scenario, polydata, max_before, \
            n_iter, stop_event, geo_cache = args
        if stop_event is not None and stop_event.is_set():
            return None
        if n_iter == 0:
            return filepath, output_name, case, scenario, polydata, max_before, polydata, max_before
        smoothed = apply_edge_smooth(polydata, n_iter=n_iter,
                                     stop_event=stop_event, geo_cache=geo_cache)
        if smoothed is None:
            return None
        max_after = find_max(smoothed, ARRAY_NAME)
        return filepath, output_name, case, scenario, polydata, max_before, smoothed, max_after
    except Exception as exc:
        return exc


def _transform_one_file(args: tuple) -> tuple:
    """Extract cells to CSV and apply coordinate transform for one VTP file.

    Returns (filepath, out_path, None) or an Exception.
    """
    try:
        filepath, dest_dir, scenario, xfm_params = args
        angle_deg    = xfm_params["angle_deg"]
        dx           = xfm_params["dx"]
        dy           = xfm_params["dy"]
        dz           = xfm_params["dz"]
        export_geom  = xfm_params.get("export_geom",  True)
        export_area  = xfm_params.get("export_area",  True)
        export_power = xfm_params.get("export_power", True)
        export_pload = xfm_params.get("export_pload", True)
        mult         = float(xfm_params.get("mult", 1.0))
        ignore_zeros = xfm_params.get("ignore_zeros", False)
        coord_scale  = float(xfm_params.get("coord_scale", 1.0))

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        csv_name = f"{scenario}__{filepath.stem}.csv"
        out_path = dest_dir / csv_name
        tmp_csv  = dest_dir / f"{scenario}__{filepath.stem}.tmp.csv"

        extract_cells_to_csv(
            str(filepath), str(tmp_csv),
            export_geom=export_geom, export_area=export_area,
            export_power=export_power, export_pload=export_pload,
            mult=mult, ignore_zeros=ignore_zeros,
        )
        _trf.process_file(
            input_path=tmp_csv, output_path=out_path,
            x_col=None, y_col=None, z_col=None,
            angle_deg=angle_deg, dx=dx, dy=dy, dz=dz,
            coord_scale=coord_scale,
        )
        tmp_csv.unlink(missing_ok=True)
        return filepath, out_path, None
    except Exception as exc:
        return exc


# ── Process worker (subprocess rendering) ────────────────────────────────────

def _render_subprocess(args: tuple) -> tuple:
    """Render and save snapshot(s) for one file.  MUST run in a separate PROCESS.

    On Windows, vtkRenderWindow uses WGL which acquires a per-process GPU driver
    lock via wglMakeCurrent().  Running in separate processes gives each its own
    GPU context so DWM compositing of the Tkinter window is never blocked.
    """
    try:
        (orig_vtp_path, smooth_vtp_path, out_paths,
         array_name, snapshot_only, label, mult_factor) = args
        t0 = time.perf_counter()

        pd_orig = read_vtp(str(orig_vtp_path))
        pd_orig = _scale_polydata_array(pd_orig, array_name, mult_factor)

        # Camera: always use Power_Density_W_m2 for focal-point placement so both
        # render types look from the same angle (beam hot-spot, not edge cell).
        def _make_precomputed(pd_scaled, pd_raw_for_cam):
            pre_arr = precompute_snapshot(pd_scaled, array_name)
            if array_name == ARRAY_NAME or pre_arr is None:
                return pre_arr
            pre_cam = precompute_snapshot(pd_raw_for_cam, ARRAY_NAME)
            if pre_cam is None:
                return pre_arr
            return {**pre_cam, "max_val": pre_arr["max_val"]}

        pd_orig_raw = read_vtp(str(orig_vtp_path))
        _raw_total  = find_total(pd_orig_raw, POWER_ARRAY)
        _total_pwr  = (_raw_total * mult_factor) if _raw_total is not None else None
        pre_orig    = _make_precomputed(pd_orig, pd_orig_raw)

        if snapshot_only:
            save_max_snapshot(pd_orig, array_name, Path(out_paths[0]),
                              vmax=None, precomputed=pre_orig, total_power_W=_total_pwr)
        else:
            save_max_snapshot(pd_orig, array_name, Path(out_paths[0]),
                              vmax=None, precomputed=pre_orig, total_power_W=_total_pwr)
            pd_smooth     = read_vtp(str(smooth_vtp_path))
            pd_smooth_raw = pd_smooth
            pd_smooth     = _scale_polydata_array(pd_smooth, array_name, mult_factor)
            if pd_smooth is pd_smooth_raw:
                pd_smooth_raw = read_vtp(str(smooth_vtp_path))
            pre_smooth = _make_precomputed(pd_smooth, pd_smooth_raw)
            save_max_snapshot(pd_smooth, array_name, Path(out_paths[1]),
                              vmax=None, precomputed=pre_smooth, total_power_W=_total_pwr)

        return label, time.perf_counter() - t0
    except Exception as exc:
        return exc


# ── Legacy thread-based snapshot worker (fallback) ────────────────────────────

def _save_one_snapshot(args: tuple) -> tuple:
    """Legacy thread-based snapshot worker — kept as fallback.

    Prefer _render_subprocess via multiprocessing.Pool for Win32 responsiveness.
    """
    try:
        (filepath, output_name, case, scenario,
         polydata_orig, polydata_smooth, snap_dir, snapshot_only) = args

        pd_orig = vtk.vtkPolyData()
        pd_orig.DeepCopy(polydata_orig)
        pre_orig = precompute_snapshot(pd_orig, ARRAY_NAME)

        stem          = filepath.stem
        case_snap_dir = snap_dir / output_name / case / scenario
        case_snap_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()

        if snapshot_only:
            png_snap = case_snap_dir / f"{scenario}__{stem}.png"
            save_max_snapshot(pd_orig, ARRAY_NAME, png_snap, precomputed=pre_orig)
            return png_snap.name, time.perf_counter() - t0
        else:
            pd_smooth = vtk.vtkPolyData()
            pd_smooth.DeepCopy(polydata_smooth)
            pre_smooth  = precompute_snapshot(pd_smooth, ARRAY_NAME)
            png_before  = case_snap_dir / f"{scenario}__{stem}__before.png"
            save_max_snapshot(pd_orig,   ARRAY_NAME, png_before, precomputed=pre_orig)
            png_after   = case_snap_dir / f"{scenario}__{stem}__after.png"
            save_max_snapshot(pd_smooth, ARRAY_NAME, png_after,  precomputed=pre_smooth)
            return stem, time.perf_counter() - t0
    except Exception as exc:
        return exc
