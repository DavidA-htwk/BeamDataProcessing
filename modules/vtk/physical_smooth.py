"""
modules/vtk/physical_smooth.py
------------------------------
Physically-consistent power-density estimation from a Monte Carlo tally.

This is the "physical" smoothing mode.  Unlike the "edge" and "auto" modes it
does not hunt for outliers to repair: it treats the problem as what it is —
estimating a continuous surface power-density field q(x) [W/m2] from a noisy
Monte Carlo tally — and derives the error bar from the same estimator.

Three guarantees, in the order the requirements were stated:

1. ENERGY IS CONSERVED EXACTLY.
   The estimator solves  (M + L) rho' = M rho = P  on the dual mesh graph,
   with M = diag(area) and L a symmetric finite-volume (TPFA) Laplacian.
   Because L has zero row sums, 1^T (M + L) rho' = 1^T M rho', hence

       sum_i A_i rho'_i  ==  sum_i P_i

   identically, at any bandwidth, for any gating, on any mesh.  (In practice
   it holds to the CG solve tolerance; the achieved error is reported.)
   Contrast the "gather" form rho_i = sum_B P / sum_B A used by the reference
   script, which is normalised per *target* cell and therefore duplicates
   power in sparse regions: measured on the DNB duct liner it drifts from
   +0.06 % at a 3-ring neighbourhood to +1.7 % at 20 rings.

2. NO SHARP CUTS.
   (M + L) is an M-matrix, so its inverse is non-negative: the solution is
   strictly positive wherever any power is connected to it.  No ringing, no
   negative overshoot, and a zero-count cell surrounded by hit cells cannot
   survive — it relaxes to the local flux.  Diffusion is intrinsic to the
   surface, so power never leaks across a gap or onto a shadowed back face
   the way a Euclidean ball query does.

3. THE ERROR ESTIMATE COMES FROM THE SMOOTHING.
   Reported per cell as three separate contributions, see `error estimate`
   below.  Statistical error is obtained by parametric bootstrap through the
   *identical* operator, so it stays correct under adaptive bandwidth and
   under normal gating, where an analytic propagation would not.

Adaptive bandwidth
------------------
A single global radius forces a choice between "still noisy in the tails" and
"peak flattened".  Instead `target_rel_error` is treated as a CEILING on the
statistical error, and the bandwidth is fitted to it per cell:

    every cell starts at one cell width (nothing below that is resolvable);
    each pass measures the error that bandwidth actually delivers, and widens
    only the cells that exceed the ceiling, by achieved/target (sigma ~ 1/h).

The schedule is monotone, so a well-sampled peak keeps full resolution and its
gradients, while a sparse tail widens until it meets the ceiling or hits
`max_bandwidth`.  An earlier version picked the bandwidth analytically from a
pilot flux estimate; that is not done, because a box average at the maximum
bandwidth folds in un-illuminated area wherever the footprint is smaller than
the box and so asks for a far wider kernel than the statistics need (measured:
14.6 mm requested where 5 mm suffices, driving chi2/dof to 69).

Monte Carlo counts
------------------
`Deposited_Power_W` from BeamOnTarget is quantised: P = w * N with w the
per-macro-particle weight and N an integer count.  w is recovered as the
smallest positive deposit, which makes the per-cell particle count — and
hence a first-principles Poisson error — available with no extra metadata.
The Poisson model is then calibrated against the data itself via the
adjacent-pair dispersion statistic (see `estimate_dispersion`).

Error estimate
--------------
    Power_Density_sigma_stat_W_m2  1-sigma Monte Carlo counting error,
                                   propagated through the actual operator by
                                   parametric bootstrap.
    Power_Density_bias_W_m2        smoothing-induced error, |rho_h - rho_2h|
                                   (bandwidth sensitivity / Richardson).
    Power_Density_p95_W_m2         rho + 1.645 * sqrt(sigma^2 + bias^2),
                                   the design-relevant upper bound.
    Smoothing_N_eff                effective macro-particles behind each cell.
    Smoothing_bandwidth_m          the bandwidth actually applied.

plus, in field data, the input/output power totals, the achieved conservation
error, the calibrated dispersion factor and chi2/dof.  chi2/dof is the Pearson
fit of the raw counts to the counts the smoothed field predicts, and is the
bandwidth justification: ~1 means exactly as much smoothing as the statistics
support, >1 means real structure is being erased, <1 means the field is still
following the noise.  Measured on the DNB duct liner: 2.22 at a two-cell
bandwidth floor, 1.10 at one cell, 0.76 at a quarter cell.

Public API
----------
    precompute_diffusion_geometry(src, ...) -> dict   (once per component)
    physical_smooth(src, geo_cache=..., ...)  -> vtkPolyData
"""

from __future__ import annotations

import numpy as np
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

from modules.core.settings import (
    ARRAY_NAME, POWER_ARRAY,
    PHYS_TARGET_REL_ERROR, PHYS_MAX_BANDWIDTH_M, PHYS_MIN_BANDWIDTH_FACTOR,
    PHYS_FEATURE_ANGLE, PHYS_FEATURE_ANGLE_SOFT, PHYS_SUBSTEPS,
    PHYS_BOOTSTRAP_REPLICAS, PHYS_CG_TOL, PHYS_CG_MAXITER, PHYS_BAND_FACTOR,
    PHYS_REFINE_PASSES, PHYS_REFINE_PROBES, PHYS_BOOTSTRAP_BLOCK,
)

# Output array names.
SIGMA_ARRAY = "Power_Density_sigma_stat_W_m2"
BIAS_ARRAY  = "Power_Density_bias_W_m2"
P95_ARRAY   = "Power_Density_p95_W_m2"
NEFF_ARRAY  = "Smoothing_N_eff"
BW_ARRAY    = "Smoothing_bandwidth_m"

# Median of a chi^2_1 variate — used to turn the median adjacent-pair
# statistic into a robust dispersion factor (the mean is useless here: it is
# dominated by the minority of pairs that straddle genuine shadow edges).
_CHI2_1_MEDIAN = 0.4549364231195728

_Z95 = 1.6448536269514722


# ── Geometry pre-computation (once per component) ────────────────────────────

def precompute_diffusion_geometry(
        src: vtk.vtkPolyData,
        feature_angle: float = PHYS_FEATURE_ANGLE,
        feature_angle_soft: float = PHYS_FEATURE_ANGLE_SOFT,
        log_fn=None,
) -> dict | None:
    """Build the dual-graph diffusion operator for a mesh.

    Depends only on topology and vertex positions, so it is identical for
    every VTP of the same component — compute once, reuse for every file,
    exactly like precompute_smooth_geometry() does for the other modes.

    The returned transmissibilities carry the geometric factor only
    (l_ij / d_ij, times the feature-angle gate); the per-file solve multiplies
    them by the local diffusivity kappa = h^2 / 4.

    feature_angle / feature_angle_soft
        Dihedral gate.  Conductance across an edge is 1 below
        `feature_angle_soft` degrees, ramps smoothly to 0 at `feature_angle`,
        and is 0 above.  This stops beam power diffusing around a corner onto
        a shadowed face — measured on the DNB duct liner, ~31 % of the
        zero-count cells adjacent to hot cells sit across such an edge and are
        *correctly* zero.  The ramp (rather than a hard cut) is what keeps
        requirement 2 intact: a hard cut would reintroduce a discontinuity at
        the gate boundary.

        Note this threshold is mesh-resolution dependent: on a fine mesh a
        tight fillet turns by several degrees per cell, so an aggressively
        small angle can sever a genuinely continuous curved surface.  The
        default matches FEATURE_ANGLE used elsewhere in the pipeline.

    Returns a dict, or None if the mesh has no usable triangles.
    """
    def _log(msg: str) -> None:
        print(msg)
        if log_fn:
            log_fn(msg)

    polys = src.GetPolys()
    if polys is None or polys.GetNumberOfCells() == 0:
        _log("    [PHYS] No polygons - cannot build diffusion operator.")
        return None

    conn = vtk_to_numpy(polys.GetConnectivityArray()).astype(np.int64)
    offs = vtk_to_numpy(polys.GetOffsetsArray()).astype(np.int64)
    sizes = np.diff(offs)
    n_cells = len(sizes)
    if not np.all(sizes == 3):
        _log(f"    [PHYS] Mesh is not purely triangular "
             f"({int((sizes != 3).sum()):,} non-triangles) - not supported.")
        return None

    pts = vtk_to_numpy(src.GetPoints().GetData()).reshape(-1, 3).astype(np.float64)
    n_pts = len(pts)
    tri = conn.reshape(-1, 3)
    _log(f"    [PHYS] Mesh: {n_cells:,} triangles, {n_pts:,} points")

    # ── Areas, centroids, normals (vectorised) ───────────────────────────────
    v0, v1, v2 = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    twice_area = np.linalg.norm(cross, axis=1)
    areas = 0.5 * twice_area
    centroids = (v0 + v1 + v2) / 3.0
    with np.errstate(invalid="ignore", divide="ignore"):
        normals = cross / twice_area[:, None]
    normals[~np.isfinite(normals).all(axis=1)] = 0.0

    n_degenerate = int((areas <= 0.0).sum())
    if n_degenerate:
        _log(f"    [PHYS] {n_degenerate:,} degenerate zero-area cell(s) "
             f"- excluded from the area measure, they carry no energy.")

    # ── Manifold dual edges ──────────────────────────────────────────────────
    # Each triangle contributes three undirected point-pairs; an edge shared by
    # exactly two triangles becomes one dual-graph edge.
    e = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]], axis=0)
    e.sort(axis=1)
    owner = np.tile(np.arange(n_cells, dtype=np.int64), 3)
    key = e[:, 0] * np.int64(n_pts) + e[:, 1]
    order = np.argsort(key, kind="stable")
    key, owner = key[order], owner[order]
    e = e[order]
    dup = np.where(key[1:] == key[:-1])[0]
    # Drop non-manifold edges (three or more owners): a run of length > 2 would
    # otherwise contribute several overlapping pairs.
    if len(dup) > 1:
        runs_ok = np.ones(len(dup), dtype=bool)
        triple = np.where(dup[1:] == dup[:-1] + 1)[0]
        if len(triple):
            runs_ok[triple] = False
            runs_ok[triple + 1] = False
        dup = dup[runs_ok]
    ei, ej = owner[dup], owner[dup + 1]
    shared = e[dup]

    edge_len = np.linalg.norm(pts[shared[:, 0]] - pts[shared[:, 1]], axis=1)
    dist = np.linalg.norm(centroids[ei] - centroids[ej], axis=1)
    _log(f"    [PHYS] Dual graph: {len(ei):,} manifold edges")

    # ── Feature-angle gate (smooth ramp, never a hard cut) ───────────────────
    dot = np.clip(np.einsum("ij,ij->i", normals[ei], normals[ej]), -1.0, 1.0)
    angle = np.degrees(np.arccos(dot))
    hard, soft = float(feature_angle), float(feature_angle_soft)
    if hard <= soft:
        hard = soft + 1e-6
    x = np.clip((angle - soft) / (hard - soft), 0.0, 1.0)
    gate = 1.0 - x * x * (3.0 - 2.0 * x)          # smoothstep, C1 at both ends
    n_cut = int((gate <= 0.0).sum())
    _log(f"    [PHYS] Feature gate: {n_cut:,} edge(s) fully closed "
         f"(>{hard:g} deg), ramp from {soft:g} deg")

    # Geometric transmissibility (dimensionless); kappa [m^2] is applied later.
    with np.errstate(divide="ignore", invalid="ignore"):
        trans_geom = gate * edge_len / dist
    bad = ~np.isfinite(trans_geom) | (dist <= 0.0) | (areas[ei] <= 0.0) | (areas[ej] <= 0.0)
    trans_geom[bad] = 0.0

    keep = trans_geom > 0.0
    ei, ej, trans_geom, dist = ei[keep], ej[keep], trans_geom[keep], dist[keep]
    _log(f"    [PHYS] Active dual edges: {len(ei):,}")

    return {
        "kind":        "physical",
        "n_cells":     n_cells,
        "n_pts":       n_pts,
        "areas":       areas,
        "centroids":   centroids,
        "edge_i":      ei,
        "edge_j":      ej,
        "trans_geom":  trans_geom,
        "edge_dist":   dist,
        "n_degenerate": n_degenerate,
    }


# ── Sparse linear algebra (numpy only — no scipy dependency) ─────────────────

def _build_csr_structure(ei, ej, n):
    """Symmetric edge list -> CSR structure, independent of the edge weights.

    The matvec is the inner loop of every solve, so it is worth paying once to
    get it down to a single gather + reduceat instead of two bincounts over a
    doubled edge list.  The ordering depends only on the topology, so it is
    built once and reused for the pilot, main, bias and bootstrap operators,
    which differ only in their weights (see `_csr_values`).
    """
    src = np.concatenate([ei, ej])
    dst = np.concatenate([ej, ei])
    order = np.argsort(src, kind="stable")
    offsets = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(src[order], minlength=n), out=offsets[1:])
    empty = offsets[1:] == offsets[:-1]
    return offsets, dst[order], order, empty


def _csr_values(trans, perm):
    """Edge weights in CSR order for a structure built by _build_csr_structure."""
    return np.concatenate([trans, trans])[perm]


def _row_reduce(values, offsets, empty):
    """Per-row sums of a CSR value array; `values` may carry a column axis.

    np.add.reduceat rejects a start index equal to len(values), which is what
    an isolated trailing row produces, so starts are clamped and the rows that
    own no entries are zeroed afterwards.
    """
    if len(values) == 0:
        return np.zeros((len(offsets) - 1,) + values.shape[1:])
    starts = np.minimum(offsets[:-1], len(values) - 1)
    acc = np.add.reduceat(values, starts, axis=0)
    if empty.any():
        acc[empty] = 0.0
    return acc


def _spmv(x, diag, offsets, nbr, val, empty):
    """(M + L) @ X for the symmetric TPFA operator, from CSR neighbour arrays.

    X may be a single vector or an (n, k) block.  Solving the bootstrap
    replicas as a block rather than one at a time reuses a single pass over
    the neighbour and weight arrays for all k right-hand sides, which is most
    of the memory traffic — the replicas differ only in their values.
    """
    if x.ndim == 1:
        return diag * x - _row_reduce(val * x[nbr], offsets, empty)
    return diag[:, None] * x - _row_reduce(val[:, None] * x[nbr], offsets, empty)


def _cg(rhs, diag, offsets, nbr, val, empty, n, tol, maxiter, x0=None):
    """Jacobi-preconditioned conjugate gradient for the SPD operator (M + L).

    The operator is an M-matrix with a strictly positive diagonal wherever a
    cell has area or a connection, so plain diagonal preconditioning already
    gives a well-behaved iteration (condition number scales as (h/dx)^2, so a
    few tens of iterations suffice at the bandwidths used here).

    Returns (x, iterations, relative_residual).
    """
    block = rhs.ndim == 2
    if x0 is None:
        x = np.zeros_like(rhs)
    else:
        x = np.broadcast_to(x0[:, None] if block and x0.ndim == 1 else x0,
                            rhs.shape).copy()

    def _dot(a, b):
        """Column-wise inner product, or a scalar for a single vector."""
        return (a * b).sum(axis=0) if block else float(a @ b)

    nrm = np.sqrt(_dot(rhs, rhs))
    if not np.any(nrm > 0.0):
        return np.zeros_like(rhs), 0, 0.0
    nrm = np.where(nrm > 0.0, nrm, 1.0) if block else max(nrm, 1e-300)

    r = rhs - _spmv(x, diag, offsets, nbr, val, empty)
    minv = np.where(diag > 0.0, 1.0 / np.where(diag > 0.0, diag, 1.0), 0.0)
    mv = minv[:, None] if block else minv
    z = mv * r
    p = z.copy()
    rz = _dot(r, z)
    resid = float(np.max(np.sqrt(_dot(r, r)) / nrm))
    for k in range(maxiter):
        if resid < tol:
            return x, k, resid
        if not np.any(rz != 0.0):
            break
        ap = _spmv(p, diag, offsets, nbr, val, empty)
        pap = _dot(p, ap)
        if np.any(pap <= 0.0):
            break
        alpha = rz / pap
        x += alpha * p
        r -= alpha * ap
        z = mv * r
        rz_new = _dot(r, z)
        # A column that has already converged has rz == 0; its update is zero
        # either way, so guard the division rather than dropping the column.
        beta = (np.divide(rz_new, rz, out=np.zeros_like(rz_new), where=rz != 0.0)
                if block else rz_new / rz)
        p = z + beta * p
        rz = rz_new
        resid = float(np.max(np.sqrt(_dot(r, r)) / nrm))
    return x, maxiter, resid


def _diffuse(power, areas, csr, trans, n, substeps, tol, maxiter, x0=None):
    """Apply the conservative diffusion operator to a power vector.

    Solves  (M + L/m) rho <- M rho_prev  m times, so the total kernel second
    moment is <r^2> = 4 * kappa regardless of m.  Several substeps make the
    kernel approach a Gaussian; a single step leaves the Bessel-K0 kernel of
    the resolvent, which is too peaked at the origin.

    `power` is the per-cell integrated power P_i [W]; the result is the
    density rho_i [W/m2].  sum(areas * result) == sum(power) by construction —
    the conservation error equals 1^T r / sum(P), so it stays negligible even
    when CG is stopped well short of full convergence.

    `x0` warm-starts the first substep; passing the unperturbed solution when
    solving a bootstrap replica cuts the iteration count several-fold.
    """
    offsets, nbr, perm, empty = csr
    val = _csr_values(trans / float(substeps), perm)
    diag = areas + _row_reduce(val, offsets, empty)
    rhs = np.array(power, dtype=np.float64, copy=True)
    block = rhs.ndim == 2
    mass = areas[:, None] if block else areas
    if x0 is None:
        rho = np.zeros_like(rhs)
    else:
        rho = np.broadcast_to(x0[:, None] if block and x0.ndim == 1 else x0,
                              rhs.shape).copy()
    it_total, worst = 0, 0.0
    for _ in range(substeps):
        rho, iters, res = _cg(rhs, diag, offsets, nbr, val, empty,
                              n, tol, maxiter, x0=rho)
        it_total += iters
        worst = max(worst, res)
        rhs = mass * rho           # M rho becomes the next right-hand side
    return rho, it_total, worst


# ── Monte Carlo statistics ───────────────────────────────────────────────────

def recover_particle_counts(power: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Recover per-cell macro-particle counts from a quantised power tally.

    BeamOnTarget deposits a fixed weight w per macro-particle, so the tally is
    P = w * N with N integer.  w is the smallest positive deposit.  Returns
    (N, w, quality) where quality is the fraction of non-zero cells whose
    P / w lands within 0.01 of an integer — on the DNB duct liner this is
    0.97, the residual being float32 storage precision.

    If the tally is not quantised (quality low) the caller should fall back to
    a continuous error model; N is still returned as P / w, which then just
    means "power in units of the smallest deposit".
    """
    pos = power[power > 0.0]
    if len(pos) == 0:
        return np.zeros_like(power), 0.0, 0.0
    w = float(pos.min())
    ratio = pos / w
    quality = float((np.abs(ratio - np.round(ratio)) < 0.01).mean())
    counts = np.maximum(power / w, 0.0)
    return counts, w, quality


def estimate_dispersion(
        counts: np.ndarray,
        areas: np.ndarray,
        ei: np.ndarray,
        ej: np.ndarray,
        min_pairs: int = 500,
) -> tuple[float, int]:
    """Calibrate the counting-noise model against the data itself.

    For two edge-adjacent cells under a locally constant flux,
    N_i | (N_i + N_j) = n  ~  Binomial(n, p),  p = A_i / (A_i + A_j), so

        T = (N_i - n p)^2 / (n p (1 - p))

    is chi^2_1 under pure Poisson statistics.  The *median* of T is used, not
    the mean: on real data the mean is 2-13 because a minority of adjacent
    pairs straddle genuine shadow edges (real structure, correctly excluded by
    a median), while the median tracks the bulk counting noise.

    Returns (dispersion_factor, n_pairs).  A factor of 1 means textbook
    Poisson; >1 means overdispersed, so a plain Poisson error bar would be
    optimistic.  Measured 1.17 on the DNB duct liner.  Falls back to 1.0 when
    there are too few usable pairs.
    """
    n = counts[ei] + counts[ej]
    denom = areas[ei] + areas[ej]
    usable = (n >= 10.0) & (denom > 0.0)
    if int(usable.sum()) < min_pairs:
        return 1.0, int(usable.sum())
    p = areas[ei][usable] / denom[usable]
    nn = n[usable]
    var = nn * p * (1.0 - p)
    ok = var > 0.0
    if int(ok.sum()) < min_pairs:
        return 1.0, int(ok.sum())
    t_stat = (counts[ei][usable][ok] - nn[ok] * p[ok]) ** 2 / var[ok]
    factor = float(np.median(t_stat) / _CHI2_1_MEDIAN)
    # Guard against pathological calibration on tiny/degenerate samples.
    factor = float(np.clip(factor, 0.25, 10.0))
    return factor, int(ok.sum())


def _bootstrap_sigma(rho, counts, areas, csr, trans, n, substeps,
                     quantum, dispersion, replicas, tol, maxiter, seed,
                     block_size=PHYS_BOOTSTRAP_BLOCK):
    """1-sigma statistical error of the smoothed field, by parametric bootstrap.

    Each replica resamples the per-cell macro-particle counts from their own
    counting distribution — Poisson, widened by the measured dispersion factor
    — and is pushed through the *identical* operator.  The spread of the
    replicas is the error.  Doing it this way rather than propagating a
    covariance analytically is what keeps the result correct under an adaptive
    bandwidth and a feature gate, where the operator's rows are not something
    there is a closed form for.

    Replicas are warm-started from the unperturbed solution: a replica differs
    from it only by counting noise, so CG converges in a fraction of the
    iterations a cold start would need.
    """
    rng = np.random.default_rng(seed)
    lam = np.maximum(counts, 0.0) / dispersion
    mean_acc = np.zeros(n)
    sq_acc = np.zeros(n)
    done = 0
    while done < replicas:
        k = min(max(1, int(block_size)), replicas - done)
        p_rep = (rng.poisson(np.repeat(lam[:, None], k, axis=1))
                 .astype(np.float64) * (dispersion * quantum))
        rho_rep, _, _ = _diffuse(p_rep, areas, csr, trans, n,
                                 substeps, tol, maxiter, x0=rho)
        mean_acc += rho_rep.sum(axis=1)
        sq_acc += (rho_rep * rho_rep).sum(axis=1)
        done += k
    mean_acc /= replicas
    var = np.maximum(sq_acc / replicas - mean_acc * mean_acc, 0.0)
    return np.sqrt(var * replicas / max(replicas - 1, 1))


# ── Main entry point ─────────────────────────────────────────────────────────

def physical_smooth(
        src: vtk.vtkPolyData,
        geo_cache: dict | None = None,
        target_rel_error: float = PHYS_TARGET_REL_ERROR,
        max_bandwidth: float = PHYS_MAX_BANDWIDTH_M,
        min_bandwidth_factor: float = PHYS_MIN_BANDWIDTH_FACTOR,
        substeps: int = PHYS_SUBSTEPS,
        bootstrap_replicas: int = PHYS_BOOTSTRAP_REPLICAS,
        refine_passes: int = PHYS_REFINE_PASSES,
        refine_probes: int = PHYS_REFINE_PROBES,
        band_factor: float = PHYS_BAND_FACTOR,
        cg_tol: float = PHYS_CG_TOL,
        cg_maxiter: int = PHYS_CG_MAXITER,
        seed: int = 12345,
        stop_event=None,
        log_fn=None,
) -> vtk.vtkPolyData | None:
    """Estimate the power-density field and its uncertainty.

    Returns a new vtkPolyData (src is never modified) carrying the smoothed
    `Power_Density_W_m2`, a consistent `Deposited_Power_W` (= area * density,
    so the two arrays agree and both totals are preserved), and the error
    arrays listed in the module docstring.  Returns None if stop_event fires.

    The input density array is deliberately ignored: `Deposited_Power_W` is
    the raw quantised tally, whereas the shipped `Power_Density_W_m2` has
    already been through a non-conservative neighbourhood average upstream
    (on the DNB duct liner sum(D*A) is 11 % below sum(P)).  Density is
    recomputed here from the tally and the mesh areas.
    """
    def _log(msg: str) -> None:
        print(msg)
        if log_fn:
            log_fn(msg)

    def _cancelled() -> bool:
        return stop_event is not None and stop_event.is_set()

    out = vtk.vtkPolyData()
    out.DeepCopy(src)

    cd_in = src.GetCellData()
    p_vtk = cd_in.GetArray(POWER_ARRAY)
    if p_vtk is None:
        _log(f"  [PHYS] '{POWER_ARRAY}' not found - cannot estimate density.")
        return out

    geo = geo_cache
    if geo is None or geo.get("kind") != "physical":
        geo = precompute_diffusion_geometry(src, log_fn=log_fn)
    if geo is None:
        return out
    if geo["n_cells"] != src.GetNumberOfCells():
        _log(f"  [PHYS] Cache/mesh mismatch ({geo['n_cells']:,} vs "
             f"{src.GetNumberOfCells():,} cells) - rebuilding geometry.")
        geo = precompute_diffusion_geometry(src, log_fn=log_fn)
        if geo is None:
            return out

    n_all = geo["n_cells"]
    areas_all = geo["areas"]
    power_all = vtk_to_numpy(p_vtk).astype(np.float64)
    total_in = float(power_all.sum())
    if total_in <= 0.0:
        _log("  [PHYS] No deposited power in this file - output unchanged.")
        return out

    counts_all, quantum, quality = recover_particle_counts(power_all)
    _log(f"  [PHYS] MC weight w = {quantum:.6e} W  "
         f"(quantisation quality {quality*100:.1f} %), "
         f"total {counts_all.sum():.4g} macro-particles")
    if quality < 0.5:
        _log("  [PHYS] [WARN] Tally does not look quantised - the statistical "
             "error bar assumes P = w*N and may be unreliable.")

    # ── Restrict the solve to a band around the signal ───────────────────────
    # Everything outside is exactly zero for a decaying kernel.  Dropping the
    # edges that leave the band imposes a no-flux boundary, which keeps energy
    # conservation exact rather than merely approximate: no power can escape.
    active = _active_band(geo, power_all > 0.0, max_bandwidth,
                          int(np.ceil(band_factor)))
    n = int(active.sum())
    if n == 0:
        _log("  [PHYS] Empty active band - output unchanged.")
        return out
    idx = np.where(active)[0]
    remap = np.full(n_all, -1, dtype=np.int64)
    remap[idx] = np.arange(n, dtype=np.int64)

    ei_a, ej_a = geo["edge_i"], geo["edge_j"]
    keep = active[ei_a] & active[ej_a]
    ei = remap[ei_a[keep]]
    ej = remap[ej_a[keep]]
    trans_geom = geo["trans_geom"][keep]

    areas = areas_all[idx]
    power = power_all[idx]
    counts = counts_all[idx]
    power_band = float(power.sum())
    _log(f"  [PHYS] Active band: {n:,} cells / {len(ei):,} edges "
         f"({100.0 * power_band / total_in:.4f} % of the power)")

    if _cancelled():
        return None

    # ── Calibrate the counting-noise model ───────────────────────────────────
    dispersion, n_pairs = estimate_dispersion(counts, areas, ei, ej)
    _log(f"  [PHYS] Dispersion factor {dispersion:.3f} from {n_pairs:,} "
         f"adjacent pairs (1.0 = textbook Poisson)")

    csr = _build_csr_structure(ei, ej, n)

    # ── Adaptive bandwidth: start at the resolution floor and widen ──────────
    # `target_rel_error` is a CEILING on the statistical error, not a value to
    # hit: a cell that already beats it with less smoothing is strictly better
    # off left alone.  So the schedule starts every cell at the finest scale
    # the mesh can support — one cell width, since nothing below that is
    # resolvable — and only ever widens, where the measured error demands it.
    #
    # This replaces an analytic pilot estimate of the local flux, which was
    # tried first and abandoned: a box average at the maximum bandwidth folds
    # in un-illuminated area wherever the beam footprint is smaller than the
    # box, understates the flux, and so asks for a far wider kernel than the
    # statistics need (measured: 14.6 mm requested where 5 mm suffices, driving
    # chi2/dof to 69).  Starting from the floor needs no flux estimate at all.
    h_floor = np.maximum(min_bandwidth_factor * np.sqrt(np.maximum(areas, 0.0)),
                         1e-9)
    h = np.clip(h_floor, None, max_bandwidth)

    def _trans_for(bandwidth):
        """Edge transmissibilities for a per-cell bandwidth field.

        The face coefficient is the harmonic mean of the two cells' diffusivity
        — the standard TPFA choice for a variable coefficient.  It keeps the
        operator symmetric, so exact conservation and the M-matrix property
        both survive the adaptivity.
        """
        kappa = 0.25 * bandwidth * bandwidth
        ki, kj = kappa[ei], kappa[ej]
        return trans_geom * np.where(
            ki + kj > 0.0, 2.0 * ki * kj / np.maximum(ki + kj, 1e-300), 0.0)

    # ── Widen the bandwidth until the measured error meets the ceiling ───────
    # Each pass measures the error the current bandwidth actually delivers
    # (short bootstrap) and widens where it exceeds the target: sigma ~ 1/h for
    # a 2-D kernel, so h <- h * (achieved / target).  Clamped to >= 1, so the
    # schedule is monotone and a noisy probe can never shrink a cell below the
    # resolution floor.
    rho = None
    for it in range(max(0, int(refine_passes)) + 1):
        trans = _trans_for(h)
        _log(f"  [PHYS] Bandwidth pass {it}: median {np.median(h)*1e3:.2f} mm, "
             f"p5 {np.percentile(h, 5)*1e3:.2f} mm, "
             f"p95 {np.percentile(h, 95)*1e3:.2f} mm "
             f"(target {target_rel_error*100:.1f} %, cap {max_bandwidth*1e3:.0f} mm)")
        rho, iters, res = _diffuse(power, areas, csr, trans, n,
                                   substeps, cg_tol, cg_maxiter, x0=rho)
        if it >= int(refine_passes) or _cancelled():
            break
        probe = _bootstrap_sigma(rho, counts, areas, csr, trans, n, substeps,
                                 quantum, dispersion, refine_probes,
                                 max(cg_tol, 1e-6), cg_maxiter, seed + 977 * it)
        with np.errstate(divide="ignore", invalid="ignore"):
            achieved = np.where(rho > 0.0, probe / np.maximum(rho, 1e-300), np.nan)
        signal = np.isfinite(achieved) & (power > 0.0)
        if not signal.any():
            break
        global_scale = float(np.median(achieved[signal]) / target_rel_error)
        scale = np.clip(np.where(np.isfinite(achieved),
                                 achieved / target_rel_error, global_scale),
                        1.0, 8.0)
        n_widened = int((scale > 1.01).sum())
        _log(f"  [PHYS]   achieved {np.median(achieved[signal])*100:.2f} % on "
             f"hit cells; widening {n_widened:,} cell(s) over the "
             f"{target_rel_error*100:.1f} % ceiling "
             f"(max {scale.max():.2f}x)")
        if n_widened == 0:
            break
        h = np.clip(h * scale, h_floor, max_bandwidth)
    energy_out = float((areas * rho).sum())
    cons_err = (energy_out - power_band) / power_band if power_band else 0.0
    _log(f"  [PHYS] Main pass: {iters} CG iterations (residual {res:.2e}), "
         f"conservation error {cons_err*100:+.3e} %")
    if res > cg_tol:
        _log(f"  [PHYS] [WARN] CG stopped at the iteration cap. Energy is still "
             f"conserved to {cons_err*100:+.2e} %, but the field is "
             f"under-relaxed; lower PHYS_MAX_BANDWIDTH_M or raise PHYS_CG_MAXITER.")
    if _cancelled():
        return None

    # ── Smoothing bias: bandwidth sensitivity (Richardson) ───────────────────
    rho_2h, _, _ = _diffuse(power, areas, csr, 4.0 * trans, n,
                            substeps, cg_tol, cg_maxiter, x0=rho)
    bias = np.abs(rho - rho_2h)
    _log(f"  [PHYS] Bias (|rho_h - rho_2h|): median "
         f"{_safe_rel(np.median(bias[power > 0]), np.median(rho[power > 0]))}")

    # ── Statistical error: parametric bootstrap through the same operator ────
    sigma = np.zeros(n)
    if bootstrap_replicas > 1:
        sigma = _bootstrap_sigma(rho, counts, areas, csr, trans, n, substeps,
                                 quantum, dispersion, bootstrap_replicas,
                                 max(cg_tol, 1e-7), cg_maxiter, seed)
        _log(f"  [PHYS] Bootstrap: {bootstrap_replicas} replicas, "
             f"median relative sigma "
             f"{_safe_rel(np.median(sigma[power > 0]), np.median(rho[power > 0]))}")

    # N_eff = (rho/sigma)^2, guarded: sigma can underflow to 0 in cells the
    # bootstrap never perturbs, which would square to infinity.
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        ratio = np.where(sigma > 0.0, rho / np.maximum(sigma, 1e-300), 0.0)
    ratio = np.clip(np.nan_to_num(ratio, nan=0.0, posinf=0.0, neginf=0.0),
                    0.0, 1e9)
    n_eff = ratio * ratio

    # ── chi2/dof: is this bandwidth statistically justified? ─────────────────
    # Pearson goodness-of-fit of the raw counts against the counts the
    # smoothed field predicts:  lambda_i = rho_i * A_i / w.  Under the counting
    # model E[(N - lambda)^2] = f * lambda exactly, so the mean of the
    # statistic is 1 with no large-lambda approximation needed.
    #
    # Zero-count cells are deliberately INCLUDED.  Testing only hit cells
    # conditions on an upward fluctuation and inflates the statistic badly
    # wherever lambda is of order 1 — which is exactly the sparse region the
    # diagnostic is supposed to judge.
    lam_pred = np.maximum(rho, 0.0) * areas / max(quantum, 1e-300)
    test = lam_pred > 0.0
    chi2_dof = (float((((counts[test] - lam_pred[test]) ** 2)
                       / (dispersion * lam_pred[test])).mean())
                if int(test.sum()) > 0 else float("nan"))
    # Direction: a wider kernel makes the smoothed field a more rigid model of
    # the counts, so the residuals grow -> chi2 rises.  chi2 > 1 therefore means
    # OVER-smoothed (real structure erased); chi2 < 1 means the field is still
    # following the noise.  Measured on the DNB duct liner: 2.22 at a two-cell
    # bandwidth floor, 1.10 at one cell, 0.76 at a quarter cell.
    verdict = ("over-smoothed - real structure is being erased" if chi2_dof > 1.5
               else "under-smoothed - the field is still following the noise"
               if chi2_dof < 0.5
               else "consistent: as much smoothing as the statistics support")
    _log(f"  [PHYS] chi2/dof of the counts against the smoothed field: "
         f"{chi2_dof:.3f} over {int(test.sum()):,} cells  ({verdict})")

    # ── Scatter back to the full mesh and write ──────────────────────────────
    total = np.sqrt(sigma * sigma + bias * bias)
    full = {
        ARRAY_NAME:   _scatter(rho, idx, n_all),
        POWER_ARRAY:  _scatter(areas * rho, idx, n_all),
        SIGMA_ARRAY:  _scatter(sigma, idx, n_all),
        BIAS_ARRAY:   _scatter(bias, idx, n_all),
        P95_ARRAY:    _scatter(rho + _Z95 * total, idx, n_all),
        NEFF_ARRAY:   _scatter(n_eff, idx, n_all),
        BW_ARRAY:     _scatter(h, idx, n_all),
    }

    cd = out.GetCellData()
    for name, values in full.items():
        existing = cd.GetArray(name)
        vtk_type = existing.GetDataType() if existing is not None else vtk.VTK_DOUBLE
        arr = numpy_to_vtk(np.ascontiguousarray(values, dtype=np.float64),
                           deep=True, array_type=vtk_type)
        arr.SetName(name)
        cd.RemoveArray(name)
        cd.AddArray(arr)
    cd.SetActiveScalars(ARRAY_NAME)

    fd = out.GetFieldData()
    for name, value in (
            ("smoothing_power_in_W",        total_in),
            ("smoothing_power_out_W",       float(full[POWER_ARRAY].sum())),
            ("smoothing_conservation_error", cons_err),
            ("smoothing_dispersion_factor", dispersion),
            ("smoothing_mc_weight_W",       quantum),
            ("smoothing_chi2_per_dof",      chi2_dof),
            ("smoothing_target_rel_error",  float(target_rel_error)),
            ("smoothing_max_bandwidth_m",   float(max_bandwidth)),
    ):
        a = numpy_to_vtk(np.array([value], dtype=np.float64), deep=True)
        a.SetName(name)
        fd.RemoveArray(name)
        fd.AddArray(a)

    _log(f"  [PHYS] Done: sum(P) in {total_in:.4f} W -> out "
         f"{float(full[POWER_ARRAY].sum()):.4f} W  "
         f"({(float(full[POWER_ARRAY].sum()) - total_in) / total_in * 100:+.3e} %); "
         f"peak {rho.max():.4e} W/m2 "
         f"(raw P/A peak "
         f"{np.max(np.where(areas > 0.0, power / np.maximum(areas, 1e-300), 0.0)):.4e})")
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_rel(num: float, den: float) -> str:
    return f"{100.0 * num / den:.2f} %" if den > 0 else "n/a"


def _scatter(values: np.ndarray, idx: np.ndarray, n_all: int) -> np.ndarray:
    full = np.zeros(n_all, dtype=np.float64)
    full[idx] = values
    return full


def _active_band(geo: dict, seed_mask: np.ndarray, voxel: float,
                 layers: int) -> np.ndarray:
    """Cells within roughly `voxel * layers` of a seed cell, by grid dilation.

    A geodesic band would be tighter, but a Euclidean over-estimate is exactly
    what is wanted here: the band only has to be generous enough that the
    kernel has decayed at its edge.  Cells pulled in across a gap cost a few
    unknowns and receive zero, because the *solve* is still restricted to the
    gated dual graph and cannot reach them.

    The voxel is sized at one bandwidth and dilated `layers` times, rather
    than using one giant voxel: same reach, but the band hugs the signal far
    more closely, which is what keeps the linear system small.
    """
    centroids = geo["centroids"]
    if not seed_mask.any():
        return np.zeros(len(centroids), dtype=bool)
    cell = max(voxel, 1e-9)
    origin = centroids.min(axis=0) - cell
    ijk = np.floor((centroids - origin) / cell).astype(np.int64)
    dims = ijk.max(axis=0) + 2
    flat = (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]

    occupied = np.zeros(int(dims.prod()), dtype=bool)
    occupied[flat[seed_mask]] = True
    grid = occupied.reshape(dims)
    for _ in range(max(1, int(layers))):
        nxt = grid.copy()
        for axis in range(3):
            nxt |= np.roll(grid, 1, axis=axis)
            nxt |= np.roll(grid, -1, axis=axis)
        grid = nxt
    return grid.reshape(-1)[flat]
