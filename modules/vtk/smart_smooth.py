"""
modules/vtk/smart_smooth.py
---------------------------
Smart Smooth AUTO: per-candidate local classification and smoothing.

Public function:
    smart_smooth_auto  — find outlier candidates, classify each as edge/spike
                         using a local k-ring patch, then smooth all candidates.

Strategy
--------
1. Candidate detection   — local z-score (no global VTK call; pure numpy).
2. Per-candidate patch   — k-ring BFS using cached CSR (microseconds each).
3. Classification        — boundary check via cached edge_pt_ids_arr (fast);
                           feature-edge check via vtkFeatureEdges on tiny patch.
4. Edge dilation         — BFS expand edge-classified candidates (same vectorised
                           loop as precompute_smooth_geometry Step 5).
5. Vectorised smoothing  — CSR neighbour-mean, identical to apply_edge_smooth.

This replaces the three separate Paraview macros
(SmartSmoothEdge / SmartSmoothSpike / Smart_Smooth_AUTO) with a single
pipeline-integrated function that scales to 26 M triangles.
"""

from __future__ import annotations

import vtk
import numpy as np
from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk

from modules.core.settings import (
    ARRAY_NAME, FEATURE_ANGLE, SPIKE_SIGMA, MIN_NEIGHBORS, SMOOTH_K_RING,
    SPIKE_RATIO, EDGE_TOP_PERCENTILE,
)
from modules.vtk.smoothing import _build_active_csr


# ── k-ring BFS (topology, uses cached CSR) ────────────────────────────────────

def _k_ring_cells(
        seed_id: int,
        k: int,
        conn: np.ndarray,
        offs: np.ndarray,
        pt_cell_offsets: np.ndarray,
        sorted_cell_ids: np.ndarray,
) -> np.ndarray:
    """Return sorted unique cell IDs within k topological hops of seed_id.

    Uses the same vectorised BFS as precompute_smooth_geometry Step 5
    but operates on a single seed rather than an initial frontier set.
    Returns an array that always includes seed_id itself.
    """
    visited = np.array([seed_id], dtype=np.int64)
    ring    = visited

    for _ in range(k):
        # Points of all cells in the current frontier ring
        r_starts  = offs[ring]
        r_ends    = offs[ring + 1]
        r_sizes   = (r_ends - r_starts).astype(np.int64)
        total_r   = int(r_sizes.sum())
        if total_r == 0:
            break
        cum_r     = np.empty(len(ring) + 1, dtype=np.int64)
        cum_r[0]  = 0
        np.cumsum(r_sizes, out=cum_r[1:])
        local_r   = (np.arange(total_r, dtype=np.int64)
                     - np.repeat(cum_r[:-1], r_sizes))
        pts_flat  = np.unique(conn[np.repeat(r_starts, r_sizes) + local_r])

        # Cells sharing those points
        p_starts  = pt_cell_offsets[pts_flat]
        p_ends    = pt_cell_offsets[pts_flat + 1]
        p_sizes   = (p_ends - p_starts).astype(np.int64)
        total_p   = int(p_sizes.sum())
        if total_p == 0:
            break
        cum_p     = np.empty(len(pts_flat) + 1, dtype=np.int64)
        cum_p[0]  = 0
        np.cumsum(p_sizes, out=cum_p[1:])
        local_p   = (np.arange(total_p, dtype=np.int64)
                     - np.repeat(cum_p[:-1], p_sizes))
        all_nbrs  = sorted_cell_ids[
            np.repeat(p_starts, p_sizes) + local_p
        ].astype(np.int64)

        new_ids = np.setdiff1d(np.unique(all_nbrs), visited, assume_unique=False)
        if len(new_ids) == 0:
            break
        visited = np.union1d(visited, new_ids)
        ring    = new_ids

    return visited


# ── Main smart-smooth entry point ─────────────────────────────────────────────

def smart_smooth_auto(
        src: vtk.vtkPolyData,
        n_iter: int = 1,
        stop_event=None,
        geo_cache: dict | None = None,
        spike_sigma: float = SPIKE_SIGMA,
        spike_ratio: float = SPIKE_RATIO,
        min_neighbors: int = MIN_NEIGHBORS,
        k_ring: int = SMOOTH_K_RING,
        feature_angle: float = FEATURE_ANGLE,
        dilation_rings: int = 1,
        proximity_radius: float = 0.0,
        smooth_spikes: bool = False,
) -> vtk.vtkPolyData | None:
    """Smart per-candidate smoothing: local z-score + local edge classification.

    Returns a new vtkPolyData (src is never modified), or None if stop_event
    fires mid-computation.

    Parameters
    ----------
    src            : input mesh
    n_iter         : smoothing passes applied to all active cells
    stop_event     : threading.Event; checked between phases
    geo_cache      : dict from precompute_smooth_geometry() — must contain
                     conn, offs, pt_cell_offsets, sorted_cell_ids, edge_pt_ids_arr
    spike_sigma    : local z-score threshold (cell must exceed
                     local_mean + spike_sigma * local_std).  Acts as a cheap
                     first-pass pre-filter before the ratio check.
    spike_ratio    : ratio filter threshold.  When > 1.0, a candidate must also
                     satisfy val > max(nbr_vals) * spike_ratio.  Gradient cells
                     are only slightly above their peak neighbour (ratio ≈ 1) so
                     they are eliminated; true isolated needles have ratio >> 1.
                     Set to 0.0 to disable (sigma-only, legacy behaviour).
                     Typical values: 1.5 – 3.0.
    min_neighbors  : minimum neighbors for local stats to be meaningful
    k_ring         : topological radius for the k-ring BFS patch (used for
                     edge-point lookup, not vtkFeatureEdges)
    feature_angle  : reserved; no longer used since Level-2 vtkFeatureEdges
                     was removed (precompute already covers all feature edges)
    dilation_rings : BFS rings by which edge-classified candidates are expanded
    proximity_radius: unused in AUTO mode (k-ring replaces spatial proximity)
    smooth_spikes  : if True, spike-classified candidates (not near any edge
                     point) are also smoothed.  If False (default), only
                     edge-classified candidates (+ dilation) are smoothed.
    """
    def _cancelled() -> bool:
        return stop_event is not None and stop_event.is_set()

    out = vtk.vtkPolyData()
    out.DeepCopy(src)

    in_arr  = src.GetCellData().GetArray(ARRAY_NAME)
    out_arr = out.GetCellData().GetArray(ARRAY_NAME)
    if in_arr is None or out_arr is None:
        print(f"  [SKIP] Array '{ARRAY_NAME}' not found in cell data.")
        return out

    raw_vals = vtk_to_numpy(in_arr).astype(float, copy=True)
    num_cells = len(raw_vals)

    # ── Unpack connectivity from cache ────────────────────────────────────────
    if (geo_cache is None
            or geo_cache.get("conn") is None
            or geo_cache.get("pt_cell_offsets") is None):
        print("  [WARN] smart_smooth_auto requires a full geo_cache. "
              "No cache provided or cache incomplete — skipping.")
        return out

    conn            = geo_cache["conn"]
    offs            = geo_cache["offs"]
    pt_cell_offsets = geo_cache["pt_cell_offsets"]
    sorted_cell_ids = geo_cache["sorted_cell_ids"].astype(np.int64)
    edge_pt_ids_arr = geo_cache.get("edge_pt_ids_arr",
                                    np.array([], dtype=np.int64))
    n_pts_cache     = len(pt_cell_offsets) - 1

    # ── Phase A: candidate detection via local z-score (fully vectorised) ───────
    # All neighbor lookups use the cached CSR topology arrays (conn, offs,
    # pt_cell_offsets, sorted_cell_ids) — zero VTK GetCellPoints/GetPointCells
    # calls on src.  For a 26 M-cell mesh with 288 K nonzero cells this reduces
    # Phase A from ~80 s (Python VTK loop) to <1 s (numpy bincount).
    nonzero_ids = np.nonzero(raw_vals)[0].astype(np.int64)
    print(f"  [AUTO] Checking {len(nonzero_ids):,} non-zero cells "
          f"out of {num_cells:,} total.")

    candidates: list[int] = []

    if len(nonzero_ids) > 0 and len(offs) >= num_cells + 1:
        # 1. Flat array of point IDs for all nonzero cells
        nz_s      = offs[nonzero_ids]
        nz_e      = offs[nonzero_ids + 1]
        nz_sz     = (nz_e - nz_s).astype(np.int64)
        tot_p     = int(nz_sz.sum())
        cum_nz    = np.empty(len(nonzero_ids) + 1, dtype=np.int64)
        cum_nz[0] = 0
        np.cumsum(nz_sz, out=cum_nz[1:])
        loc_p     = (np.arange(tot_p, dtype=np.int64)
                     - np.repeat(cum_nz[:-1], nz_sz))
        pts_flat  = conn[np.repeat(nz_s, nz_sz) + loc_p]
        nz_of_p   = np.repeat(np.arange(len(nonzero_ids), dtype=np.int64), nz_sz)

        # 2. Keep only points within the cached mesh range
        ok       = pts_flat < n_pts_cache
        pts_ok   = pts_flat[ok]
        nz_ok    = nz_of_p[ok]

        # 3. Neighbor cell IDs for each valid point
        p_s      = pt_cell_offsets[pts_ok]
        p_e      = pt_cell_offsets[pts_ok + 1]
        p_sz     = (p_e - p_s).astype(np.int64)
        tot_n    = int(p_sz.sum())
        cum_p    = np.empty(len(pts_ok) + 1, dtype=np.int64)
        cum_p[0] = 0
        np.cumsum(p_sz, out=cum_p[1:])
        loc_n    = (np.arange(tot_n, dtype=np.int64)
                    - np.repeat(cum_p[:-1], p_sz))
        nbr_c    = sorted_cell_ids[np.repeat(p_s, p_sz) + loc_n].astype(np.int64)
        nz_of_n  = np.repeat(nz_ok, p_sz)
        self_c   = nonzero_ids[nz_of_n]

        # 4. Remove self-connections
        keep     = nbr_c != self_c
        nbr_c    = nbr_c[keep]
        nz_of_n  = nz_of_n[keep]

        # 5. Per-nonzero-cell neighbor statistics via bincount (O(N))
        N        = len(nonzero_ids)
        nbr_v    = raw_vals[nbr_c]
        cnt      = np.bincount(nz_of_n, minlength=N).astype(np.float64)
        sm1      = np.bincount(nz_of_n, weights=nbr_v,    minlength=N)
        sm2      = np.bincount(nz_of_n, weights=nbr_v**2, minlength=N)
        del nbr_c, nz_of_n, self_c, keep, nbr_v, pts_flat, nz_of_p, pts_ok, nz_ok

        # 6. Flag candidates where val > local_mean + sigma * local_std
        enough   = cnt >= min_neighbors
        safe_c   = np.where(cnt > 0, cnt, 1.0)
        mu       = np.where(enough, sm1 / safe_c, 0.0)
        var      = np.maximum(np.where(enough, sm2 / safe_c - mu**2, 0.0), 0.0)
        sig_a    = np.sqrt(var)
        cv       = raw_vals[nonzero_ids]
        flagged  = enough & (sig_a >= 1e-12) & (cv > mu + spike_sigma * sig_a)
        candidates = nonzero_ids[flagged].tolist()
        del cnt, sm1, sm2, enough, mu, var, sig_a, cv

    elif len(nonzero_ids) > 0:
        # Fallback: sequential VTK path (mesh size mismatch — should not occur)
        _pt  = vtk.vtkIdList()
        _nb  = vtk.vtkIdList()
        for cell_id in nonzero_ids.tolist():
            if _cancelled():
                return None
            src.GetCellPoints(cell_id, _pt)
            nbr_v_fb: list[float] = []
            for p_idx in range(_pt.GetNumberOfIds()):
                p_id = _pt.GetId(p_idx)
                if p_id >= n_pts_cache:
                    continue
                src.GetPointCells(p_id, _nb)
                for c_idx in range(_nb.GetNumberOfIds()):
                    ncid = _nb.GetId(c_idx)
                    if ncid != cell_id:
                        nbr_v_fb.append(raw_vals[ncid])
            if len(nbr_v_fb) < min_neighbors:
                continue
            lm = float(np.mean(nbr_v_fb))
            ls = float(np.std(nbr_v_fb))
            if ls < 1e-12 or raw_vals[cell_id] <= lm + spike_sigma * ls:
                continue
            candidates.append(cell_id)

    print(f"  [AUTO] {len(candidates):,} outlier candidate(s) "
          f"at sigma={spike_sigma}.")

    # ── Phase A-bis: edge-direct pass (secondary, vectorised) ─────────────────
    # Cells touching a known edge point that sit in the global top
    # (100 - EDGE_TOP_PERCENTILE) % of non-zero values are added as candidates
    # regardless of local z-score.  This robustly catches tight clusters of
    # 2-3 adjacent hot cells at an edge where the elevated neighbours inflate
    # each other's local mean/std, defeating the sigma threshold alone.
    # True spikes near low-valued cells are already caught by Phase A (sigma).
    edge_direct_set: set[int] = set()   # tracked separately to skip Phase B
    if len(edge_pt_ids_arr) > 0 and len(nonzero_ids) > 0:
        ep = edge_pt_ids_arr[edge_pt_ids_arr < n_pts_cache]
        if len(ep) > 0:
            p_starts = pt_cell_offsets[ep]
            p_ends   = pt_cell_offsets[ep + 1]
            p_sizes  = (p_ends - p_starts).astype(np.int64)
            total_p  = int(p_sizes.sum())
            if total_p > 0:
                cum_p    = np.empty(len(ep) + 1, dtype=np.int64)
                cum_p[0] = 0
                np.cumsum(p_sizes, out=cum_p[1:])
                local_p  = (np.arange(total_p, dtype=np.int64)
                            - np.repeat(cum_p[:-1], p_sizes))
                edge_adj = np.unique(
                    sorted_cell_ids[
                        np.repeat(p_starts, p_sizes) + local_p
                    ].astype(np.int64)
                )
                # Global percentile threshold over all non-zero cells
                global_thresh = float(np.percentile(raw_vals[nonzero_ids],
                                                     EDGE_TOP_PERCENTILE))
                # Edge-adjacent, non-zero, above global threshold
                edge_adj_nz   = edge_adj[raw_vals[edge_adj] > 0.0]
                edge_direct   = edge_adj_nz[raw_vals[edge_adj_nz] >= global_thresh]
                if len(edge_direct) > 0:
                    cand_set = set(candidates)
                    n_before = len(candidates)
                    for cid in edge_direct.tolist():
                        if cid not in cand_set:
                            candidates.append(cid)
                            cand_set.add(cid)
                        edge_direct_set.add(cid)
                    n_added = len(candidates) - n_before
                    if n_added > 0:
                        print(f"  [AUTO] Edge-direct: +{n_added:,} candidate(s) "
                              f"above {EDGE_TOP_PERCENTILE:.4g}th percentile.")

    if not candidates:
        print("  [AUTO] No candidates found — output unchanged.")
        return out

    if _cancelled():
        return None

    # ── Phase B: classify each candidate — edge or spike ─────────────────────
    # pt_ids_tmp / neighbor_ids only used if spike_ratio > 1.0 (ratio filter)
    pt_ids_tmp   = vtk.vtkIdList()
    neighbor_ids = vtk.vtkIdList()
    edge_candidates: list[int] = []
    spike_candidates: list[int] = []

    # Boolean mask for O(1) per-point edge lookup: avoids sorting 2.2 M-element
    # edge_pt_ids_arr on every candidate.  Falls back to intersect1d if not cached.
    edge_pt_mask: np.ndarray | None = geo_cache.get("edge_pt_mask")

    for cell_id in candidates:
        # Edge-direct: adjacent to edge point by construction → "edge" instantly
        if cell_id in edge_direct_set:
            edge_candidates.append(cell_id)
            continue

        # Level-1: k-ring point check against edge_pt_mask / edge_pt_ids_arr
        patch_cells = _k_ring_cells(
            cell_id, k_ring, conn, offs, pt_cell_offsets, sorted_cell_ids
        )
        pt_starts  = offs[patch_cells]
        pt_ends    = offs[patch_cells + 1]
        pt_sizes   = (pt_ends - pt_starts).astype(np.int64)
        total_pts  = int(pt_sizes.sum())
        if total_pts == 0:
            spike_candidates.append(cell_id)
            continue
        cum_pts    = np.empty(len(patch_cells) + 1, dtype=np.int64)
        cum_pts[0] = 0
        np.cumsum(pt_sizes, out=cum_pts[1:])
        local_pts  = (np.arange(total_pts, dtype=np.int64)
                      - np.repeat(cum_pts[:-1], pt_sizes))
        patch_pts  = np.unique(conn[np.repeat(pt_starts, pt_sizes) + local_pts])

        # Boolean mask lookup: O(n_patch) vectorized gather vs O(n_edge × log n_edge)
        # intersect1d sort+merge.  For 2.2 M edge points and 15 K candidates this
        # saves ~90 % of the per-candidate classification time.
        if edge_pt_mask is not None:
            valid_pp = patch_pts[patch_pts < len(edge_pt_mask)]
            is_near_edge = bool(edge_pt_mask[valid_pp].any()) if len(valid_pp) else False
        else:
            is_near_edge = (len(edge_pt_ids_arr) > 0
                            and np.intersect1d(patch_pts, edge_pt_ids_arr,
                                               assume_unique=True).size > 0)

        if is_near_edge:
            edge_candidates.append(cell_id)
            continue

        # Level-1 failed — candidate is not near any edge point → spike.
        if spike_ratio > 1.0:
            cell_val = raw_vals[cell_id]
            src.GetCellPoints(cell_id, pt_ids_tmp)
            nbr_max_vals: list[float] = []
            for p_idx in range(pt_ids_tmp.GetNumberOfIds()):
                p_id = pt_ids_tmp.GetId(p_idx)
                if p_id < n_pts_cache:
                    src.GetPointCells(p_id, neighbor_ids)
                    for c_idx in range(neighbor_ids.GetNumberOfIds()):
                        ncid = neighbor_ids.GetId(c_idx)
                        if ncid != cell_id:
                            nbr_max_vals.append(raw_vals[ncid])
            if nbr_max_vals:
                max_nbr = float(max(nbr_max_vals))
                if max_nbr > 0.0 and cell_val <= max_nbr * spike_ratio:
                    continue   # gradient cell — skip
        spike_candidates.append(cell_id)

    ratio_tag = f", ratio>{spike_ratio}" if spike_ratio > 1.0 else ""
    print(f"  [AUTO] Classification: {len(edge_candidates):,} edge, "
          f"{len(spike_candidates):,} spike{ratio_tag}.")

    if _cancelled():
        return None

    # ── Phase C: BFS-dilate edge candidates ──────────────────────────────────
    if dilation_rings > 0 and edge_candidates:
        edge_set     = np.array(sorted(set(edge_candidates)), dtype=np.int64)
        n_pre_dil    = len(edge_set)
        visited_arr  = edge_set
        ring         = edge_set

        for _layer in range(dilation_rings):
            r_starts  = offs[ring]
            r_ends    = offs[ring + 1]
            r_sizes   = (r_ends - r_starts).astype(np.int64)
            total_r   = int(r_sizes.sum())
            if total_r == 0:
                break
            cum_r     = np.empty(len(ring) + 1, dtype=np.int64)
            cum_r[0]  = 0
            np.cumsum(r_sizes, out=cum_r[1:])
            local_r   = (np.arange(total_r, dtype=np.int64)
                         - np.repeat(cum_r[:-1], r_sizes))
            pts_flat  = np.unique(conn[np.repeat(r_starts, r_sizes) + local_r])

            p_starts  = pt_cell_offsets[pts_flat]
            p_ends    = pt_cell_offsets[pts_flat + 1]
            p_sizes   = (p_ends - p_starts).astype(np.int64)
            total_p   = int(p_sizes.sum())
            if total_p == 0:
                break
            cum_p     = np.empty(len(pts_flat) + 1, dtype=np.int64)
            cum_p[0]  = 0
            np.cumsum(p_sizes, out=cum_p[1:])
            local_p   = (np.arange(total_p, dtype=np.int64)
                         - np.repeat(cum_p[:-1], p_sizes))
            all_nbrs  = sorted_cell_ids[
                np.repeat(p_starts, p_sizes) + local_p
            ].astype(np.int64)

            new_ids = np.setdiff1d(np.unique(all_nbrs), visited_arr,
                                   assume_unique=False)
            if len(new_ids) == 0:
                break
            visited_arr = np.union1d(visited_arr, new_ids)
            ring        = new_ids

        n_dilated = len(visited_arr) - n_pre_dil
        if n_dilated > 0:
            print(f"  [AUTO] Edge dilation: +{n_dilated:,} cells "
                  f"({dilation_rings} ring(s)).")
        edge_candidates_final = visited_arr
    else:
        edge_candidates_final = (
            np.array(sorted(set(edge_candidates)), dtype=np.int64)
            if edge_candidates else np.array([], dtype=np.int64)
        )

    if _cancelled():
        return None

    # ── Phase D: build active set and smooth ──────────────────────────────────
    spike_arr  = np.array(sorted(set(spike_candidates)), dtype=np.int64)
    if smooth_spikes:
        active_cells = np.union1d(edge_candidates_final, spike_arr)
    else:
        active_cells = edge_candidates_final
        if len(spike_arr) > 0:
            print(f"  [AUTO] {len(spike_arr):,} spike candidate(s) detected but "
                  f"not smoothed (smooth_spikes=off).")

    # Zero-filter: skip cells permanently zero in this file
    active_cells = active_cells[raw_vals[active_cells] != 0.0]

    if len(active_cells) == 0:
        print("  [AUTO] All candidates are zero — nothing to smooth.")
        return out

    print(f"  [AUTO] Smoothing {len(active_cells):,} active cells "
          f"({n_iter} iteration(s)).")

    csr_offsets, csr_nbr_ids = _build_active_csr(
        active_cells, conn, offs, pt_cell_offsets, sorted_cell_ids,
    )

    csr_counts   = np.diff(csr_offsets).astype(np.float64)
    has_nbrs     = csr_counts > 0
    nonzero_flag = raw_vals[active_cells] != 0.0
    active_mask  = has_nbrs & nonzero_flag

    current_vals = np.copy(raw_vals)
    for iteration in range(max(1, int(n_iter))):
        if _cancelled():
            return None
        nbr_vals   = current_vals[csr_nbr_ids]
        sums       = np.add.reduceat(nbr_vals, csr_offsets[:-1].astype(np.intp))
        next_vals  = np.copy(current_vals)
        next_vals[active_cells[active_mask]] = (
            sums[active_mask] / csr_counts[active_mask]
        )
        current_vals = next_vals
        if n_iter > 1:
            print(f"  [AUTO] Smooth pass {iteration + 1}/{n_iter} done.")

    new_arr = numpy_to_vtk(current_vals, deep=True,
                           array_type=out_arr.GetDataType())
    new_arr.SetName(ARRAY_NAME)
    out.GetCellData().RemoveArray(ARRAY_NAME)
    out.GetCellData().AddArray(new_arr)
    out.GetCellData().SetActiveScalars(ARRAY_NAME)

    n_edge_f  = int(np.sum(np.isin(active_cells, edge_candidates_final)))
    n_spike_f = int(np.sum(np.isin(active_cells, spike_arr))) if smooth_spikes else 0
    spike_tag = f" + {n_spike_f:,} spike" if smooth_spikes else ""
    print(f"  [AUTO] Done: {n_edge_f:,} edge{spike_tag} = "
          f"{len(active_cells):,} cells smoothed "
          f"({n_iter} pass(es)).")
    return out
