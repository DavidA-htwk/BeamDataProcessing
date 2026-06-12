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


# ── Per-candidate edge / spike classification ─────────────────────────────────

def _classify_candidate(
        src: vtk.vtkPolyData,
        k_ring_cell_ids: np.ndarray,
        k_ring_pt_ids: np.ndarray,
        edge_pt_ids_arr: np.ndarray,
        feature_angle: float,
) -> str:
    """Return 'edge' or 'spike' for a single candidate cell.

    Two-level check:
    1. Boundary check (no VTK, pure numpy):
       if any point in the k-ring matches a known mesh boundary/feature-edge
       point (from the precomputed cache), classify as 'edge'.
    2. Local feature-edge check (vtkExtractCells + vtkFeatureEdges on tiny patch):
       if the small patch contains an internal feature edge at `feature_angle`,
       classify as 'edge'.
    Otherwise 'spike'.
    """
    # Level 1 — fast boundary check via cached edge point IDs
    if (len(edge_pt_ids_arr) > 0
            and np.intersect1d(k_ring_pt_ids, edge_pt_ids_arr,
                               assume_unique=True).size > 0):
        return "edge"

    # Level 2 — local feature-edge check on extracted patch
    id_list = vtk.vtkIdList()
    for cid in k_ring_cell_ids.tolist():
        id_list.InsertNextId(int(cid))

    extractor = vtk.vtkExtractCells()
    extractor.SetInputData(src)
    extractor.SetCellList(id_list)
    extractor.Update()
    patch = extractor.GetOutput()

    if patch.GetNumberOfPoints() == 0:
        return "spike"

    # vtkExtractCells outputs vtkUnstructuredGrid; vtkFeatureEdges needs vtkPolyData.
    geom_filter = vtk.vtkGeometryFilter()
    geom_filter.SetInputData(patch)
    geom_filter.Update()
    patch_poly = geom_filter.GetOutput()

    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(patch_poly)
    fe.BoundaryEdgesOff()      # boundary of the global mesh already handled above
    fe.FeatureEdgesOn()
    fe.SetFeatureAngle(feature_angle)
    fe.NonManifoldEdgesOff()
    fe.ManifoldEdgesOff()
    fe.ColoringOff()
    fe.Update()

    if fe.GetOutput().GetNumberOfPoints() > 0:
        return "edge"

    return "spike"


# ── Main smart-smooth entry point ─────────────────────────────────────────────

def smart_smooth_auto(
        src: vtk.vtkPolyData,
        n_iter: int = 1,
        stop_event=None,
        geo_cache: dict | None = None,
        spike_sigma: float = SPIKE_SIGMA,
        min_neighbors: int = MIN_NEIGHBORS,
        k_ring: int = SMOOTH_K_RING,
        feature_angle: float = FEATURE_ANGLE,
        dilation_rings: int = 1,
        proximity_radius: float = 0.0,
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
    spike_sigma    : local z-score threshold for candidate detection
    min_neighbors  : minimum neighbors for local stats to be meaningful
    k_ring         : topological radius for classification patch
    feature_angle  : angle (degrees) for vtkFeatureEdges feature detection
    dilation_rings : BFS rings by which edge-classified candidates are expanded
    proximity_radius: unused in AUTO mode (k-ring replaces spatial proximity)
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

    # ── Phase A: candidate detection via local z-score ────────────────────────
    nonzero_ids = np.nonzero(raw_vals)[0].astype(np.int64)
    print(f"  [AUTO] Checking {len(nonzero_ids):,} non-zero cells "
          f"out of {num_cells:,} total.")

    pt_ids_tmp   = vtk.vtkIdList()
    neighbor_ids = vtk.vtkIdList()
    candidates: list[int] = []

    for cell_id in nonzero_ids.tolist():
        if _cancelled():
            return None
        # Collect point-connected neighbor values using VTK API
        # (cheaper than CSR slice for the small set of nonzero cells)
        src.GetCellPoints(cell_id, pt_ids_tmp)
        nbr_vals: list[float] = []
        for p_idx in range(pt_ids_tmp.GetNumberOfIds()):
            p_id = pt_ids_tmp.GetId(p_idx)
            if p_id >= n_pts_cache:
                continue
            src.GetPointCells(p_id, neighbor_ids)
            for c_idx in range(neighbor_ids.GetNumberOfIds()):
                ncid = neighbor_ids.GetId(c_idx)
                if ncid != cell_id:
                    nbr_vals.append(raw_vals[ncid])

        if len(nbr_vals) < min_neighbors:
            continue

        loc_mean = float(np.mean(nbr_vals))
        loc_std  = float(np.std(nbr_vals))
        if loc_std < 1e-12:
            continue

        if raw_vals[cell_id] > loc_mean + spike_sigma * loc_std:
            candidates.append(cell_id)

    print(f"  [AUTO] {len(candidates):,} outlier candidate(s) "
          f"at sigma={spike_sigma}.")

    if not candidates:
        print("  [AUTO] No candidates found — output unchanged.")
        return out

    if _cancelled():
        return None

    # ── Phase B: classify each candidate — edge or spike ─────────────────────
    edge_candidates: list[int] = []
    spike_candidates: list[int] = []

    for cell_id in candidates:
        patch_cells = _k_ring_cells(
            cell_id, k_ring, conn, offs, pt_cell_offsets, sorted_cell_ids
        )
        # Collect unique point IDs for all patch cells
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

        label = _classify_candidate(
            src, patch_cells, patch_pts, edge_pt_ids_arr, feature_angle
        )
        if label == "edge":
            edge_candidates.append(cell_id)
        else:
            spike_candidates.append(cell_id)

    print(f"  [AUTO] Classification: {len(edge_candidates):,} edge, "
          f"{len(spike_candidates):,} spike.")

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
    active_cells = np.union1d(edge_candidates_final, spike_arr)

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

    n_edge_f = int(len(edge_candidates_final))
    n_spike  = int(len(spike_arr))
    print(f"  [AUTO] Done: {n_edge_f:,} edge cell(s), {n_spike:,} spike cell(s) smoothed.")
    return out
