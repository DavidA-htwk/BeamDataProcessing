"""
modules/processing/post_pipeline.py
------------------------------------
Post-processing pipeline: cell-wise worst-case merge across selected cases.

run_post_processing(cfg, log, stop_event)
  - Groups VTP files from selected scenario dirs by (output_name, case, stem)
  - Per cell, picks the case with the highest Power_Density_W_m2 and takes ALL
    cell arrays (species, errors) from it — the totals are the per-cell max
    (running, memory-efficient — only one mesh in RAM beyond the geometry copy)
  - Applies mult_factor to merged arrays if source=original (skipped otherwise)
  - Writes merged VTPs to {output_folder}/post_processed/{output_name}/{case}/
  - Optionally renders snapshots via the same _render_subprocess pool as pipeline.py
  - Writes post_processed_batch.csv

run_component_worst_case_merge(cfg, log, stop_event)
  - Appends one picked scenario file per component into a single VTK

run_sum_merge(cfg, log, stop_event)
  - Sums Deposited_Power_W of the same scenario/component across several
    output folders and recomputes Power_Density_W_m2 = summed power / area
"""

from __future__ import annotations

import csv
import multiprocessing
import os
import time
import threading
from collections import defaultdict
from pathlib import Path

import vtk
import numpy as np
from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk

from modules.core.settings import ARRAY_NAME, POWER_ARRAY
from modules.core.console import configure_stdio
from modules.core.path_utils import extract_case_scenario
from modules.vtk.vtk_io import read_vtp, _write_vtp
from modules.vtk.snapshot_max import precompute_snapshot
from modules.processing.workers import _render_subprocess


def run_post_processing(
        cfg: dict,
        log,
        stop_event: threading.Event | None = None,
) -> None:
    """Cell-wise max merge pipeline."""

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    input_dirs_legacy = cfg.get("input_dirs", [])   # backward compat: no groups key
    # New multi-group format: {group_name: [dir_path, ...]}
    # Fall back to a single unnamed group ("merged") when old format used
    groups_cfg: dict[str, list[str]] = cfg.get("groups") or (
        {"merged": input_dirs_legacy} if input_dirs_legacy else {}
    )
    # Custom labels: {internal_key: display_name} — used for output filenames
    group_labels: dict[str, str] = cfg.get("group_labels", {})
    pattern     = cfg.get("pattern", "smoothed_results_*.vtp")
    name_filter = cfg.get("name_filter", "")
    out_root    = cfg.get("output_folder", "")
    mult_factor = float(cfg.get("mult_factor", 1.0))
    apply_mult  = bool(cfg.get("apply_mult", True))
    # Note: VTP files (original and post-smoothed) always store RAW unscaled values.
    # The mult_factor is only applied to snapshots and CSV in the Processing pipeline,
    # never baked into the .vtp files themselves.  apply_mult is therefore always True.
    # Every cell array follows the per-cell winning case, so these only pick
    # the ranking array (Power_Density_W_m2 unless merge_pd is off).
    merge_pd    = bool(cfg.get("merge_pd",   True))
    merge_pwr   = bool(cfg.get("merge_pwr",  True))
    snap_pd     = bool(cfg.get("snap_pwr_density", True))
    snap_tp     = bool(cfg.get("snap_total_pwr", False))
    save_snaps  = bool(cfg.get("save_snapshots", False)) and (snap_pd or snap_tp)

    script_dir  = Path(__file__).resolve().parent.parent.parent
    out_dir     = Path(out_root) if out_root else script_dir / "output"
    pp_dir      = out_dir / "post_processed"
    snap_dir    = out_dir / "post_processed_snapshots"
    csv_dir     = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    _ts         = time.strftime("%Y-%m-%d_%H-%M-%S")
    csv_path    = csv_dir / f"post_processed_batch_{_ts}.csv"

    os.makedirs(pp_dir, exist_ok=True)

    if not groups_cfg:
        log("No cases assigned to any group. Assign cases using the colour tools.")
        return

    # ── Phase 0: Collect and group files ─────────────────────────────────────
    log("Collecting files from selected cases...")
    log(f"  Pattern: {pattern}  |  filter: {name_filter or '(none)'}")
    log(f"  Snapshot factor: {mult_factor:.6g} (applied to snapshots only — VTPs always store raw values)")
    # Worst case per cell is decided by the power density; Deposited_Power_W
    # ranks the cases identically on a shared mesh (same cell area), so it is
    # only used when Power Density is unticked.
    rank_name = ARRAY_NAME if merge_pd else POWER_ARRAY
    log(f"  Merge: per-cell worst case ranked by {rank_name}; every cell array "
        "(totals, species _H0/_H+/_H-, StdErr, Relative_Error) is taken from "
        "the winning case of that cell.")
    log(f"  Groups: {[group_labels.get(k, k) for k in groups_cfg.keys()]}")

    # slot_groups[(group_name, output_name, stem)] = [(Path, scenario_name), ...]
    slot_groups: dict[tuple, list[tuple]] = defaultdict(list)
    terms = ([t.strip().lower() for t in name_filter.split(",") if t.strip()]
             if name_filter else [])

    n_raw_found   = 0   # files matching pattern (before name filter)
    n_filtered_out = 0  # files excluded by name filter

    for group_name, dir_list in groups_cfg.items():
        for dir_str in dir_list:
            if stopped():
                break
            p = Path(dir_str)
            if not p.is_dir():
                log(f"  [SKIP] Not a directory: {dir_str}")
                continue
            output_name, _, scenario_name = extract_case_scenario(str(p))
            raw_files = sorted(p.rglob(pattern))
            n_raw_found += len(raw_files)
            if terms:
                kept  = [f for f in raw_files if any(t in f.stem.lower() for t in terms)]
                n_filtered_out += len(raw_files) - len(kept)
                files = kept
            else:
                files = raw_files
            for fp in files:
                slot_groups[(group_name, output_name, fp.stem)].append(
                    (fp, scenario_name))

    if not slot_groups:
        if n_raw_found > 0 and n_filtered_out > 0:
            log(f"No files passed the name filter.  "
                f"{n_raw_found} file(s) matched the pattern but all were excluded "
                f"by the name filter '{name_filter}'.")
            log("  → Clear the name filter or adjust it to match your filenames.")
        else:
            log("No files found. Check pattern and selected cases.")
        return

    total_slots  = len(slot_groups)
    total_inputs = sum(len(v) for v in slot_groups.values())
    n_groups     = len({k[0] for k in slot_groups})
    log(f"  {n_groups} group(s), {total_slots} merge slot(s), "
        f"{total_inputs} input file(s).")
    log("=" * 80)

    # ── Phase 1: Merge ────────────────────────────────────────────────────────
    t0_total   = time.perf_counter()
    snap_args: list[tuple] = []
    done_count = 0
    written_vtps: set[Path] = set()   # outputs of this run — never overwritten

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["group", "output_name", "merged_cases", "filename",
                         "n_cases_merged", "max_val", "total_power",
                         "snap_factor", "source",
                         "snapshot", "vtp_path"])

        for (group_name, output_name, stem), file_case_pairs in sorted(slot_groups.items()):
            if stopped():
                break
            done_count += 1
            t0 = time.perf_counter()

            # ── Running per-cell worst case (memory-efficient: O(1) polydata in RAM)
            # Each cell takes ALL its cell arrays from the case with the highest
            # rank_name value there, so the species split (_H0/_H+/_H-), SumSq,
            # StdErr and Relative_Error stay consistent with that cell's total.
            # Different cells can come from different cases, so the result is
            # still an envelope that matches no single scenario.
            geom_pd      = None    # vtkPolyData — geometry from first file, retained
            running: dict[str, np.ndarray] = {}   # {cell array name: worst-case values}
            dropped: set[str] = set()             # arrays missing from some source
            n_loaded     = 0
            merged_cases: list[str] = []   # case names that contributed

            _slot_label = group_labels.get(group_name, group_name)
            log(f"  [{done_count}/{total_slots}] [{_slot_label}] {stem}  "
                f"({len(file_case_pairs)} source file(s)):")
            for fp, case_name in file_case_pairs:
                log(f"    + [{case_name}]  {fp}")

            for fp, case_name in file_case_pairs:
                if stopped():
                    break
                try:
                    pd = read_vtp(str(fp))
                except Exception as exc:
                    log(f"  [ERROR] {fp.name}: {exc}")
                    continue

                cd = pd.GetCellData()
                rank_vtk = cd.GetArray(rank_name)
                if rank_vtk is None:
                    log(f"  [SKIP] {fp.name}: '{rank_name}' not found.")
                    del pd
                    continue

                if geom_pd is None:
                    geom_pd = pd
                    for i in range(cd.GetNumberOfArrays()):
                        name, arr = cd.GetArrayName(i), cd.GetArray(i)
                        if name and arr is not None:
                            running[name] = vtk_to_numpy(arr).copy()
                else:
                    if pd.GetNumberOfCells() != geom_pd.GetNumberOfCells():
                        log(f"  [SKIP] {fp.name}: {pd.GetNumberOfCells()} cells vs "
                            f"{geom_pd.GetNumberOfCells()} in the first file — "
                            "cannot merge cell-wise.")
                        del pd
                        continue
                    win = vtk_to_numpy(rank_vtk) > running[rank_name]
                    for name in list(running):
                        arr = cd.GetArray(name)
                        new = vtk_to_numpy(arr) if arr is not None else None
                        if new is None or new.shape != running[name].shape:
                            # A partial worst case would be misleading — drop it.
                            log(f"    [NOTE] {fp.name}: '{name}' missing or of a "
                                "different shape — dropped from the merged output.")
                            del running[name]
                            dropped.add(name)
                            continue
                        running[name][win] = new[win]
                    del pd
                merged_cases.append(case_name)
                n_loaded += 1

            if geom_pd is None:
                log(f"  [{done_count}/{total_slots}] [{group_name}] {stem}: "
                    "no valid files — skipped.")
                continue

            # VTPs always store raw values — snapshot factor is applied only during rendering.
            running_pd  = running.get(ARRAY_NAME)
            running_pwr = running.get(POWER_ARRAY)
            max_val   = float(running_pd.max())  if running_pd  is not None else None
            max_pwr_v = float(running_pwr.max()) if running_pwr is not None else None
            total_pwr = float(running_pwr.sum()) if running_pwr is not None else None

            # ── Build merged vtkPolyData ──────────────────────────────────────
            merged = vtk.vtkPolyData()
            merged.DeepCopy(geom_pd)
            del geom_pd

            mcd = merged.GetCellData()
            for name in dropped:
                mcd.RemoveArray(name)
            for name, arr_np in running.items():
                new_a = numpy_to_vtk(arr_np, deep=True)
                new_a.SetName(name)
                mcd.RemoveArray(name)
                mcd.AddArray(new_a)
            mcd.SetActiveScalars(ARRAY_NAME if running_pd is not None else rank_name)

            # ── Snapshot precompute (must happen before del merged) ───────────
            pre_cam = None
            if save_snaps:
                merged.BuildLinks()
                pre_cam = precompute_snapshot(merged, ARRAY_NAME)

            # ── Write merged VTP ──────────────────────────────────────────────
            # Use the custom label for the output subdir and filename prefix.
            _group_label = group_labels.get(group_name, group_name)
            # Sanitise label for use in filesystem names
            import re as _re
            _safe_label = _re.sub(r'[\\/:*?"<>|]', "_", _group_label).strip()
            # Strip source prefix from stem for clean merged name, but tag
            # smoothed sources with a suffix: a recursive pattern such as
            # "*.vtp" picks up results_X.vtp AND SMOOTHED/smoothed_results_X.vtp,
            # and without the tag both slots would write the same file.
            _bare_stem, _variant = stem, ""
            for _pfx, _tag in (("post_smooth_results_", "__post_smooth"),
                               ("post_smooth__",        "__post_smooth"),
                               ("smoothed_results_",    "__smoothed"),
                               ("merged_results_",      ""),
                               ("merged__",             "")):
                if _bare_stem.startswith(_pfx):
                    _bare_stem, _variant = _bare_stem[len(_pfx):], _tag
                    break
            if _bare_stem.startswith("results_"):
                _bare_stem = _bare_stem[len("results_"):]
            _merged_base = f"merged_results_{_bare_stem}{_variant}"
            out_subdir = pp_dir / output_name / _safe_label
            out_subdir.mkdir(parents=True, exist_ok=True)
            if out_subdir / f"{_merged_base}.vtp" in written_vtps:
                # Last-resort guard — never overwrite an output of this run.
                _merged_base = f"merged_{stem}"
            _merged_filename = f"{_merged_base}.vtp"
            out_vtp = out_subdir / _merged_filename
            written_vtps.add(out_vtp)
            _write_vtp(merged, out_vtp)

            elapsed = time.perf_counter() - t0
            cases_str = "; ".join(merged_cases)
            _max_s = f"{max_val:.4g}"   if max_val   is not None else "n/a"
            _tp_s  = f"{total_pwr:.4g}" if total_pwr is not None else "n/a"
            log(f"    └─ merged {n_loaded} case(s) in {elapsed:.1f}s  "
                f"max={_max_s}  total_pwr={_tp_s}  → {_merged_filename}")

            # ── Queue snapshot args ───────────────────────────────────────────
            snap_str = ""
            if save_snaps and pre_cam is not None:
                snap_dir_case = snap_dir / output_name / _safe_label
                snap_dir_case.mkdir(parents=True, exist_ok=True)
                _extra = (pre_cam, pre_cam, max_pwr_v, max_pwr_v, total_pwr)
                if snap_pd:
                    p_pd = [str(snap_dir_case / f"{_merged_base}__merged__pwr_density.png")]
                    snap_args.append(
                        (str(out_vtp), str(out_vtp), p_pd,
                         ARRAY_NAME, True, stem, mult_factor) + _extra)
                    snap_str = p_pd[0]
                if snap_tp:
                    p_tp = [str(snap_dir_case / f"{_merged_base}__merged__total_pwr.png")]
                    snap_args.append(
                        (str(out_vtp), str(out_vtp), p_tp,
                         POWER_ARRAY, True, stem, mult_factor) + _extra)
                    if not snap_str:
                        snap_str = p_tp[0]

            del merged, running, running_pd, running_pwr

            snap_link = f'=HYPERLINK("{snap_str}","Open")' if snap_str else ""
            writer.writerow([
                _safe_label, output_name, cases_str,
                f"{_merged_filename}", n_loaded,
                f"{max_val:.6g}" if max_val is not None else "",
                f"{total_pwr:.6g}" if total_pwr is not None else "",
                f"{mult_factor:.6g}",
                cfg.get("pp_source", "original"),
                snap_link, str(out_vtp),
            ])

    log(f"\nMerge done: {done_count} slot(s) in "
        f"{time.perf_counter() - t0_total:.1f}s")
    # ── Phase 2: Snapshots ────────────────────────────────────────────────────
    if snap_args:
        n_snap         = len(snap_args)
        n_snap_workers = min(6, n_snap)
        log(f"\nSaving {n_snap} snapshot(s) ({n_snap_workers} render process(es))...")
        t0 = time.perf_counter()
        try:
            # Pool workers are fresh processes with their own stdout.
            with multiprocessing.Pool(processes=n_snap_workers,
                                      initializer=configure_stdio) as pool:
                for done, result in enumerate(
                        pool.imap_unordered(_render_subprocess, snap_args), 1):
                    if stopped():
                        pool.terminate()
                        break
                    if isinstance(result, Exception):
                        log(f"  [{done}/{n_snap}] ERROR: {result}")
                        continue
                    lbl, elapsed = result
                    log(f"  [{done}/{n_snap}] {lbl}  ({elapsed:.1f}s)")
        except Exception as exc:
            log(f"  [ERROR] Snapshot pool: {exc}")
        log(f"  Snapshots done in {time.perf_counter() - t0:.1f}s")
    elif save_snaps:
        log("\nNo snapshots queued (no files processed).")

    log("\n" + "=" * 80)
    if stopped():
        log("STOPPED by user.")
    else:
        log(f"Post-processing complete. CSV saved to:\n  {csv_path}")


def run_component_worst_case_merge(
        cfg: dict,
        log,
        stop_event: threading.Event | None = None,
) -> None:
    """Assembly merge: ONE user-picked scenario file per component, appended
    together (vtkAppendPolyData) into a SINGLE combined VTK file.

    This is intentionally a completely separate code path from
    run_post_processing() above:
      - run_post_processing() cell-wise MAX-merges multiple scenario files of
        the SAME component (one output file per component, per group).
      - run_component_worst_case_merge() takes exactly one already-chosen file
        per component (different components, potentially different meshes)
        and concatenates their geometry/arrays into one assembled mesh. No
        per-cell maximum is computed here — each component keeps the raw
        values from its single selected scenario. The same scenario file may
        be selected for more than one component.

    cfg keys:
      component_selection: {component_name: file_path}
      assembly_name:        str, used for output folder/filename
      output_folder:        str
      merge_pd / merge_pwr: bool, which cell arrays to keep in the output
    """

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    selection: dict = cfg.get("component_selection", {}) or {}
    assembly_name = str(cfg.get("assembly_name", "assembly")).strip() or "assembly"
    out_root  = cfg.get("output_folder", "")
    merge_pd  = bool(cfg.get("merge_pd",  True))
    merge_pwr = bool(cfg.get("merge_pwr", True))

    if not selection:
        log("No components selected for the assembly merge.")
        return

    script_dir = Path(__file__).resolve().parent.parent.parent
    out_dir    = Path(out_root) if out_root else script_dir / "output"
    pp_dir     = out_dir / "post_processed"
    csv_dir    = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    _ts       = time.strftime("%Y-%m-%d_%H-%M-%S")
    csv_path  = csv_dir / f"assembly_merge_batch_{_ts}.csv"

    import re as _re
    _safe_name = _re.sub(r'[\\/:*?"<>|]', "_", assembly_name).strip() or "assembly"
    out_subdir = pp_dir / "assembly" / _safe_name
    out_subdir.mkdir(parents=True, exist_ok=True)
    out_vtp    = out_subdir / f"assembly_{_safe_name}.vtp"

    log(f"Assembly merge: {len(selection)} component(s) \u2192 single VTK")
    log(f"  Merge arrays: "
        f"{'Power Density' if merge_pd else ''}"
        f"{' + ' if merge_pd and merge_pwr else ''}"
        f"{'Total Power' if merge_pwr else ''}")
    log("=" * 80)

    t0 = time.perf_counter()
    append_filter = vtk.vtkAppendPolyData()
    rows: list[list] = []
    n_added = 0

    for comp_name, fp_str in sorted(selection.items()):
        if stopped():
            break
        fp = Path(fp_str)
        if not fp.is_file():
            log(f"  [SKIP] {comp_name}: file not found \u2014 {fp}")
            continue
        try:
            pd = read_vtp(str(fp))
        except Exception as exc:
            log(f"  [ERROR] {comp_name}: {exc}")
            continue

        cd = pd.GetCellData()
        arr_pd  = cd.GetArray(ARRAY_NAME)  if merge_pd  else None
        arr_pwr = cd.GetArray(POWER_ARRAY) if merge_pwr else None

        if merge_pd and arr_pd is None:
            log(f"  [WARN] {comp_name}: '{ARRAY_NAME}' not found in {fp.name}.")
        if merge_pwr and arr_pwr is None:
            log(f"  [WARN] {comp_name}: '{POWER_ARRAY}' not found in {fp.name}.")

        # Drop arrays the user didn't ask to keep so vtkAppendPolyData doesn't
        # have to reconcile mismatched array sets across components.
        if not merge_pd and cd.GetArray(ARRAY_NAME) is not None:
            cd.RemoveArray(ARRAY_NAME)
        if not merge_pwr and cd.GetArray(POWER_ARRAY) is not None:
            cd.RemoveArray(POWER_ARRAY)

        max_pd  = float(vtk_to_numpy(arr_pd).max())  if arr_pd  is not None else None
        max_pwr = float(vtk_to_numpy(arr_pwr).max()) if arr_pwr is not None else None

        append_filter.AddInputData(pd)
        n_added += 1
        log(f"  + [{comp_name}]  {fp}")
        rows.append([comp_name, fp.name, str(fp),
                     f"{max_pd:.6g}"  if max_pd  is not None else "",
                     f"{max_pwr:.6g}" if max_pwr is not None else ""])

    if n_added == 0:
        log("No valid component files found \u2014 nothing to merge.")
        return

    if stopped():
        log("STOPPED by user.")
        return

    append_filter.Update()
    merged = append_filter.GetOutput()
    _write_vtp(merged, out_vtp)
    del merged

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["component", "filename", "source_path",
                         "max_power_density", "max_total_power"])
        writer.writerows(rows)

    elapsed = time.perf_counter() - t0
    log("=" * 80)
    log(f"Assembly merge complete: {n_added}/{len(selection)} component(s) "
        f"in {elapsed:.1f}s")
    log(f"  Output VTP: {out_vtp}")
    log(f"  CSV saved to: {csv_path}")


# ── Sum merge ─────────────────────────────────────────────────────────────────
# Cell-array families written by BeamOnTarget. Each family can carry a species
# suffix ("" for the all-species total, "_H-", "_H0", "_H+"). For one family
# member, with P = power, S2 = sum of squared macro-particle powers and A the
# cell area, the upstream RAW results_*.vtp satisfy:
#   Power_Density_W_m2        = P / A
#   Deposited_Power_StdErr_W  = sqrt(S2)
#   Power_Density_StdErr_W_m2 = StdErr / A
#   Relative_Error            = StdErr / P   (0 where P == 0)
# P and S2 are additive over independent runs; everything else is derived.
_SUM_P    = POWER_ARRAY                    # "Deposited_Power_W"
_SUM_S2   = "Deposited_Power_SumSq_W2"
_SUM_SE   = "Deposited_Power_StdErr_W"
_SUM_DSE  = "Power_Density_StdErr_W_m2"
_SUM_RE   = "Relative_Error"
_SUM_FAMILIES = (_SUM_P, ARRAY_NAME, _SUM_S2, _SUM_SE, _SUM_DSE, _SUM_RE)


def _power_suffixes(cd: vtk.vtkCellData) -> set[str]:
    """Species suffixes present in *cd*, e.g. {"", "_H-", "_H0", "_H+"}."""
    out: set[str] = set()
    for i in range(cd.GetNumberOfArrays()):
        name = cd.GetArrayName(i) or ""
        if name == _SUM_P or name.startswith(_SUM_P + "_"):
            out.add(name[len(_SUM_P):])
    return out


def _cell_areas(pd: vtk.vtkPolyData) -> np.ndarray:
    f = vtk.vtkCellSizeFilter()
    f.SetInputData(pd)
    f.ComputeAreaOn()
    f.ComputeLengthOff()
    f.ComputeVolumeOff()
    f.ComputeVertexCountOff()
    f.Update()
    return vtk_to_numpy(f.GetOutput().GetCellData().GetArray("Area")).astype(np.float64)


# Point coordinates may differ by float round-off (e.g. after a coordinate
# transform was applied to each run separately). Accept deviations up to this
# fraction of the bounding-box diagonal — ~17 µm on a 1.7 m part, well below a
# typical ~2 mm cell edge — as long as the cell connectivity is identical.
_MESH_REL_TOL = 1e-5


def _mesh_mismatch(pd: vtk.vtkPolyData, ref: vtk.vtkPolyData,
                   pts: np.ndarray, pts_ref: np.ndarray) -> str:
    """"" if *pd* is the same mesh as *ref* (same cells, same connectivity,
    points equal within round-off), otherwise a short reason."""
    if pd.GetNumberOfCells() != ref.GetNumberOfCells():
        return f"{pd.GetNumberOfCells()} vs {ref.GetNumberOfCells()} cells"
    if pts.shape != pts_ref.shape:
        return f"{pd.GetNumberOfPoints()} vs {ref.GetNumberOfPoints()} points"
    for kind in ("GetVerts", "GetLines", "GetPolys", "GetStrips"):
        a, b = getattr(pd, kind)(), getattr(ref, kind)()
        if not (np.array_equal(vtk_to_numpy(a.GetOffsetsArray()),
                               vtk_to_numpy(b.GetOffsetsArray()))
                and np.array_equal(vtk_to_numpy(a.GetConnectivityArray()),
                                   vtk_to_numpy(b.GetConnectivityArray()))):
            return "different cell connectivity"
    if np.array_equal(pts, pts_ref):
        return ""
    lo, hi = pts_ref.min(axis=0), pts_ref.max(axis=0)
    tol = _MESH_REL_TOL * float(np.linalg.norm(hi - lo))
    dev = float(np.abs(pts.astype(np.float64) - pts_ref).max())
    if dev > tol:
        return f"points differ by up to {dev:.3g}, tolerance {tol:.3g}"
    return ""


def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    out = np.zeros_like(num)
    np.divide(num, den, out=out, where=den > 0)
    return out


def run_sum_merge(
        cfg: dict,
        log,
        stop_event: threading.Event | None = None,
) -> None:
    """Cell-wise SUM of the same scenario/component across several output folders.

    Unlike run_post_processing() (per-cell maximum), this adds the deposited
    power of independent runs — e.g. a beam run and a separate ray run of the
    same scenario — so every cell carries the cumulative load. Power density is
    neither summed nor maxed: it is recomputed from the summed power and the
    cell area, so the reported maximum is the true maximum of the combined
    load. Statistical errors are combined in quadrature via the additive sum
    of squares.

    Each source may carry a factor f (e.g. to weight a ray run): its power is
    multiplied by f and its sum of squares by f², so the error bar scales with
    it. Unlike the snapshot factor elsewhere, this factor IS baked into the
    written VTP, because it changes what the sum means.

    The output mirrors a BeamOnTarget OUTPUT_* folder
    ({output_folder}/post_processed/summed/OUTPUT_<name>/<scenario>/results_<comp>.vtp)
    so it can be fed straight back into the Processing tab.

    cfg keys:
      sum_slots:     [{"scenario": key, "component": comp,
                       "sources": [[output_name, file_path, factor], ...]}, ...]
                     (factor is optional and defaults to 1.0)
      sum_name:      str, used for the OUTPUT_<name> folder
      output_folder: str
    """

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    slots: list[dict] = cfg.get("sum_slots", []) or []
    sum_name = str(cfg.get("sum_name", "SUM")).strip() or "SUM"
    out_root = cfg.get("output_folder", "")

    if not slots:
        log("Nothing to sum — select at least one scenario.")
        return

    import re as _re
    _safe_name = _re.sub(r'[\\/:*?"<>|]', "_", sum_name).strip() or "SUM"
    if not _safe_name.upper().startswith("OUTPUT_"):
        _safe_name = f"OUTPUT_{_safe_name}"

    script_dir = Path(__file__).resolve().parent.parent.parent
    out_dir    = Path(out_root) if out_root else script_dir / "output"
    sum_root   = out_dir / "post_processed" / "summed" / _safe_name
    csv_dir    = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    _ts       = time.strftime("%Y-%m-%d_%H-%M-%S")
    csv_path  = csv_dir / f"sum_merge_batch_{_ts}.csv"

    def _src_factor(src) -> float:
        return float(src[2]) if len(src) > 2 else 1.0

    out_factors: dict[str, float] = {}
    for s in slots:
        for src in s["sources"]:
            out_factors.setdefault(src[0], _src_factor(src))
    n_scen = len({s["scenario"] for s in slots})
    log(f"Sum merge: {n_scen} scenario(s), {len(slots)} component file(s)")
    log("  Summing outputs (factor applied to the power before adding):")
    for o, f in sorted(out_factors.items()):
        log(f"    ×{f:.6g}  {o}")
    log(f"  {_SUM_P} is summed; {ARRAY_NAME} is recomputed as summed power / cell area.")
    log(f"  Output: {sum_root}")
    log("=" * 80)

    t0_total = time.perf_counter()
    n_done = n_single = n_failed = 0
    ordered = sorted(slots, key=lambda s: (s["scenario"], s["component"]))

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario", "component", "n_sources", "sources",
                         "factor_per_source",
                         "total_power_per_source", "total_power_sum",
                         "max_pd_per_source", "max_pd_sum", "vtp_path"])

        for i_slot, slot in enumerate(ordered, 1):
            if stopped():
                break
            scen, comp, sources = slot["scenario"], slot["component"], slot["sources"]
            t0 = time.perf_counter()
            log(f"  [{i_slot}/{len(ordered)}] {scen} / {comp}  ({len(sources)} source(s))")

            geom_pd  = None
            n_cells  = 0
            pts_ref  = None
            sums: dict[str, dict[str, np.ndarray]] = {}   # {suffix: {"P": .., "S2": ..}}
            common_sfx: set[str] | None = None
            has_s2: dict[str, bool] = {}
            src_names: list[str] = []
            src_tp: list[float] = []
            src_f: list[float] = []
            src_maxpd: list[float] = []
            failed = False

            for src in sources:
                out_name, fp, fac = src[0], src[1], _src_factor(src)
                if stopped():
                    break
                try:
                    pd = read_vtp(fp)
                except Exception as exc:
                    log(f"    [ERROR] {out_name}: {exc}")
                    failed = True
                    break
                cd = pd.GetCellData()
                if cd.GetArray(_SUM_P) is None:
                    log(f"    [ERROR] {out_name}: '{_SUM_P}' not found in {Path(fp).name}")
                    failed = True
                    break
                pts = vtk_to_numpy(pd.GetPoints().GetData())
                if geom_pd is None:
                    geom_pd, n_cells, pts_ref = pd, pd.GetNumberOfCells(), pts
                else:
                    mesh_err = _mesh_mismatch(pd, geom_pd, pts, pts_ref)
                    if mesh_err:
                        log(f"    [ERROR] {out_name}: mesh differs from {src_names[0]} "
                            f"({mesh_err}) — cannot sum cell-wise.")
                        failed = True
                        break
                    if not np.array_equal(pts, pts_ref):
                        log(f"    [NOTE] {out_name}: point coordinates differ from "
                            f"{src_names[0]} by up to "
                            f"{np.abs(pts.astype(np.float64) - pts_ref).max():.3g} "
                            "(round-off, within tolerance) — geometry of the first "
                            "source is kept.")

                sfx_here = _power_suffixes(cd)
                common_sfx = sfx_here if common_sfx is None else (common_sfx & sfx_here)
                for sfx in sfx_here:
                    acc = sums.setdefault(sfx, {"P": np.zeros(n_cells), "S2": np.zeros(n_cells)})
                    acc["P"] += fac * vtk_to_numpy(cd.GetArray(_SUM_P + sfx)).astype(np.float64)
                    s2_arr = cd.GetArray(_SUM_S2 + sfx)
                    if s2_arr is not None:
                        acc["S2"] += fac * fac * vtk_to_numpy(s2_arr).astype(np.float64)
                    has_s2[sfx] = has_s2.get(sfx, True) and s2_arr is not None

                d_arr = cd.GetArray(ARRAY_NAME)
                src_names.append(out_name)
                src_f.append(fac)
                src_tp.append(float(vtk_to_numpy(cd.GetArray(_SUM_P)).astype(np.float64).sum()))
                src_maxpd.append(float(vtk_to_numpy(d_arr).max()) if d_arr is not None
                                 else float("nan"))
                _fac_str = f"  ×{fac:.6g} → {fac * src_tp[-1]:.6g} W" if fac != 1.0 else ""
                log(f"    + [{out_name}]  P={src_tp[-1]:.6g} W{_fac_str}  "
                    f"maxPD={src_maxpd[-1]:.6g} W/m²  {fp}")
                if pd is not geom_pd:
                    del pd

            if stopped():
                break
            if failed or geom_pd is None:
                log("    └─ skipped.")
                n_failed += 1
                continue

            # A species family missing from any source would hold only part of
            # the load — drop it rather than write a misleading partial sum.
            common_sfx = common_sfx or set()
            dropped = sorted(set(sums) - common_sfx)
            if dropped:
                log(f"    [NOTE] species arrays {', '.join(s.lstrip('_') for s in dropped)} "
                    "not present in every source — dropped from the output.")

            areas  = _cell_areas(geom_pd)
            merged = vtk.vtkPolyData()
            merged.DeepCopy(geom_pd)
            del geom_pd
            mcd = merged.GetCellData()
            for name in [mcd.GetArrayName(k) for k in range(mcd.GetNumberOfArrays())]:
                if name and any(name == b or name.startswith(b + "_") for b in _SUM_FAMILIES):
                    mcd.RemoveArray(name)

            def _put(arr: np.ndarray, name: str) -> None:
                a = numpy_to_vtk(arr.astype(np.float32), deep=True)
                a.SetName(name)
                mcd.AddArray(a)

            for sfx in sorted(common_sfx):
                p = sums[sfx]["P"]
                _put(p, _SUM_P + sfx)
                _put(_safe_div(p, areas), ARRAY_NAME + sfx)
                if has_s2.get(sfx):
                    se = np.sqrt(sums[sfx]["S2"])
                    _put(sums[sfx]["S2"], _SUM_S2 + sfx)
                    _put(se, _SUM_SE + sfx)
                    _put(_safe_div(se, areas), _SUM_DSE + sfx)
                    _put(_safe_div(se, p), _SUM_RE + sfx)
            mcd.SetActiveScalars(ARRAY_NAME)

            p_sum = sums[""]["P"]
            lost  = (p_sum > 0) & (areas <= 0)
            if lost.any():
                log(f"    [WARN] {int(lost.sum())} zero-area cell(s) carry power "
                    f"({p_sum[lost].sum():.4g} W) — density set to 0 there.")
            tp_sum    = float(p_sum.sum())
            maxpd_sum = float(_safe_div(p_sum, areas).max()) if n_cells else 0.0

            out_sub = sum_root / Path(_re.sub(r'[:*?"<>|]', "_", scen))
            out_sub.mkdir(parents=True, exist_ok=True)
            out_vtp = out_sub / f"results_{comp}.vtp"
            _write_vtp(merged, out_vtp)
            del merged, sums

            if len(src_names) < 2:
                n_single += 1
                log(f"    [WARN] only one source ({src_names[0]}) — nothing added to it.")
            log(f"    └─ P={tp_sum:.6g} W  maxPD={maxpd_sum:.6g} W/m²  "
                f"({time.perf_counter() - t0:.1f}s)")
            n_done += 1

            writer.writerow([
                scen, comp, len(src_names), "; ".join(src_names),
                "; ".join(f"{v:.6g}" for v in src_f),
                "; ".join(f"{v:.6g}" for v in src_tp), f"{tp_sum:.6g}",
                "; ".join(f"{v:.6g}" for v in src_maxpd), f"{maxpd_sum:.6g}",
                str(out_vtp),
            ])

    log("=" * 80)
    if stopped():
        log("STOPPED by user.")
    log(f"Sum merge: {n_done} file(s) written in {time.perf_counter() - t0_total:.1f}s"
        + (f", {n_single} with a single source" if n_single else "")
        + (f", {n_failed} skipped" if n_failed else ""))
    log(f"  Output folder: {sum_root}")
    log("  (Add it to the Processing tab to smooth / snapshot the summed results.)")
    log(f"  CSV saved to: {csv_path}")

