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
from modules.vtk.smart_smooth import smart_smooth_auto
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
            n_iter, stop_event, geo_cache, smooth_mode, spike_sigma, \
            proximity_radius = args
        if stop_event is not None and stop_event.is_set():
            return None
        if n_iter == 0:
            return filepath, output_name, case, scenario, polydata, max_before, polydata, max_before
        if smooth_mode == "auto":
            smoothed = smart_smooth_auto(
                polydata, n_iter=n_iter, stop_event=stop_event,
                geo_cache=geo_cache, spike_sigma=spike_sigma,
                proximity_radius=proximity_radius,
            )
        else:
            smoothed = apply_edge_smooth(polydata, n_iter=n_iter,
                                         stop_event=stop_event, geo_cache=geo_cache)
        if smoothed is None:
            return None
        max_after = find_max(smoothed, ARRAY_NAME)
        return filepath, output_name, case, scenario, polydata, max_before, smoothed, max_after
    except Exception as exc:
        return exc


def _load_smooth_write_one_file(args: tuple) -> tuple:
    """Load, smooth, and optionally write a temp VTP for one file.

    This combined worker is the memory-efficient path: polydata is loaded,
    processed, and freed entirely within this function.  Only scalar metadata
    (floats, paths, precomputed camera dicts) is returned to the caller.

    Camera precomputation (vtkCellLocator + multi-ray cast) is done here while
    polydata is already in memory, so _render_subprocess can skip rebuilding the
    locator on the large mesh — the most expensive part of snapshot rendering.

    Returns (filepath, output_name, case, scenario, max_before, max_after,
             total_pwr, smooth_vtp_path,
             pre_orig_cam, pre_smooth_cam, max_pwr_orig, max_pwr_smooth)
    or an Exception or None if stopped.
    """
    try:
        import hashlib
        (fp, on, c, s,
         n_iter, stop_event, geo_cache,
         smooth_mode, spike_sigma, proximity_radius,
         smooth_spikes, spike_ratio,
         needs_snap, snap_dir_str, pid) = args

        if stop_event is not None and stop_event.is_set():
            return None

        # ── Load ──────────────────────────────────────────────────────────────
        polydata   = read_vtp(str(fp))
        # Build VTK's internal cell-links structure explicitly before entering
        # the thread pool's shared execution.  vtkPolyData.GetPointCells() builds
        # this structure lazily on first call via a non-reentrant C++ lock; if 12
        # threads all call GetPointCells() on their respective polydata objects
        # simultaneously, they can deadlock on that lock.  Building it here
        # (sequentially within each thread, before any concurrent VTK calls)
        # ensures every subsequent GetPointCells() call is a fast O(1) lookup.
        polydata.BuildLinks()
        max_before = find_max(polydata, ARRAY_NAME)
        total_pwr  = find_total(polydata, POWER_ARRAY)

        if max_before is None:
            del polydata
            return Exception(f"Array '{ARRAY_NAME}' not found in {fp.name}")

        if stop_event is not None and stop_event.is_set():
            del polydata
            return None

        # ── Smooth + precompute camera ─────────────────────────────────────────
        smooth_vtp_path: str | None  = None
        pre_orig_cam:    dict | None = None
        pre_smooth_cam:  dict | None = None
        max_pwr_orig:    float | None = None
        max_pwr_smooth:  float | None = None

        if n_iter > 0:
            if smooth_mode == "auto":
                smoothed = smart_smooth_auto(
                    polydata, n_iter=n_iter, stop_event=stop_event,
                    geo_cache=geo_cache, spike_sigma=spike_sigma,
                    spike_ratio=spike_ratio,
                    proximity_radius=proximity_radius,
                    smooth_spikes=smooth_spikes,
                )
            else:
                smoothed = apply_edge_smooth(
                    polydata, n_iter=n_iter,
                    stop_event=stop_event, geo_cache=geo_cache,
                )

            # Precompute camera from original while still in memory.
            # Camera placement uses vtkCellLocator (expensive on large meshes);
            # doing it here avoids rebuilding the locator in the render process.
            if needs_snap:
                pre_orig_cam = precompute_snapshot(polydata, ARRAY_NAME)
                max_pwr_orig = find_max(polydata, POWER_ARRAY)

            del polydata   # free original immediately after smoothing + precompute
            if smoothed is None:
                return None

            max_after      = find_max(smoothed, ARRAY_NAME)
            total_pwr_after = find_total(smoothed, POWER_ARRAY)

            if needs_snap:
                # BuildLinks() needed so _robust_normal → GetPointCells works
                # correctly inside precompute_snapshot (smoothed is a fresh
                # DeepCopy that hasn't built its internal link structure yet).
                smoothed.BuildLinks()
                pre_smooth_cam = precompute_snapshot(smoothed, ARRAY_NAME)
                max_pwr_smooth = find_max(smoothed, POWER_ARRAY)
                h   = hashlib.md5(str(fp).encode()).hexdigest()[:12]
                tmp = Path(snap_dir_str) / f"_tmp_smooth_{pid}_{h}.vtp"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                _write_vtp(smoothed, tmp)
                smooth_vtp_path = str(tmp)

            del smoothed   # free after precompute + write
        else:
            if needs_snap:
                pre_orig_cam   = precompute_snapshot(polydata, ARRAY_NAME)
                max_pwr_orig   = find_max(polydata, POWER_ARRAY)
                pre_smooth_cam = pre_orig_cam   # snapshot_only: same file for both
                max_pwr_smooth = max_pwr_orig
            del polydata
            max_after       = max_before
            total_pwr_after = total_pwr  # no smoothing applied

        return (fp, on, c, s, max_before, max_after, total_pwr, total_pwr_after, smooth_vtp_path,
                pre_orig_cam, pre_smooth_cam, max_pwr_orig, max_pwr_smooth)

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

    pre_orig_cam / pre_smooth_cam contain centroid, normal, cam_pos, diag and
    max_val precomputed in the smoothing worker (polydata already in memory there).
    This avoids rebuilding vtkCellLocator on the 26 M-cell mesh here.
    """
    try:
        (orig_vtp_path, smooth_vtp_path, out_paths,
         array_name, snapshot_only, label, mult_factor,
         pre_orig_cam, pre_smooth_cam,
         max_pwr_orig, max_pwr_smooth, total_pwr_raw) = args
        t0 = time.perf_counter()

        _total_pwr = (total_pwr_raw * mult_factor) if total_pwr_raw is not None else None

        def _build_pre(cam: dict | None, max_pwr_raw: float | None) -> dict | None:
            """Build precomputed dict from cached camera data + scaled max_val.

            cam contains geometry-only fields (centroid, normal, cam_pos, diag,
            max_cid) from ARRAY_NAME.  max_val is overridden with the appropriate
            scaled value for the current array_name.
            """
            if cam is None:
                return None
            if array_name == ARRAY_NAME:
                # Power density: max_val from the same array used for camera
                return {**cam, "max_val": cam["max_val"] * mult_factor}
            else:
                # Total power: camera from pwr_density, max_val from POWER_ARRAY
                mv = (max_pwr_raw * mult_factor
                      if max_pwr_raw is not None
                      else cam["max_val"] * mult_factor)
                return {**cam, "max_val": mv}

        pre_orig   = _build_pre(pre_orig_cam,   max_pwr_orig)
        pre_smooth = _build_pre(pre_smooth_cam, max_pwr_smooth)

        pd_orig = read_vtp(str(orig_vtp_path))
        pd_orig = _scale_polydata_array(pd_orig, array_name, mult_factor)

        # For total power: the precomputed max_val comes from the worker's
        # find_max() on the unscaled mesh.  Re-derive it from the actual
        # polydata here to ensure it matches whatever was scaled/read back,
        # and also correctly sets the active scalar so the mapper uses
        # POWER_ARRAY for coloring (not the last-set active ARRAY_NAME).
        if array_name != ARRAY_NAME:
            _arr = pd_orig.GetCellData().GetArray(array_name)
            if _arr is not None:
                from vtk.util.numpy_support import vtk_to_numpy as _v2n
                _actual_max = float(_v2n(_arr).max())
                if pre_orig is not None:
                    pre_orig = {**pre_orig, "max_val": _actual_max}
                pd_orig.GetCellData().SetActiveScalars(array_name)

        if snapshot_only:
            save_max_snapshot(pd_orig, array_name, Path(out_paths[0]),
                              vmax=None, precomputed=pre_orig, total_power_W=_total_pwr)
        else:
            save_max_snapshot(pd_orig, array_name, Path(out_paths[0]),
                              vmax=None, precomputed=pre_orig, total_power_W=_total_pwr)
            pd_smooth = read_vtp(str(smooth_vtp_path))
            pd_smooth = _scale_polydata_array(pd_smooth, array_name, mult_factor)
            if array_name != ARRAY_NAME:
                _arr_s = pd_smooth.GetCellData().GetArray(array_name)
                if _arr_s is not None:
                    from vtk.util.numpy_support import vtk_to_numpy as _v2n_s
                    _actual_max_s = float(_v2n_s(_arr_s).max())
                    if pre_smooth is not None:
                        pre_smooth = {**pre_smooth, "max_val": _actual_max_s}
                    pd_smooth.GetCellData().SetActiveScalars(array_name)
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
