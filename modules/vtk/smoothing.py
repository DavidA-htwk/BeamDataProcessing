"""
modules/smoothing.py
--------------------
Edge-based iterative neighbour-mean smoothing for vtkPolyData.

Two public functions:
  precompute_smooth_geometry  — topology-only pre-computation (once per component)
  apply_edge_smooth           — scalar-value smoothing using cached or fresh topology
"""

from __future__ import annotations

import vtk
import numpy as np
from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk

from modules.core.settings import (
    ARRAY_NAME, FEATURE_ANGLE, SMOOTH_PROXIMITY_RADIUS,
    TRUE_MAX_GUARD_K_RING, TRUE_MAX_GUARD_RATIO, TRUE_MAX_GUARD_MIN_SUPPORT,
    MAIN_AREA_SUPPORT_K_RING, MAIN_AREA_SUPPORT_RATIO,
    MAIN_AREA_SUPPORT_MIN_COUNT, MAIN_AREA_MAX_SCAN_CANDIDATES,
)


# ── Geometry pre-computation ──────────────────────────────────────────────────

def precompute_smooth_geometry(
        src: vtk.vtkPolyData,
        proximity_radius: float | None = None,
        log_fn=None,
        skip_edge_expansion: bool = False,
) -> dict | None:
    """Compute and return the geometry-only data needed by apply_edge_smooth.

    This is ONLY a function of mesh topology + vertex positions — identical for
    every VTP file of the same component (same underlying mesh).  Call once per
    component, then pass the result via geo_cache= to skip Steps 1-4 for all
    subsequent files.

    skip_edge_expansion: when True (AUTO mode), Step 3 is skipped.
        Step 3 (flag cells touching a feature/boundary edge point) is only
        needed by "edge" mode.  AUTO mode finds its own candidates via local
        z-score and only needs the CSR connectivity + edge_pt_ids_arr.
        NOTE: "edge" mode no longer grows outward from those direct edge
        cells (proximity-based topological expansion was reverted — it was
        over-smoothing good/undamaged cells).

    Returns a dict with keys:
        edge_cell_list, cell_neighbours, n_cells, n_direct,
        edge_cell_arr, csr_offsets, csr_nbr_ids
    Returns an empty-list dict if no edge cells are found.
    """
    def _log(msg: str) -> None:
        print(msg)
        if log_fn:
            log_fn(msg)

    if proximity_radius is None:
        proximity_radius = SMOOTH_PROXIMITY_RADIUS

    n_cells = src.GetCellData().GetNumberOfTuples() if src.GetCellData() else 0
    n_pts   = src.GetNumberOfPoints()
    _log(f"    Mesh: {n_cells:,} cells, {n_pts:,} points")

    # Step 1 — extract boundary + feature edges
    _log("    Step 1/5: extracting feature/boundary edges...")
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(src)
    fe.BoundaryEdgesOn()
    fe.FeatureEdgesOn()
    fe.SetFeatureAngle(FEATURE_ANGLE)
    fe.NonManifoldEdgesOff()
    fe.ManifoldEdgesOff()
    fe.ColoringOff()
    fe.Update()
    edge_pts    = fe.GetOutput().GetPoints()
    n_edge_pts  = edge_pts.GetNumberOfPoints() if edge_pts else 0
    _log(f"    Step 1/5: done — {n_edge_pts:,} edge points")

    # Step 2 — match each edge point to its closest source mesh point.
    # FindClosestPoint is fast (no Python radius loop).
    # Proximity is handled later via topological ring expansion (Step 5).
    edge_pt_ids: set = set()
    if edge_pts and n_edge_pts > 0:
        _log(f"    Step 2/4: matching {n_edge_pts:,} edge points to source mesh...")
        loc_pts = vtk.vtkStaticPointLocator()
        loc_pts.SetDataSet(src)
        loc_pts.BuildLocator()
        for i in range(n_edge_pts):
            edge_pt_ids.add(loc_pts.FindClosestPoint(edge_pts.GetPoint(i)))
        _log(f"    Step 2/4: done — {len(edge_pt_ids):,} unique source points on edges")

    # Convert edge point IDs to array once — reused by smart_smooth_auto for
    # fast boundary-edge classification without re-running global vtkFeatureEdges.
    edge_pt_ids_arr = np.fromiter(edge_pt_ids, dtype=np.int64)

    # Boolean mask equivalent of edge_pt_ids_arr: O(1) per-point lookup vs O(log N)
    # intersect1d sort+merge.  Stored in cache so Phase B never re-sorts the 2.2 M
    # entry array per candidate.
    edge_pt_mask = np.zeros(n_pts, dtype=bool)
    if len(edge_pt_ids_arr) > 0:
        valid_ep = edge_pt_ids_arr[edge_pt_ids_arr < n_pts]
        edge_pt_mask[valid_ep] = True

    # Step 3 — flag ALL cells owning an edge point. Pure topology, no scalars.
    # Skipped in AUTO mode: smart_smooth_auto finds its own candidates via
    # local z-score and never uses edge_cells_arr.
    edge_cells_arr   = np.array([], dtype=np.int64)
    n_direct         = 0
    conn             = None
    offs             = None
    cell_areas       = np.zeros(n_cells, dtype=np.float32)
    sliver_threshold = 0.0
    if edge_pt_ids:
        # Always read conn/offs — Step 4 (CSR build) needs them regardless of mode.
        polys = src.GetPolys()
        if polys is not None and polys.GetNumberOfCells() > 0:
            conn = vtk_to_numpy(polys.GetConnectivityArray())
            offs = vtk_to_numpy(polys.GetOffsetsArray())
            # Per-cell triangle area — vectorised, once per component.
            # Used downstream to detect and correct degenerate sliver cells
            # before any scalar processing (area < 1e-4 × median_area).
            _pts_vtk = src.GetPoints()
            if _pts_vtk is not None:
                _pts_np = vtk_to_numpy(_pts_vtk.GetData()).reshape(-1, 3).astype(np.float64)
                _csizes = np.diff(offs)
                _tri    = np.where(_csizes == 3)[0]
                if len(_tri) > 0:
                    _ts    = offs[_tri]
                    _v0    = _pts_np[conn[_ts]]
                    _v1    = _pts_np[conn[_ts + 1]]
                    _v2    = _pts_np[conn[_ts + 2]]
                    _cr    = np.cross(_v1 - _v0, _v2 - _v0)
                    _atri  = 0.5 * np.linalg.norm(_cr, axis=1)
                    cell_areas[_tri] = _atri.astype(np.float32)
                    _pos   = _atri[_atri > 0.0]
                    if len(_pos) > 0:
                        _med             = float(np.median(_pos))
                        sliver_threshold = _med * 1e-4
                        _log(f"    Cell areas: median={_med:.3e}, "
                             f"sliver threshold={sliver_threshold:.3e} "
                             f"({len(_tri):,} triangles)")
                    del _pts_np, _csizes, _tri, _ts, _v0, _v1, _v2, _cr, _atri, _pos
                del _pts_vtk

        if not skip_edge_expansion:
            _log(f"    Step 3/4: flagging edge cells (vectorized, {n_cells:,} cells)...")
            if conn is not None and offs is not None:
                pt_mask = np.zeros(n_pts, dtype=np.uint8)
                pt_mask[np.fromiter(edge_pt_ids, dtype=np.int64)] = 1
                cell_hits = np.add.reduceat(pt_mask[conn], offs[:-1].astype(np.intp))
                edge_cells_arr = np.where(cell_hits > 0)[0].astype(np.int64)
                n_direct = len(edge_cells_arr)
            else:
                cell_pts_tmp = vtk.vtkIdList()
                ec: list[int] = []
                for cid in range(n_cells):
                    src.GetCellPoints(cid, cell_pts_tmp)
                    for k in range(cell_pts_tmp.GetNumberOfIds()):
                        if cell_pts_tmp.GetId(k) in edge_pt_ids:
                            ec.append(cid); break
                edge_cells_arr = np.array(ec, dtype=np.int64)
                n_direct = len(edge_cells_arr)
            _log(f"    Step 3/4: done - {n_direct:,} direct edge cells (no scalar filter)")
        else:
            _log("    Step 3/4: skipped (AUTO mode — candidates found via z-score)")
    else:
        _log("    Step 3/4: skipped (no edge points found)")

    _empty = {"edge_cells_arr":   np.array([], dtype=np.int64), "edge_cell_list": [],
              "cell_neighbours":  {}, "n_cells": n_cells, "n_direct": 0,
              "conn":             None, "offs": None, "pt_cell_offsets": None,
              "sorted_cell_ids":  None,
              "proximity_radius": proximity_radius,
              "edge_pt_ids_arr":  edge_pt_ids_arr,
              "edge_pt_mask":     edge_pt_mask,
              "edge_cell_arr":    np.array([], dtype=np.int64),
              "csr_offsets":      None, "csr_nbr_ids": None,
              "cell_areas":       cell_areas, "sliver_threshold": sliver_threshold}
    if len(edge_cells_arr) == 0 and not skip_edge_expansion:
        return _empty

    # Step 4 \u2014 build point\u2192cell CSR for fast per-file neighbour lookup.
    pt_cell_offsets = None
    sorted_cell_ids = None

    if conn is not None and offs is not None:
        _log(f"    Step 4/5: building point\u2192cell CSR ({n_pts:,} pts)...")
        dtype_cid      = np.int32 if n_cells < 2_000_000_000 else np.int64
        cell_sizes_all = np.diff(offs).astype(np.int64)
        cell_of_conn   = np.repeat(np.arange(n_cells, dtype=dtype_cid), cell_sizes_all)
        order_by_pt    = np.argsort(conn, kind='stable')
        sorted_cell_ids = cell_of_conn[order_by_pt]
        del cell_of_conn
        sorted_pts_tmp  = conn[order_by_pt]
        del order_by_pt
        pt_cell_offsets = np.zeros(n_pts + 1, dtype=np.int64)
        np.add.at(pt_cell_offsets[1:], sorted_pts_tmp, 1)
        np.cumsum(pt_cell_offsets, out=pt_cell_offsets)
        del sorted_pts_tmp
        _log("    Step 4/5: done")

        # Step 5 — REVERTED: topological ring expansion used to grow the
        # direct edge-cell set outward by n_expand layers, but this ended up
        # smoothing good/undamaged cells too aggressively. "edge" mode now
        # only smooths cells that directly touch a feature/boundary edge
        # point (Step 3's edge_cells_arr) — no outward growth.
        if skip_edge_expansion:
            _log("    Step 5/5: skipped (AUTO mode — per-candidate k-ring used instead)")
        else:
            _log(f"    Step 5/5: disabled — smoothing restricted to the "
                 f"{n_direct:,} direct edge cells (no outward proximity growth)")

    return {
        "edge_cells_arr":   edge_cells_arr,
        "n_direct":         n_direct,
        "n_cells":          n_cells,
        "n_pts":            n_pts,
        "conn":             conn,
        "offs":             offs,
        "pt_cell_offsets":  pt_cell_offsets,
        "sorted_cell_ids":  sorted_cell_ids,
        "proximity_radius": proximity_radius,
        "edge_pt_ids_arr":  edge_pt_ids_arr,
        "edge_pt_mask":     edge_pt_mask,
        "cell_areas":       cell_areas,
        "sliver_threshold": sliver_threshold,
        # backward-compat keys
        "edge_cell_list":   edge_cells_arr.tolist(),
        "edge_cell_arr":    edge_cells_arr,
        "cell_neighbours":  {},
        "csr_offsets":      None,
        "csr_nbr_ids":      None,
    }



# ── Per-file CSR builder (uses cached topology, no VTK API calls) ─────────────

def _build_active_csr(
        active_cells: np.ndarray,
        conn: np.ndarray,
        offs: np.ndarray,
        pt_cell_offsets: np.ndarray,
        sorted_cell_ids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Build CSR neighbour arrays for active_cells using cached topology.
    Pure numpy — no VTK GetPointCells / GetCellPoints API calls.
    """
    n_active = len(active_cells)
    if n_active == 0:
        return np.zeros(1, dtype=np.int64), np.array([], dtype=np.int64)

    offsets_out: list = [0]
    nbrs_out:    list = []
    n_pts_cache  = len(pt_cell_offsets) - 1  # guard against cross-file pt count diff
    for cid in active_cells.tolist():
        s, e = int(offs[cid]), int(offs[cid + 1])
        nbr_set: set = set()
        for pid in conn[s:e].tolist():
            if pid >= n_pts_cache:
                continue   # point exists in this file but not cached mesh — skip
            ps, pe = int(pt_cell_offsets[pid]), int(pt_cell_offsets[pid + 1])
            for ncid in sorted_cell_ids[ps:pe].tolist():
                if ncid != cid:
                    nbr_set.add(int(ncid))
        nbrs_out.extend(sorted(nbr_set))
        offsets_out.append(len(nbrs_out))
    return np.array(offsets_out, dtype=np.int64), np.array(nbrs_out, dtype=np.int64)


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

    Vectorised BFS: each hop gathers all points of the current frontier's
    cells, then all cells sharing those points. Shared by smart_smooth_auto
    (per-candidate classification patch) and the "true max area" guard below
    (wider-area support check). Always includes seed_id itself.
    """
    visited = np.array([seed_id], dtype=np.int64)
    ring    = visited

    for _ in range(k):
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

        new_ids = np.setdiff1d(np.unique(all_nbrs), visited, assume_unique=False)
        if len(new_ids) == 0:
            break
        visited = np.union1d(visited, new_ids)
        ring    = new_ids

    return visited


# ── "True max area" guard ──────────────────────────────────────────────────

def true_max_area_guard(
        cell_ids: np.ndarray,
        raw_vals: np.ndarray,
        conn: np.ndarray,
        offs: np.ndarray,
        pt_cell_offsets: np.ndarray,
        sorted_cell_ids: np.ndarray,
        k_ring: int = TRUE_MAX_GUARD_K_RING,
        ratio: float = TRUE_MAX_GUARD_RATIO,
        min_support: int = TRUE_MAX_GUARD_MIN_SUPPORT,
) -> np.ndarray:
    """Identify candidates that sit inside a genuine wide high-value area.

    Both EDGE mode (every boundary/feature-edge cell is otherwise smoothed
    unconditionally) and AUTO mode's edge-direct / global-fallback passes can
    flag a cell for smoothing using only its immediate 1-ring neighbours or a
    bare percentile threshold — neither distinguishes a real, physically
    meaningful hot-spot (surrounded by other comparably high values) from an
    isolated sliver/mesh-artifact spike (surrounded by low/zero values).

    This check looks at a WIDER neighbourhood (``k_ring`` topological hops,
    intentionally bigger than the 1-ring used elsewhere) around each
    candidate. If at least ``min_support`` cells in that bigger area already
    carry a value >= ``ratio`` * the candidate's own value, the candidate is
    considered part of a genuine high-value area and must be protected from
    smoothing.

    Returns a boolean mask (same length/order as cell_ids): True = protect
    (do NOT smooth), False = safe to smooth (isolated outlier).
    """
    n = len(cell_ids)
    protect = np.zeros(n, dtype=bool)
    if n == 0 or conn is None or offs is None or pt_cell_offsets is None or sorted_cell_ids is None:
        return protect

    for i, cid in enumerate(cell_ids.tolist()):
        cval = float(raw_vals[cid])
        if cval <= 0.0:
            continue
        ring = _k_ring_cells(cid, k_ring, conn, offs, pt_cell_offsets, sorted_cell_ids)
        ring = ring[ring != cid]
        if len(ring) == 0:
            continue
        nbr_vals = raw_vals[ring]
        n_support = int(np.count_nonzero(nbr_vals >= cval * ratio))
        if n_support >= min_support:
            protect[i] = True

    return protect


# ── Main high-value area detection ────────────────────────────────────────

def find_main_area_max(
        polydata: vtk.vtkPolyData,
        array_name: str = ARRAY_NAME,
        geo_cache: dict | None = None,
        k_ring: int = MAIN_AREA_SUPPORT_K_RING,
        support_ratio: float = MAIN_AREA_SUPPORT_RATIO,
        min_support: int = MAIN_AREA_SUPPORT_MIN_COUNT,
        max_candidates: int = MAIN_AREA_MAX_SCAN_CANDIDATES,
) -> float | None:
    """Return the peak value that genuinely represents a broad load area.

    The file's plain global max can be a small, spatially tiny stress-
    concentration spike (e.g. at a fillet/hole) that is even a literal
    topological SUBSET of / embedded inside the main load area — touching
    or surrounded by it, not a separate island elsewhere on the mesh. That
    rules out a single global threshold + connected-components approach:
    whatever threshold is inclusive enough to capture the real broad area
    also reconnects it to the embedded spike, merging them into one region
    whose max is trivially the spike's value again.

    Instead, cells are scanned in descending value order. A candidate is
    accepted as the representative peak only if a WIDE topological
    neighbourhood around it (``k_ring`` hops — deliberately much bigger than
    the 1-ring/local checks used elsewhere in this module) contains at
    least ``min_support`` other cells whose own value is also
    >= ``support_ratio`` * the candidate's value. A small embedded spike
    cluster (a handful to a few dozen cells) cannot manufacture that many
    comparably-high neighbours across a wide radius, so it is skipped and
    the next-highest candidate is tried, until one is found that truly has
    broad support — i.e. sits inside a large, contiguous elevated area
    rather than being a narrow peak riding on top of it.

    ``max_candidates`` bounds the scan (the answer is normally found within
    the first few dozen candidates, since spikes are rare/small); if nothing
    qualifies within the budget the plain global max is returned as a safe
    fallback.

    Reuses cached topology (conn/offs/pt_cell_offsets/sorted_cell_ids) from
    ``geo_cache`` when it matches this mesh's cell count; otherwise rebuilds
    the CSR connectivity directly from ``polydata``.

    Returns None if the array is missing, or the plain max if there's no
    usable topology / no non-zero cells.
    """
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return None
    vals = vtk_to_numpy(arr).astype(np.float64)
    n_cells = len(vals)
    if n_cells == 0:
        return None
    global_max = float(vals.max())
    if global_max <= 0.0:
        return global_max

    nonzero_ids = np.nonzero(vals > 0.0)[0]
    if len(nonzero_ids) == 0:
        return global_max

    # ── Connectivity: reuse cache if it matches this mesh, else rebuild ──────
    conn = offs = pt_cell_offsets = sorted_cell_ids = None
    if geo_cache is not None and geo_cache.get("n_cells") == n_cells:
        conn            = geo_cache.get("conn")
        offs            = geo_cache.get("offs")
        pt_cell_offsets = geo_cache.get("pt_cell_offsets")
        sorted_cell_ids = geo_cache.get("sorted_cell_ids")

    if conn is None or offs is None or pt_cell_offsets is None or sorted_cell_ids is None:
        polys = polydata.GetPolys()
        if polys is None or polys.GetNumberOfCells() == 0:
            return global_max
        conn  = vtk_to_numpy(polys.GetConnectivityArray())
        offs  = vtk_to_numpy(polys.GetOffsetsArray())
        n_pts = polydata.GetNumberOfPoints()
        cell_sizes_all  = np.diff(offs).astype(np.int64)
        cell_of_conn    = np.repeat(np.arange(n_cells, dtype=np.int64), cell_sizes_all)
        order_by_pt     = np.argsort(conn, kind='stable')
        sorted_cell_ids = cell_of_conn[order_by_pt]
        sorted_pts_tmp  = conn[order_by_pt]
        pt_cell_offsets = np.zeros(n_pts + 1, dtype=np.int64)
        np.add.at(pt_cell_offsets[1:], sorted_pts_tmp, 1)
        np.cumsum(pt_cell_offsets, out=pt_cell_offsets)

    sorted_cell_ids = sorted_cell_ids.astype(np.int64)

    # ── Descending scan with a wide-neighbourhood support check ─────────────
    order  = nonzero_ids[np.argsort(-vals[nonzero_ids])]
    n_scan = min(len(order), max_candidates)
    for cid in order[:n_scan].tolist():
        cval = float(vals[cid])
        ring = _k_ring_cells(cid, k_ring, conn, offs, pt_cell_offsets, sorted_cell_ids)
        ring = ring[ring != cid]
        if len(ring) == 0:
            continue
        support = int(np.count_nonzero(vals[ring] >= cval * support_ratio))
        if support >= min_support:
            return cval

    # Nothing in the scanned budget had enough wide-area support (unusual) —
    # fall back to the plain global max rather than an unbounded scan.
    return global_max



# ── Smoothing ─────────────────────────────────────────────────────────────────

def apply_edge_smooth(
        src: vtk.vtkPolyData,
        n_iter: int = 1,
        stop_event=None,
        geo_cache: dict | None = None,
        proximity_radius: float | None = None,
) -> vtk.vtkPolyData | None:
    """Iterative neighbour-mean smoothing restricted to cells that touch
    boundary or feature edges (angle >= FEATURE_ANGLE degrees).

    Returns a NEW vtkPolyData (src is never modified), or None if stop_event
    is set mid-computation.

    geo_cache: dict from precompute_smooth_geometry(); when provided, Steps 1-4
    and the neighbour-map build are skipped entirely for this call.
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

    # ── Geometry phase ────────────────────────────────────────────────────────
    cell_neighbours: dict = {}

    if geo_cache is not None and geo_cache.get("pt_cell_offsets") is not None:
        # ── Cached path (proximity already baked in precompute) ──
        # No zero-value filtering: every topology+proximity edge cell is smoothed
        # unconditionally, matching the ParaView macro's behaviour (a flagged cell
        # is smoothed regardless of its current scalar value, including zeros).
        edge_cells_all = geo_cache["edge_cells_arr"]
        active_cells   = edge_cells_all

        if len(active_cells) == 0:
            return out

        # Wide-area guard: a cell that touches a feature/boundary edge would
        # otherwise be smoothed unconditionally, even if it's a genuine,
        # physically meaningful hot-spot sitting in a real high-value area
        # (e.g. a beam peak that happens to fall near the component boundary).
        # Only protect cells that have real support in a WIDER neighbourhood —
        # isolated slivers/artifacts have no such support and are still smoothed.
        conn_g = geo_cache.get("conn")
        if conn_g is not None:
            protect_mask = true_max_area_guard(
                active_cells, raw_vals, conn_g, geo_cache["offs"],
                geo_cache["pt_cell_offsets"],
                geo_cache["sorted_cell_ids"].astype(np.int64),
            )
            n_protected = int(protect_mask.sum())
            if n_protected:
                print(f"  True-max guard: protecting {n_protected:,} cell(s) "
                      f"inside a genuine high-value area from edge smoothing.")
                active_cells = active_cells[~protect_mask]
            if len(active_cells) == 0:
                return out

        print(f"  Active: {len(active_cells):,} topology+proximity cells "
              f"(no zero-value filter — matches macro behaviour)")

        # Build neighbour CSR using cached conn/offs — no GetCellPoints/GetPointCells.
        # Same mesh → same point numbering → cached connectivity is valid for every file.
        csr_offsets, csr_nbr_ids = _build_active_csr(
            active_cells,
            geo_cache["conn"], geo_cache["offs"],
            geo_cache["pt_cell_offsets"],
            geo_cache["sorted_cell_ids"].astype(np.int64),  # force int64 regardless of cache dtype
        )
        edge_cell_arr  = active_cells
        edge_cell_list = active_cells.tolist()
        print(f"  Neighbour map: {len(csr_nbr_ids):,} edges for {len(active_cells):,} cells")

    elif geo_cache is not None:
        # ── Old-style cache (backward compat) ────────────────────────────────
        edge_cell_list  = geo_cache["edge_cell_list"]
        cell_neighbours = geo_cache.get("cell_neighbours", {})
        edge_cell_arr   = geo_cache.get("edge_cell_arr")
        csr_offsets     = geo_cache.get("csr_offsets")
        csr_nbr_ids     = geo_cache.get("csr_nbr_ids")
    else:
        n_cells = in_arr.GetNumberOfTuples()
        fe = vtk.vtkFeatureEdges()
        fe.SetInputData(src)
        fe.BoundaryEdgesOn()
        fe.FeatureEdgesOn()
        fe.SetFeatureAngle(FEATURE_ANGLE)
        fe.NonManifoldEdgesOff()
        fe.ManifoldEdgesOff()
        fe.ColoringOff()
        fe.Update()
        if _cancelled():
            return None

        edge_pts    = fe.GetOutput().GetPoints()
        edge_pt_ids: set = set()
        if edge_pts and edge_pts.GetNumberOfPoints() > 0:
            _sc    = np.int64(10 ** 10)
            ep_np  = np.round(vtk_to_numpy(edge_pts.GetData()).reshape(-1, 3) * _sc).astype(np.int64)
            ep_keys = set(map(tuple, ep_np.tolist()))
            src_pts = src.GetPoints()
            if src_pts:
                sp_np     = np.round(vtk_to_numpy(src_pts.GetData()).reshape(-1, 3) * _sc).astype(np.int64)
                sp_tuples = list(map(tuple, sp_np.tolist()))
                edge_pt_ids = {pid for pid, key in enumerate(sp_tuples) if key in ep_keys}
        print(f"  Feature-edge points : {len(edge_pt_ids)}")

        _pr = proximity_radius if proximity_radius is not None else SMOOTH_PROXIMITY_RADIUS
        if _pr > 0.0 and edge_pts and edge_pts.GetNumberOfPoints() > 0:
            locator = vtk.vtkKdTreePointLocator()
            locator.SetDataSet(src)
            locator.BuildLocator()
            prox_result   = vtk.vtkIdList()
            n_before_prox = len(edge_pt_ids)
            for i in range(edge_pts.GetNumberOfPoints()):
                locator.FindPointsWithinRadius(_pr, edge_pts.GetPoint(i), prox_result)
                for j in range(prox_result.GetNumberOfIds()):
                    edge_pt_ids.add(prox_result.GetId(j))
            n_prox_added = len(edge_pt_ids) - n_before_prox
            if n_prox_added:
                print(f"  Proximity expansion : +{n_prox_added} points within {_pr} units")

        edge_cells: set = set()
        cell_pts = vtk.vtkIdList()
        if edge_pt_ids:
            polys = src.GetPolys()
            if polys is not None and polys.GetNumberOfCells() > 0:
                conn   = vtk_to_numpy(polys.GetConnectivityArray())
                offs   = vtk_to_numpy(polys.GetOffsetsArray())
                ep_arr = np.fromiter(edge_pt_ids, dtype=np.int64)
                mask   = np.zeros(src.GetNumberOfPoints(), dtype=bool)
                mask[ep_arr] = True
                for cid in range(len(offs) - 1):
                    if mask[conn[offs[cid]:offs[cid + 1]]].any():
                        edge_cells.add(cid)
            else:
                for cid in range(n_cells):
                    src.GetCellPoints(cid, cell_pts)
                    for k in range(cell_pts.GetNumberOfIds()):
                        if cell_pts.GetId(k) in edge_pt_ids:
                            edge_cells.add(cid)
                            break
        print(f"  Edge-ring cells     : {len(edge_cells)}")

        if not edge_cells:
            print("  Nothing to smooth.")
            return out
        if _cancelled():
            return None

        edge_cell_list = list(edge_cells)
        nbr_ids = vtk.vtkIdList()
        cell_pts_nb = vtk.vtkIdList()
        cell_neighbours: dict[int, list[int]] = {}
        for cid in edge_cell_list:
            src.GetCellPoints(cid, cell_pts_nb)
            nbrs: list[int] = []
            for k in range(cell_pts_nb.GetNumberOfIds()):
                src.GetPointCells(cell_pts_nb.GetId(k), nbr_ids)
                for m in range(nbr_ids.GetNumberOfIds()):
                    ncid = nbr_ids.GetId(m)
                    if ncid != cid:
                        nbrs.append(ncid)
            cell_neighbours[cid] = nbrs
        offsets_fb: list[int] = [0]
        nbr_flat_fb: list[int] = []
        for cid in edge_cell_list:
            nbr_flat_fb.extend(cell_neighbours[cid])
            offsets_fb.append(len(nbr_flat_fb))
        csr_offsets   = np.array(offsets_fb,      dtype=np.int64)
        csr_nbr_ids   = np.array(nbr_flat_fb,     dtype=np.int64)
        edge_cell_arr = np.array(edge_cell_list,  dtype=np.int64)

    if not edge_cell_list:
        print("  Nothing to smooth (empty edge ring).")
        return out

    # ── Scalar phase — vectorised via numpy CSR ───────────────────────────────
    n_iter       = max(1, int(n_iter))
    current_vals = np.copy(raw_vals)
    if csr_nbr_ids is not None and len(csr_nbr_ids) > 0:
        csr_counts  = np.diff(csr_offsets).astype(np.float64)
        # No zero-value filter here: every active cell (with >=1 neighbour) is
        # smoothed unconditionally, matching the ParaView macro's behaviour.
        active      = csr_counts > 0
        for iteration in range(n_iter):
            if _cancelled():
                return None
            nbr_vals  = current_vals[csr_nbr_ids]
            sums      = np.add.reduceat(nbr_vals, csr_offsets[:-1].astype(np.intp))
            next_vals = np.copy(current_vals)
            next_vals[edge_cell_arr[active]] = sums[active] / csr_counts[active]
            current_vals = next_vals
            if n_iter > 1:
                print(f"  Smooth pass {iteration + 1}/{n_iter} done.")
    else:
        for iteration in range(n_iter):
            if _cancelled():
                return None
            next_vals = np.copy(current_vals)
            for cid in edge_cell_list:
                nbrs = cell_neighbours[cid]
                if nbrs:
                    next_vals[cid] = float(np.mean(current_vals[nbrs]))
            current_vals = next_vals
            if n_iter > 1:
                print(f"  Smooth pass {iteration + 1}/{n_iter} done.")

    # ── Sliver correction ────────────────────────────────────────────────────
    # Area-weighted neighbor-mean replacement for degenerate near-zero-area
    # triangles that produce physically incorrect power densities.
    if (geo_cache is not None
            and geo_cache.get("cell_areas") is not None
            and float(geo_cache.get("sliver_threshold", 0.0)) > 0.0
            and geo_cache.get("conn") is not None
            and geo_cache.get("pt_cell_offsets") is not None):
        _ar   = np.asarray(geo_cache["cell_areas"], dtype=np.float64)
        _sthr = float(geo_cache["sliver_threshold"])
        _cn   = geo_cache["conn"]
        _of   = geo_cache["offs"]
        _pc   = geo_cache["pt_cell_offsets"]
        _sc   = geo_cache["sorted_cell_ids"].astype(np.int64)
        _npc  = len(_pc) - 1
        _sm   = (raw_vals > 0.0) & (_ar[:len(raw_vals)] < _sthr)
        _si   = np.where(_sm)[0]
        if len(_si) > 0:
            print(f"  Sliver correction: {len(_si):,} degenerate cell(s) "
                  f"(area < {_sthr:.3e}).")
            _ncorr = 0
            for _cid in _si.tolist():
                _ss, _se = int(_of[_cid]), int(_of[_cid + 1])
                _ns: set[int] = set()
                for _pid in _cn[_ss:_se].tolist():
                    if _pid < _npc:
                        _ps, _pe = int(_pc[_pid]), int(_pc[_pid + 1])
                        for _ncc in _sc[_ps:_pe].tolist():
                            if _ncc != _cid and current_vals[_ncc] != 0.0:
                                _ns.add(int(_ncc))
                if not _ns:
                    continue
                _na = np.array(sorted(_ns), dtype=np.int64)
                _w  = _ar[_na]
                _ws = float(_w.sum())
                current_vals[_cid] = (float(np.dot(_w, current_vals[_na]) / _ws)
                                      if _ws > 0.0 else float(current_vals[_na].mean()))
                _ncorr += 1
            if _ncorr:
                print(f"  Sliver correction: {_ncorr:,} cell(s) corrected.")

    new_arr = numpy_to_vtk(current_vals, deep=True, array_type=out_arr.GetDataType())
    new_arr.SetName(ARRAY_NAME)
    out.GetCellData().RemoveArray(ARRAY_NAME)
    out.GetCellData().AddArray(new_arr)
    out.GetCellData().SetActiveScalars(ARRAY_NAME)
    return out
