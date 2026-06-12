"""
modules/pipeline.py
-------------------
Batch processing pipeline: run_processing and run_transform.

run_processing  — load VTPs → smooth → write CSVs → render snapshots
run_transform   — extract cells → coordinate-transform → write CSVs
"""

from __future__ import annotations

import csv
import multiprocessing
import os
import sys
import time
import threading
from multiprocessing.pool import ThreadPool
from pathlib import Path

from modules.core.settings import (
    ARRAY_NAME, POWER_ARRAY, SMOOTH_PROXIMITY_RADIUS, SPIKE_SIGMA, _safe_float,
)
from modules.vtk.vtk_io import _write_vtp
from modules.core.path_utils import extract_case_scenario
from modules.vtk.smoothing import precompute_smooth_geometry
from modules.processing.workers import (
    _load_one_file, _smooth_one_file, _transform_one_file, _render_subprocess,
)


# ── Processing pipeline ───────────────────────────────────────────────────────

def run_processing(
        cfg: dict,
        log,
        stop_event: threading.Event | None = None,
) -> None:
    """Three-stage batch pipeline: load → smooth → snapshot/CSV."""

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    input_dirs       = cfg["input_dirs"]
    pattern          = cfg["pattern"]
    name_filter      = cfg.get("name_filter", "")
    components       = cfg.get("components", {})
    proximity_radius = _safe_float(
        cfg.get("proximity_radius", SMOOTH_PROXIMITY_RADIUS), SMOOTH_PROXIMITY_RADIUS
    )

    def _file_settings(filepath: Path) -> tuple:
        """Return (n_iter, snap_pwr_density, snap_total_pwr, mult_factor,
                   smooth_mode, spike_sigma)."""
        stem = filepath.stem.lower()
        for comp_name, comp_cfg in components.items():
            if comp_name == "(all)":
                continue
            if comp_name.lower() in stem:
                return (
                    int(comp_cfg.get("smooth_iterations", 1)),
                    bool(comp_cfg.get("save_power_density", True)),
                    bool(comp_cfg.get("save_total_power",   False)),
                    float(comp_cfg.get("mult_factor", 1.0)),
                    str(comp_cfg.get("smooth_mode", "edge")),
                    float(comp_cfg.get("spike_sigma", SPIKE_SIGMA)),
                )
        if "(all)" in components:
            c = components["(all)"]
            return (
                int(c.get("smooth_iterations", 1)),
                bool(c.get("save_power_density", True)),
                bool(c.get("save_total_power",   False)),
                float(c.get("mult_factor", 1.0)),
                str(c.get("smooth_mode", "edge")),
                float(c.get("spike_sigma", SPIKE_SIGMA)),
            )
        return (int(cfg.get("smooth_iterations", 1)), True, False, 1.0, "edge", SPIKE_SIGMA)

    # Expand OUTPUT_* folders
    expanded_dirs: list[Path] = []
    for d in input_dirs:
        p = Path(d)
        if p.is_dir() and p.name.upper().startswith("OUTPUT_"):
            subs = [s for s in sorted(p.iterdir()) if s.is_dir()]
            if subs:
                log(f"Expanding {p.name} into {len(subs)} subfolder(s).")
                expanded_dirs.extend(subs)
            else:
                log(f"[WARN] {p.name} folder is empty: {p}")
        else:
            expanded_dirs.append(p)
    input_dirs = expanded_dirs

    script_dir = Path(__file__).resolve().parent.parent
    out_dir    = Path(cfg["output_folder"]) if cfg["output_folder"] else script_dir / "output"
    os.makedirs(out_dir, exist_ok=True)

    snap_dir = out_dir / "snapshots"
    csv_path = out_dir / "max_comparison_batch.csv"
    pwr_path = out_dir / "total_power_batch.csv"

    total_files = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh, \
         open(pwr_path, "w", newline="", encoding="utf-8") as fh_pwr:
        writer     = csv.writer(fh)
        writer_pwr = csv.writer(fh_pwr)
        writer.writerow(["case", "scenario", "filename",
                         "max_before", "max_after", "delta", "discrepancy"])
        writer_pwr.writerow(["case", "scenario", "filename", "total_power_W"])

        # ── Collect all matching files ────────────────────────────────────────
        all_files: list[tuple] = []
        for input_folder in input_dirs:
            if stopped():
                break
            input_path = Path(input_folder)
            output_name, folder_case, _ = extract_case_scenario(str(input_path))
            files = sorted(input_path.rglob(pattern))
            if name_filter:
                terms = [t.strip().lower() for t in name_filter.split(",") if t.strip()]
                files = [f for f in files if any(t in f.stem.lower() for t in terms)]
            if not files:
                note = f" containing '{name_filter}'" if name_filter else ""
                log(f"[SKIP] No files matching '{pattern}'{note} in (or below):\n  {input_path}")
                continue
            log(f"  {folder_case}: {len(files)} file(s) found")
            for filepath in files:
                _, case, scenario = extract_case_scenario(str(filepath.parent))
                all_files.append((filepath, output_name, case, scenario))

        if not all_files:
            log("No files found.")
            return

        log(f"\nTotal: {len(all_files)} file(s) to process")
        log("=" * 80)

        # ── Stage 1: Load ─────────────────────────────────────────────────────
        n_all     = len(all_files)
        n_workers = min(os.cpu_count() or 4, n_all, 12)
        log(f"\n[1/3] Loading {n_all} VTP file(s) ({n_workers} workers)...")
        loaded: list[tuple] = []
        t0 = time.perf_counter()
        with ThreadPool(processes=n_workers) as pool:
            for done, result in enumerate(pool.imap_unordered(_load_one_file, all_files), 1):
                if stopped():
                    pool.terminate(); break
                if isinstance(result, Exception):
                    log(f"  [{done}/{n_all}] ERROR: {result}"); continue
                fp, on, c, s, pd, mv, tp = result
                if mv is None:
                    log(f"  [{done}/{n_all}] SKIP  {fp.name}  (no '{ARRAY_NAME}')"); continue
                loaded.append(result)
                log(f"  [{done}/{n_all}] {fp.name}  (elapsed: {time.perf_counter()-t0:.1f}s)")
        log(f"  Loading done: {len(loaded)} file(s) in {time.perf_counter()-t0:.1f}s")

        _snap_map  = {fp: _file_settings(fp) for fp, *_ in loaded}
        _power_map = {fp: tp for fp, _on, _c, _s, _pd, _mv, tp in loaded}

        # ── Geometry cache ────────────────────────────────────────────────────
        _geo_cache: dict[str, dict] = {}
        _needs_smooth = [(fp, *rest) for fp, *rest in loaded if _snap_map[fp][0] > 0]
        if _needs_smooth:
            log("\n  Pre-computing edge geometry per component...")
            seen: set = set()
            for fp, on_c, c_c, s_c, pd_c, *_ in _needs_smooth:
                comp = next(
                    (n for n in components if n != "(all)" and n.lower() in fp.stem.lower()),
                    "(all)",
                )
                if comp not in seen:
                    seen.add(comp)
                    t0g = time.perf_counter()
                    # Determine mode for this component from the first matching file.
                    comp_mode = _snap_map[fp][4]  # index 4 = smooth_mode
                    cache   = precompute_smooth_geometry(
                        pd_c,
                        proximity_radius=proximity_radius,
                        log_fn=log,
                        skip_edge_expansion=(comp_mode == "auto"),
                    )
                    n_ec    = cache.get("n_direct", 0) if cache else 0
                    n_ep    = len(cache.get("edge_pt_ids_arr", [])) if cache else 0
                    if comp_mode == "auto":
                        log(f"    [{comp}] {n_ep:,} edge points cached (cell flagging skipped)  "
                            f"({time.perf_counter()-t0g:.1f}s)  (from {fp.name})"
                            f"  [mode=auto]")
                    else:
                        log(f"    [{comp}] {n_ec:,} direct edge cells cached  "
                            f"({time.perf_counter()-t0g:.1f}s)  (from {fp.name})"
                            f"  [mode=edge]")
                    _geo_cache[comp] = cache

        def _geo_for(fp: Path) -> dict | None:
            for name in components:
                if name != "(all)" and name.lower() in fp.stem.lower():
                    return _geo_cache.get(name)
            return _geo_cache.get("(all)")

        # ── Stage 2: Smooth ───────────────────────────────────────────────────
        processed: list[tuple] = []
        n_loaded = len(loaded)
        log(f"\n[2/3] Processing {n_loaded} file(s) ({min(n_loaded, n_workers)} workers)...")
        smooth_args = [
            (fp, on, c, s, pd, mb, _snap_map[fp][0], stop_event, _geo_for(fp),
             _snap_map[fp][4], _snap_map[fp][5], proximity_radius)
            for fp, on, c, s, pd, mb, _tp in loaded
        ]
        _prev = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
        t0 = time.perf_counter()
        try:
            with ThreadPool(processes=min(n_loaded, n_workers)) as pool:
                for done, result in enumerate(
                        pool.imap_unordered(_smooth_one_file, smooth_args), 1):
                    if stopped():
                        pool.terminate(); break
                    if result is None:
                        continue
                    if isinstance(result, Exception):
                        log(f"  [{done}/{n_loaded}] ERROR: {result}"); continue
                    fp, on, c, s, pd, mb, sm, ma = result
                    processed.append(result)
                    ni   = _snap_map[fp][0]
                    mode = _snap_map[fp][4]
                    if ni > 0:
                        log(f"  [{done}/{n_loaded}] {fp.name}  "
                            f"(elapsed: {time.perf_counter()-t0:.1f}s)  "
                            f"before={mb:.4g}  after={ma:.4g}  "
                            f"({ni} iter, mode={mode})")
                    else:
                        log(f"  [{done}/{n_loaded}] {fp.name}  "
                            f"(elapsed: {time.perf_counter()-t0:.1f}s)  no smoothing")
        finally:
            sys.setswitchinterval(_prev)
        log(f"  Stage 2 done in {time.perf_counter()-t0:.1f}s")

        # ── Write CSVs ────────────────────────────────────────────────────────
        for item in processed:
            fp, on, c, s, _, mb, _, ma = item
            _, snap_pd, snap_tp, mult, *_ = _snap_map[fp]
            mbs = mb * mult; mas = ma * mult
            delta = abs(mas - mbs)
            writer.writerow([c, s, fp.name,
                             f"{mbs:.6g}", f"{mas:.6g}",
                             f"{delta:.6g}", "YES" if delta > 0.0 else "NO"])
            tp = _power_map.get(fp)
            if tp is not None:
                writer_pwr.writerow([c, s, fp.name, f"{tp * mult:.6g}"])
            total_files += 1

        # ── Stage 3: Snapshots ────────────────────────────────────────────────
        snap_items = [item for item in processed
                      if _snap_map[item[0]][1] or _snap_map[item[0]][2]]
        if snap_items:
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_proc_args: list[tuple] = []
            temp_vtp_files: list[Path] = []

            for fp, on, c, s, pd_orig, _, pd_smooth, _ in snap_items:
                n_iter_f, snap_pd, snap_tp, mult_f, *_ = _snap_map[fp]
                is_snap_only  = (n_iter_f == 0)
                stem          = fp.stem
                case_snap_dir = snap_dir / on / c / s
                case_snap_dir.mkdir(parents=True, exist_ok=True)

                smooth_path = None
                if not is_snap_only:
                    tmp = snap_dir / f"_tmp_smooth_{os.getpid()}_{len(temp_vtp_files)}.vtp"
                    log(f"  Writing temp VTP for {fp.name}...")
                    t_vtp = time.perf_counter()
                    _write_vtp(pd_smooth, tmp)
                    log(f"  Temp VTP written in {time.perf_counter()-t_vtp:.1f}s")
                    temp_vtp_files.append(tmp)
                    smooth_path = str(tmp)

                if snap_pd:
                    if is_snap_only:
                        paths_pd = [str(case_snap_dir / f"{s}__{stem}__pwr_density.png")]
                    else:
                        paths_pd = [
                            str(case_snap_dir / f"{s}__{stem}__pwr_density__before.png"),
                            str(case_snap_dir / f"{s}__{stem}__pwr_density__after.png"),
                        ]
                    snap_proc_args.append(
                        (str(fp), smooth_path, paths_pd, ARRAY_NAME, is_snap_only, stem, mult_f))

                if snap_tp:
                    if is_snap_only:
                        paths_tp = [str(case_snap_dir / f"{s}__{stem}__total_pwr.png")]
                    else:
                        paths_tp = [
                            str(case_snap_dir / f"{s}__{stem}__total_pwr__before.png"),
                            str(case_snap_dir / f"{s}__{stem}__total_pwr__after.png"),
                        ]
                    snap_proc_args.append(
                        (str(fp), smooth_path, paths_tp, POWER_ARRAY, is_snap_only, stem, mult_f))

            n_snap        = len(snap_proc_args)
            n_snap_workers = min(6, n_snap)
            log(f"\n[3/3] Saving {n_snap} snapshot(s) ({n_snap_workers} render process(es))...")
            t0 = time.perf_counter()
            try:
                with multiprocessing.Pool(processes=n_snap_workers) as pool:
                    for done, result in enumerate(
                            pool.imap_unordered(_render_subprocess, snap_proc_args), 1):
                        if stopped():
                            pool.terminate(); break
                        if isinstance(result, Exception):
                            log(f"  [{done}/{n_snap}] ERROR: {result}"); continue
                        lbl, elapsed = result
                        log(f"  [{done}/{n_snap}] {lbl}  ({elapsed:.1f}s)")
            finally:
                for tmp in temp_vtp_files:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
            log(f"  Snapshots done in {time.perf_counter()-t0:.1f}s")
        else:
            log("\n[3/3] Snapshots disabled")

    log("\n" + "=" * 80)
    if stopped():
        log(f"STOPPED by user after {total_files} file(s).")
    else:
        log(f"Processed {total_files} file(s) across {len(input_dirs)} folder(s).")
    log(f"CSV log saved to:\n  {csv_path}")
    log(f"Power CSV saved to:\n  {pwr_path}")


# ── Transform pipeline ────────────────────────────────────────────────────────

def run_transform(
        input_dirs: list,
        xfm_params: dict,
        output_folder: str,
        log,
        stop_event: threading.Event | None = None,
) -> None:
    """Extract per-cell data → coordinate transform → write CSVs."""

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    pattern     = xfm_params["pattern"]
    name_filter = xfm_params["name_filter"]

    expanded: list[Path] = []
    for d in input_dirs:
        p = Path(d)
        if p.is_dir() and p.name.upper().startswith("OUTPUT_"):
            subs = [s for s in sorted(p.iterdir()) if s.is_dir()]
            if subs:
                log(f"Expanding {p.name} into {len(subs)} subfolder(s).")
                expanded.extend(subs)
            else:
                log(f"[WARN] {p.name} is empty: {p}")
        else:
            expanded.append(p)

    script_dir = Path(__file__).resolve().parent.parent
    out_root   = Path(output_folder) if output_folder else script_dir / "output"

    all_xfm_args: list[tuple] = []
    for folder in expanded:
        if stopped():
            break
        folder = Path(folder)
        output_name, folder_case, folder_scenario = extract_case_scenario(str(folder))
        files = sorted(folder.rglob(pattern))
        if name_filter:
            terms = [t.strip().lower() for t in name_filter.split(",") if t.strip()]
            files = [f for f in files if any(t in f.stem.lower() for t in terms)]
        if not files:
            note = f" containing '{name_filter}'" if name_filter else ""
            log(f"[SKIP] No files matching '{pattern}'{note} in:\n  {folder}")
            continue
        log(f"  {folder_case}/{folder_scenario}: {len(files)} file(s)")
        for filepath in files:
            _, case, scenario = extract_case_scenario(str(filepath.parent))
            dest_dir = out_root / "transformed" / output_name / case
            all_xfm_args.append((filepath, dest_dir, scenario, xfm_params))

    n_total   = len(all_xfm_args)
    n_workers = min(os.cpu_count() or 4, n_total, 12)
    log(f"\nTransforming {n_total} file(s) total ({n_workers} workers)...")
    log("=" * 80)

    total = 0
    with ThreadPool(processes=n_workers) as pool:
        for done, result in enumerate(
                pool.imap_unordered(_transform_one_file, all_xfm_args), 1):
            if stopped():
                pool.terminate(); break
            if isinstance(result, Exception):
                log(f"  [{done}/{n_total}] ERROR: {result}"); continue
            filepath, out_path, _ = result
            log(f"  [{done}/{n_total}] {filepath.name} -> {out_path.name}")
            total += 1

    log("\n" + "=" * 80)
    if stopped():
        log(f"STOPPED after {total} file(s) transformed.")
    else:
        log(f"Transformed {total} file(s).")
    log(f"Output root: {out_root / 'transformed'}")
