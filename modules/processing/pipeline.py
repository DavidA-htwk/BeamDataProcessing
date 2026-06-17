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
    ARRAY_NAME, POWER_ARRAY, SMOOTH_PROXIMITY_RADIUS, SPIKE_SIGMA, SPIKE_RATIO, _safe_float,
)
from modules.vtk.vtk_io import _write_vtp, read_vtp
from modules.core.path_utils import extract_case_scenario
from modules.vtk.smoothing import precompute_smooth_geometry
from modules.processing.workers import (
    _load_one_file, _smooth_one_file, _transform_one_file, _render_subprocess,
    _load_smooth_write_one_file,
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
                   smooth_mode, spike_sigma, proximity_radius, smooth_spikes,
                   spike_ratio, save_smooth_vtp)."""
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
                    str(comp_cfg.get("smooth_mode", "auto")),
                    float(comp_cfg.get("spike_sigma", SPIKE_SIGMA)),
                    float(comp_cfg.get("proximity_radius", proximity_radius)),
                    bool(comp_cfg.get("smooth_spikes", False)),
                    float(comp_cfg.get("spike_ratio", SPIKE_RATIO)),
                    bool(comp_cfg.get("save_smooth_vtp", False)),
                )
        if "(all)" in components:
            c = components["(all)"]
            return (
                int(c.get("smooth_iterations", 1)),
                bool(c.get("save_power_density", True)),
                bool(c.get("save_total_power",   False)),
                float(c.get("mult_factor", 1.0)),
                str(c.get("smooth_mode", "auto")),
                float(c.get("spike_sigma", SPIKE_SIGMA)),
                float(c.get("proximity_radius", proximity_radius)),
                bool(c.get("smooth_spikes", False)),
                float(c.get("spike_ratio", SPIKE_RATIO)),
                bool(c.get("save_smooth_vtp", False)),
            )
        return (int(cfg.get("smooth_iterations", 1)), True, False, 1.0, "auto",
                SPIKE_SIGMA, proximity_radius, False, SPIKE_RATIO, False)

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

    snap_dir   = out_dir / "snapshots"
    smooth_dir = out_dir / "post_smoothed"
    csv_path   = out_dir / "max_comparison_batch.csv"

    # Purge any _tmp_smooth_* VTP files left by a previous crashed/stopped run.
    if snap_dir.exists():
        for _stale in snap_dir.glob("_tmp_smooth_*.vtp"):
            try:
                _stale.unlink(missing_ok=True)
            except Exception:
                pass

    total_files = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["case", "scenario", "filename",
                         "max_before", "max_after", "delta", "discrepancy",
                         "total_power_before", "total_power_after",
                         "total_power_delta", "total_power_discrepancy",
                         "snapshot", "paraview", "post_smoothed_vtp"])

        # ── Phase 0: Collect all matching file paths (no loading) ────────────
        all_meta: list[tuple] = []   # (filepath, output_name, case, scenario)
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
                all_meta.append((filepath, output_name, case, scenario))

        if not all_meta:
            log("No files found.")
            return

        n_all     = len(all_meta)
        n_workers = min(os.cpu_count() or 4, n_all, 6)
        log(f"\nTotal: {n_all} file(s) to process")
        log("=" * 80)

        _snap_map = {fp: _file_settings(fp) for fp, *_ in all_meta}

        # ── Settings summary (once per run) ──────────────────────────────────
        log("\n  Smoothing settings per component:")
        _seen_comps: dict[str, tuple] = {}
        for _fp, *_ in all_meta:
            _comp = next(
                (n for n in components if n != "(all)" and n.lower() in _fp.stem.lower()),
                "(all)",
            )
            if _comp not in _seen_comps:
                _seen_comps[_comp] = _snap_map[_fp]
        for _comp, _s in _seen_comps.items():
            (_ni, _spd, _stp, _mf, _sm, _ss, _pr, _spk, _sr, _sv) = _s
            _spk_str  = "ON" if _spk else "off"
            _sv_str   = "ON" if _sv  else "off"
            _snap_str = ("pwr-density" if _spd and not _stp
                         else "total-pwr" if not _spd and _stp
                         else "both" if _spd and _stp else "none")
            if _ni == 0:
                log(f"    [{_comp}] iterations=0 (no smoothing)  "
                    f"mult={_mf}  snapshots={_snap_str}")
            elif _sm == "auto":
                _ratio_str = f"  spike_ratio={_sr}" if _sr > 0.0 else ""
                log(f"    [{_comp}] mode=auto  iter={_ni}  "
                    f"sigma={_ss}{_ratio_str}  "
                    f"spike_smooth={_spk_str}  "
                    f"save_post_smooth={_sv_str}  "
                    f"mult={_mf}  snapshots={_snap_str}")
            else:
                log(f"    [{_comp}] mode=edge  iter={_ni}  "
                    f"proximity={_pr}  "
                    f"save_post_smooth={_sv_str}  "
                    f"mult={_mf}  snapshots={_snap_str}")
        log("")

        # ── Phase 1: Geo-cache precompute (load one file per component, free) ─
        # Polydata is created, used to build the cache, then immediately deleted.
        # The cache itself holds only numpy arrays (~1 GB per component) and stays
        # in RAM for the full run; no VTK objects are retained.
        _geo_cache: dict[str, dict] = {}
        _needs_smooth = [(fp, on, c, s) for fp, on, c, s in all_meta
                         if _snap_map[fp][0] > 0]
        if _needs_smooth:
            log("\n  Pre-computing edge geometry per component...")
            seen: set = set()
            for fp, on_c, c_c, s_c in _needs_smooth:
                comp = next(
                    (n for n in components if n != "(all)" and n.lower() in fp.stem.lower()),
                    "(all)",
                )
                if comp not in seen:
                    seen.add(comp)
                    t0g       = time.perf_counter()
                    comp_mode = _snap_map[fp][4]
                    comp_prox = _snap_map[fp][6]
                    pd_tmp    = read_vtp(str(fp))           # load for cache only
                    cache     = precompute_smooth_geometry(
                        pd_tmp,
                        proximity_radius=comp_prox,
                        log_fn=log,
                        skip_edge_expansion=(comp_mode == "auto"),
                    )
                    del pd_tmp                              # free immediately
                    n_ec = cache.get("n_direct", 0) if cache else 0
                    n_ep = len(cache.get("edge_pt_ids_arr", [])) if cache else 0
                    if comp_mode == "auto":
                        log(f"    [{comp}] {n_ep:,} edge points cached "
                            f"(cell flagging skipped)  "
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

        # ── Phase 2: Stream load → smooth → write temp VTP → free ────────────
        # Each worker loads its own file, smooths, writes the temp VTP if
        # snapshots are needed, then frees both polydata objects before returning.
        # At any moment only n_workers meshes exist in RAM (inside the worker
        # threads), regardless of the total file count.
        pid = os.getpid()
        snap_dir.mkdir(parents=True, exist_ok=True)

        needs_snap_map = {fp: (_snap_map[fp][1] or _snap_map[fp][2])
                          for fp, *_ in all_meta}
        proc_args = [
            (fp, on, c, s,
             _snap_map[fp][0],            # n_iter
             stop_event,
             _geo_for(fp),                # geo cache (numpy arrays, shared read-only)
             _snap_map[fp][4],            # smooth_mode
             _snap_map[fp][5],            # spike_sigma
             _snap_map[fp][6],            # proximity_radius
             _snap_map[fp][7],            # smooth_spikes
             _snap_map[fp][8],            # spike_ratio
             needs_snap_map[fp],          # write temp VTP?
             str(snap_dir),
             pid,
             _snap_map[fp][9],            # save_smooth_vtp
             str(smooth_dir),             # permanent smooth VTP root
            )
            for fp, on, c, s in all_meta
        ]

        snap_proc_args: list[tuple] = []
        temp_vtp_files: list[Path]  = []

        log(f"\n[1/2] Processing {n_all} file(s) ({n_workers} workers)...")
        _prev = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
        t0 = time.perf_counter()
        try:
            with ThreadPool(processes=n_workers) as pool:
                for done, result in enumerate(
                        pool.imap_unordered(_load_smooth_write_one_file, proc_args), 1):
                    if stopped():
                        pool.terminate(); break
                    if result is None:
                        continue
                    if isinstance(result, Exception):
                        log(f"  [{done}/{n_all}] ERROR: {result}"); continue

                    fp, on, c, s, mb, ma, tp, tp_after, smooth_path, \
                    saved_smooth_path, \
                    pre_orig, pre_smooth, max_pwr_o, max_pwr_s = result
                    ni, snap_pd, snap_tp, mult, mode, *_ = _snap_map[fp]

                    # Write CSV rows immediately (no accumulation needed)
                    mbs   = mb * mult
                    mas   = ma * mult
                    delta = abs(mas - mbs)
                    # Build relative hyperlink path using backslashes.
                    # Excel passes relative paths to ShellExecute which uses the
                    # registered .png handler (Photos), not the browser.
                    # Backslashes are required — forward slashes cause Excel to
                    # treat the path as a URL fragment and open Edge instead.
                    # The path is relative to the CSV file (both in out_dir).
                    is_snap_only_csv = (ni == 0)
                    stem_csv = fp.stem
                    if snap_pd:
                        if is_snap_only_csv:
                            _snap_rel = f"snapshots\\{on}\\{c}\\{s}\\{s}__{stem_csv}__pwr_density.png"
                        else:
                            _snap_rel = f"snapshots\\{on}\\{c}\\{s}\\{s}__{stem_csv}__pwr_density__after.png"
                    elif snap_tp:
                        if is_snap_only_csv:
                            _snap_rel = f"snapshots\\{on}\\{c}\\{s}\\{s}__{stem_csv}__total_pwr.png"
                        else:
                            _snap_rel = f"snapshots\\{on}\\{c}\\{s}\\{s}__{stem_csv}__total_pwr__after.png"
                    else:
                        _snap_rel = ""
                    _snap_link = f'=HYPERLINK("{_snap_rel}","Open")' if _snap_rel else ""
                    # Plain absolute paths — copy-paste into Explorer address bar to open
                    _pv_path       = str(fp) if fp else ""
                    _smooth_vtp_path = saved_smooth_path if saved_smooth_path else ""
                    tpbs_str = f"{tp       * mult:.6g}" if tp       is not None else ""
                    tpas_str = f"{tp_after * mult:.6g}" if tp_after is not None else ""
                    if tp is not None and tp_after is not None:
                        tp_delta     = abs(tp_after * mult - tp * mult)
                        tp_delta_str = f"{tp_delta:.6g}"
                        tp_disc_str  = "YES" if tp_delta > 0.0 else "NO"
                    else:
                        tp_delta_str = ""
                        tp_disc_str  = ""
                    writer.writerow([c, s, fp.name,
                                     f"{mbs:.6g}", f"{mas:.6g}",
                                     f"{delta:.6g}", "YES" if delta > 0.0 else "NO",
                                     tpbs_str, tpas_str, tp_delta_str, tp_disc_str,
                                     _snap_link, _pv_path, _smooth_vtp_path])
                    total_files += 1

                    if ni > 0:
                        log(f"  [{done}/{n_all}] {fp.name}  "
                            f"(elapsed: {time.perf_counter()-t0:.1f}s)  "
                            f"before={mb:.4g}  after={ma:.4g}  "
                            f"({ni} iter, mode={mode})")
                    else:
                        log(f"  [{done}/{n_all}] {fp.name}  "
                            f"(elapsed: {time.perf_counter()-t0:.1f}s)  no smoothing")

                    # Queue snapshot args — paths + precomputed camera dicts only
                    if snap_pd or snap_tp:
                        is_snap_only  = (ni == 0)
                        stem          = fp.stem
                        case_snap_dir = snap_dir / on / c / s
                        case_snap_dir.mkdir(parents=True, exist_ok=True)
                        if smooth_path:
                            temp_vtp_files.append(Path(smooth_path))

                        # Common extra args for _render_subprocess
                        _snap_extra = (pre_orig, pre_smooth, max_pwr_o, max_pwr_s, tp)

                        if snap_pd:
                            if is_snap_only:
                                paths_pd = [str(case_snap_dir / f"{s}__{stem}__pwr_density.png")]
                            else:
                                paths_pd = [
                                    str(case_snap_dir / f"{s}__{stem}__pwr_density__before.png"),
                                    str(case_snap_dir / f"{s}__{stem}__pwr_density__after.png"),
                                ]
                            snap_proc_args.append(
                                (str(fp), smooth_path, paths_pd,
                                 ARRAY_NAME, is_snap_only, stem, mult) + _snap_extra)

                        if snap_tp:
                            if is_snap_only:
                                paths_tp = [str(case_snap_dir / f"{s}__{stem}__total_pwr.png")]
                            else:
                                paths_tp = [
                                    str(case_snap_dir / f"{s}__{stem}__total_pwr__before.png"),
                                    str(case_snap_dir / f"{s}__{stem}__total_pwr__after.png"),
                                ]
                            snap_proc_args.append(
                                (str(fp), smooth_path, paths_tp,
                                 POWER_ARRAY, is_snap_only, stem, mult) + _snap_extra)
        finally:
            sys.setswitchinterval(_prev)
        log(f"  Processing done in {time.perf_counter()-t0:.1f}s")

        # ── Phase 3: Snapshots (render subprocesses read from temp VTPs) ──────
        if snap_proc_args:
            n_snap         = len(snap_proc_args)
            n_snap_workers = min(6, n_snap)
            log(f"\n[2/2] Saving {n_snap} snapshot(s) "
                f"({n_snap_workers} render process(es))...")
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
            log("\n[2/2] Snapshots disabled")
            # Still clean up any temp VTPs written by workers (e.g. if all
            # snapshot flags were off but needs_snap_map had True entries).
            for tmp in temp_vtp_files:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    log("\n" + "=" * 80)
    if stopped():
        log(f"STOPPED by user after {total_files} file(s).")
    else:
        log(f"Processed {total_files} file(s) across {len(input_dirs)} folder(s).")
    log(f"CSV log saved to:\n  {csv_path}")


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
