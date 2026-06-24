"""
modules/processing/post_pipeline.py
------------------------------------
Post-processing pipeline: cell-wise maximum merge across selected cases.

run_post_processing(cfg, log, stop_event)
  - Groups VTP files from selected scenario dirs by (output_name, case, stem)
  - Computes per-cell maximum across all case files in each group (running max,
    memory-efficient — only one mesh in RAM at a time beyond the geometry copy)
  - Applies mult_factor to merged arrays if source=original (skipped otherwise)
  - Writes merged VTPs to {output_folder}/post_processed/{output_name}/{case}/
  - Optionally renders snapshots via the same _render_subprocess pool as pipeline.py
  - Writes post_processed_batch.csv
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
    merge_pd    = bool(cfg.get("merge_pd",   True))   # merge Power_Density_W_m2
    merge_pwr   = bool(cfg.get("merge_pwr",  True))   # merge Deposited_Power_W
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
    log(f"  Merge arrays: "
        f"{'Power Density' if merge_pd else ''}"
        f"{' + ' if merge_pd and merge_pwr else ''}"
        f"{'Total Power' if merge_pwr else ''}")
    log(f"  Groups: {[group_labels.get(k, k) for k in groups_cfg.keys()]}")
    if not merge_pd:
        log("  [WARN] Power Density (Power_Density_W_m2) merge is OFF — "
            "the output VTP will inherit that array unchanged from whichever "
            "file is loaded first (arbitrary order). Values are NOT a per-cell max.")
    if not merge_pwr:
        log("  [WARN] Total Power (Deposited_Power_W) merge is OFF — "
            "same caveat: values come from the first loaded file only.")

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

            # ── Running per-cell maximum (memory-efficient: O(1) polydata in RAM)
            geom_pd      = None    # vtkPolyData — geometry from first file, retained
            running_pd   = None    # float64 ndarray — running max of ARRAY_NAME
            running_pwr  = None    # float64 ndarray — running max of POWER_ARRAY
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

                arr_pd_vtk  = pd.GetCellData().GetArray(ARRAY_NAME)  if merge_pd  else None
                arr_pwr_vtk = pd.GetCellData().GetArray(POWER_ARRAY) if merge_pwr else None

                if merge_pd and arr_pd_vtk is None:
                    log(f"  [SKIP] {fp.name}: '{ARRAY_NAME}' not found.")
                    if geom_pd is None:
                        del pd
                    continue

                arr_pd  = (vtk_to_numpy(arr_pd_vtk).astype(np.float64)
                           if arr_pd_vtk is not None else None)
                arr_pwr = (vtk_to_numpy(arr_pwr_vtk).astype(np.float64)
                           if arr_pwr_vtk is not None else None)

                if geom_pd is None:
                    geom_pd     = pd
                    running_pd  = arr_pd.copy()  if arr_pd  is not None else None
                    running_pwr = arr_pwr.copy() if arr_pwr is not None else None
                else:
                    if running_pd is not None and arr_pd is not None:
                        np.maximum(running_pd, arr_pd, out=running_pd)
                    if running_pwr is not None and arr_pwr is not None:
                        np.maximum(running_pwr, arr_pwr, out=running_pwr)
                    del pd
                merged_cases.append(case_name)
                n_loaded += 1

            if geom_pd is None or (merge_pd and running_pd is None):
                log(f"  [{done_count}/{total_slots}] [{group_name}] {stem}: "
                    "no valid files — skipped.")
                continue

            # VTPs always store raw values — snapshot factor is applied only during rendering.
            max_val   = float(running_pd.max())  if running_pd  is not None else None
            max_pwr_v = float(running_pwr.max()) if running_pwr is not None else None
            total_pwr = float(running_pwr.sum()) if running_pwr is not None else None

            # ── Build merged vtkPolyData ──────────────────────────────────────
            merged = vtk.vtkPolyData()
            merged.DeepCopy(geom_pd)
            del geom_pd

            def _replace_arr(pd_obj: vtk.vtkPolyData,
                             arr_np: np.ndarray, name: str) -> None:
                new_a = numpy_to_vtk(arr_np, deep=True)
                new_a.SetName(name)
                cd = pd_obj.GetCellData()
                cd.RemoveArray(name)
                cd.AddArray(new_a)
                cd.SetActiveScalars(name)

            _replace_arr(merged, running_pd,  ARRAY_NAME)  if running_pd  is not None else None
            _replace_arr(merged, running_pwr, POWER_ARRAY) if running_pwr is not None else None

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
            # Strip source prefix from stem for clean merged name
            _bare_stem = stem
            for _pfx in ("post_smooth_results_", "post_smooth__",
                         "smoothed_results_", "merged_results_", "merged__"):
                if _bare_stem.startswith(_pfx):
                    _bare_stem = _bare_stem[len(_pfx):]
                    break
            if _bare_stem.startswith("results_"):
                _bare_stem = _bare_stem[len("results_"):]
            _merged_filename = f"merged_results_{_bare_stem}.vtp"
            out_subdir = pp_dir / output_name / _safe_label
            out_subdir.mkdir(parents=True, exist_ok=True)
            out_vtp = out_subdir / _merged_filename
            _write_vtp(merged, out_vtp)

            elapsed = time.perf_counter() - t0
            cases_str = "; ".join(merged_cases)
            log(f"    └─ merged {n_loaded} case(s) in {elapsed:.1f}s  "
                f"max={max_val:.4g}  total_pwr={total_pwr:.4g}")

            # ── Queue snapshot args ───────────────────────────────────────────
            snap_str = ""
            if save_snaps and pre_cam is not None:
                snap_dir_case = snap_dir / output_name / _safe_label
                snap_dir_case.mkdir(parents=True, exist_ok=True)
                _extra = (pre_cam, pre_cam, max_pwr_v, max_pwr_v, total_pwr)
                if snap_pd:
                    p_pd = [str(snap_dir_case / f"merged_results_{_bare_stem}__merged__pwr_density.png")]
                    snap_args.append(
                        (str(out_vtp), str(out_vtp), p_pd,
                         ARRAY_NAME, True, stem, mult_factor) + _extra)
                    snap_str = p_pd[0]
                if snap_tp:
                    p_tp = [str(snap_dir_case / f"merged_results_{_bare_stem}__merged__total_pwr.png")]
                    snap_args.append(
                        (str(out_vtp), str(out_vtp), p_tp,
                         POWER_ARRAY, True, stem, mult_factor) + _extra)
                    if not snap_str:
                        snap_str = p_tp[0]

            del merged, running_pd, running_pwr

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
            with multiprocessing.Pool(processes=n_snap_workers) as pool:
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
