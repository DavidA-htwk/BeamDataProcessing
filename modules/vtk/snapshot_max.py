"""
snapshot_max.py
---------------
Offscreen VTK snapshot of the cell with the highest scalar value in a
vtkPolyData.

Camera strategy
~~~~~~~~~~~~~~~
1. Find the cell with the maximum value of *array_name*.
2. Compute a robust surface normal via area-weighted averaging over the
   1-ring neighbourhood of the max cell (not just the single-cell cross
   product), giving a stable direction even for small or near-degenerate
   triangles.
3. Cast rays from a disk of sample points centred on the cell centroid
   (centre + 8 equally-spaced ring points, radius ≈ 2 % of bounding-box
   diagonal), all parallel to the normal.  The camera is placed at the
   *minimum* hit distance across all rays.  This prevents a single central
   ray from slipping through a narrow gap between wall panels — any nearby
   wall blocked by a ring ray constrains the camera.
4. If no ray hits anything, fall back to 1.5 × bounding-box diagonal.
5. Set the view-up vector to whichever world axis is least parallel to the
   normal (to avoid gimbal lock).

The polydata is coloured by *array_name* using a blue->red (cool-to-warm)
colour map.  The colour range is fixed to [0, global_max] when *vmax* is
supplied, otherwise per-file.
"""

from __future__ import annotations

from pathlib import Path
import math

import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy


def _nice_ticks(vmin: float, vmax: float, n: int = 4) -> list:
    """Return ~n nicely-rounded tick values between vmin and vmax."""
    if vmax <= vmin or not math.isfinite(vmin) or not math.isfinite(vmax):
        return [vmin]
    rng = vmax - vmin
    if rng == 0:
        return [vmin]
    raw_step = rng / n
    mag = 10 ** math.floor(math.log10(raw_step))
    candidates = [mag * m for m in (1, 2, 2.5, 5, 10)]
    step = min(candidates, key=lambda s: abs(s - raw_step))
    start = math.ceil(vmin / step) * step
    ticks: list = []
    v = start
    while v <= vmax + 1e-9 * rng:
        ticks.append(round(v, 10))
        v += step
    return ticks if ticks else [vmin, vmax]


# ── Public entry point ────────────────────────────────────────────────────────

def precompute_snapshot(
    polydata: vtk.vtkPolyData,
    array_name: str,
) -> dict | None:
    """
    Do all CPU-heavy pre-render work on *polydata* (argmax, centroid, normal,
    ray-cast camera placement) and return the results as a plain dict of
    JSON-safe values.  Pass this dict as *precomputed* to save_max_snapshot to
    skip repeating that work inside the render worker.

    Returns None if *array_name* is not present in the cell data.
    """
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None or arr.GetNumberOfTuples() == 0:
        return None

    vals    = vtk_to_numpy(arr)
    max_cid = int(np.argmax(vals))
    max_val = float(vals[max_cid])

    centroid = _cell_centroid(polydata, max_cid)
    normal   = _robust_normal(polydata, max_cid)
    diag     = _bbox_diagonal(polydata)
    cam_pos  = _camera_pos_with_raycast(polydata, centroid, normal, diag, diag * 1.5)

    return {
        "max_cid":  max_cid,
        "max_val":  max_val,
        "centroid": centroid,
        "normal":   normal,
        "cam_pos":  cam_pos,
        "diag":     diag,
    }

def save_max_snapshot(
    polydata: vtk.vtkPolyData,
    array_name: str,
    out_path: Path,
    vmax: float | None = None,
    image_size: tuple[int, int] = (1200, 900),
    precomputed: dict | None = None,
    total_power_W: float | None = None,
) -> bool:
    """
    Render *polydata* coloured by *array_name* and save a PNG to *out_path*.

    Layout: image is split into two dedicated renderers so actors never overlap.
      Left 75%  — mesh renderer: geometry, sphere marker, axes triad, max label
      Right 25% — scalar-bar panel: colour bar + rotated array-name title

    Parameters
    ----------
    polydata    : source geometry (cell data must contain *array_name*)
    array_name  : scalar array used for colouring and max detection
    out_path    : full path for the output PNG (parent dir must exist)
    vmax        : upper bound of colour range; if None uses the file maximum
    image_size  : (width, height) in pixels
    precomputed : dict returned by precompute_snapshot(); if supplied, skips
                  the expensive argmax / centroid / normal / locator work.

    Returns True on success, False if the array was not found.
    """
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return False

    n = arr.GetNumberOfTuples()
    if n == 0:
        return False

    # ── Find max cell (numpy argmax -- no Python loop) ────────────────────────
    if precomputed is not None:
        max_cid       = precomputed["max_cid"]
        max_val       = precomputed["max_val"]
        cell_centroid = precomputed["centroid"]
        cell_normal   = precomputed["normal"]
        _cam_pos_pre  = precomputed["cam_pos"]
        diag          = precomputed["diag"]
    else:
        vals    = vtk_to_numpy(arr)
        max_cid = int(np.argmax(vals))
        max_val = float(vals[max_cid])
        cell_centroid = _cell_centroid(polydata, max_cid)
        cell_normal   = _robust_normal(polydata, max_cid)
        _cam_pos_pre  = None
        diag          = None

    # ── Colour map ────────────────────────────────────────────────────────────
    # Human-readable labels and units for known arrays.
    _LABEL_MAP = {
        "Power_Density_W_m2": "Power Density [W/m\u00b2]",
        "Deposited_Power_W":  "Power [W]",
    }
    _UNIT_MAP = {
        "Power_Density_W_m2": "W/m\u00b2",
        "Deposited_Power_W":  "W",
    }
    bar_label  = _LABEL_MAP.get(array_name, array_name)
    unit_label = _UNIT_MAP.get(array_name, "")

    color_min = 0.0
    color_max = float(vmax) if vmax is not None else float(max_val)

    lut = vtk.vtkLookupTable()
    lut.SetTableRange(color_min, color_max)
    lut.SetHueRange(0.667, 0.0)   # blue → red
    lut.SetSaturationRange(1, 1)
    lut.SetValueRange(1, 1)
    lut.SetNumberOfTableValues(256)
    lut.Build()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)
    mapper.SetScalarModeToUseCellData()
    mapper.SelectColorArray(array_name)
    mapper.SetLookupTable(lut)
    mapper.SetScalarRange(color_min, color_max)
    mapper.ScalarVisibilityOn()

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    # ── Max-point sphere marker + max label (Power Density only) ──────────────
    # For Total Power the marker and label are omitted: the max-power cell is
    # typically a large-area edge cell whose location is not physically meaningful
    # as a "hot spot", so showing it would be misleading.
    _is_pwr_density = (array_name == "Power_Density_W_m2")

    sphere_src = vtk.vtkSphereSource()
    sphere_src.SetCenter(*cell_centroid)
    sphere_src.SetPhiResolution(12)
    sphere_src.SetThetaResolution(12)
    sphere_mapper = vtk.vtkPolyDataMapper()
    sphere_mapper.SetInputConnection(sphere_src.GetOutputPort())
    sphere_actor = vtk.vtkActor()
    sphere_actor.SetMapper(sphere_mapper)
    sphere_actor.GetProperty().SetColor(0.498, 0.0, 1.0)   # #7F00FF violet
    sphere_actor.SetVisibility(1 if _is_pwr_density else 0)

    # ── Max value label (top-left of mesh viewport) ───────────────────────────
    max_label = vtk.vtkTextActor()
    max_label.SetInput(f"[*] Max: {color_max:.6g} {unit_label}")
    ml = max_label.GetTextProperty()
    ml.SetColor(0.498, 0.0, 1.0)
    ml.SetFontSize(18)
    ml.SetFontFamilyToTimes()
    ml.BoldOn()
    ml.ItalicOff()
    ml.SetBackgroundColor(1.0, 1.0, 1.0)
    ml.SetBackgroundOpacity(0.85)
    max_label.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    max_label.SetPosition(0.02, 0.94)
    max_label.SetVisibility(1 if _is_pwr_density else 0)

    # ── Total-power title (top-centre, always visible) ────────────────────────
    if total_power_W is not None:
        if abs(total_power_W) >= 1000.0:
            _tp_str = f"Tot. Power = {total_power_W / 1000:.4g} kW"
        else:
            _tp_str = f"Tot. Power = {total_power_W:.4g} W"
        tot_pwr_label = vtk.vtkTextActor()
        tot_pwr_label.SetInput(_tp_str)
        tpl = tot_pwr_label.GetTextProperty()
        tpl.SetColor(0.1, 0.1, 0.1)
        tpl.SetFontSize(18)
        tpl.SetFontFamilyToTimes()
        tpl.BoldOn()
        tpl.ItalicOff()
        tpl.SetJustificationToCentered()
        tpl.SetBackgroundColor(1.0, 1.0, 1.0)
        tpl.SetBackgroundOpacity(0.80)
        tot_pwr_label.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
        tot_pwr_label.SetPosition(0.50, 0.96)
    else:
        tot_pwr_label = None
    _MESH_VP = 0.75   # fraction of total width given to the mesh
    mesh_renderer = vtk.vtkRenderer()
    mesh_renderer.SetViewport(0.0, 0.0, _MESH_VP, 1.0)
    mesh_renderer.SetBackground(1, 1, 1)
    mesh_renderer.AddActor(actor)
    mesh_renderer.AddActor(sphere_actor)
    mesh_renderer.AddActor2D(max_label)
    if tot_pwr_label is not None:
        mesh_renderer.AddActor2D(tot_pwr_label)

    # ── Right renderer: scalar bar panel (25% of image width) ─────────────────
    sbar_renderer = vtk.vtkRenderer()
    sbar_renderer.SetViewport(_MESH_VP, 0.0, 1.0, 1.0)
    sbar_renderer.SetBackground(0.95, 0.95, 0.95)   # light grey panel

    # Scalar bar fills its dedicated panel — no frosted overlay needed, the
    # panel background is clean.  Width/height are in sbar_renderer's own
    # normalised coords (0–1 within that 25% strip).
    scalar_bar = vtk.vtkScalarBarActor()
    scalar_bar.SetLookupTable(lut)
    scalar_bar.SetTitle("")
    scalar_bar.SetNumberOfLabels(6)
    scalar_bar.SetWidth(0.40)
    scalar_bar.SetHeight(0.88)
    scalar_bar.SetPosition(0.08, 0.06)
    try:
        scalar_bar.SetTextPad(4)
    except AttributeError:
        pass
    sb_lbl = scalar_bar.GetLabelTextProperty()
    sb_lbl.SetColor(0.0, 0.0, 0.0)
    sb_lbl.SetFontSize(14)
    sb_lbl.SetFontFamilyToTimes()
    sb_lbl.ItalicOff()
    sb_lbl.BoldOff()
    scalar_bar.UnconstrainedFontSizeOn()
    scalar_bar.DrawBackgroundOff()
    scalar_bar.DrawFrameOff()
    sbar_renderer.AddActor2D(scalar_bar)

    # Rotated array-name title on the right edge of the scalar bar panel
    sbar_title = vtk.vtkTextActor()
    sbar_title.SetInput(bar_label)
    sbt = sbar_title.GetTextProperty()
    sbt.SetColor(0.0, 0.0, 0.0)
    sbt.SetFontSize(16)
    sbt.SetFontFamilyToTimes()
    sbt.ItalicOn()
    sbt.BoldOff()
    sbt.SetOrientation(90)
    sbar_title.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    sbar_title.SetPosition(0.82, 0.06)
    sbar_renderer.AddActor2D(sbar_title)

    # ── Camera in mesh renderer ───────────────────────────────────────────────
    if diag is None:
        diag = _bbox_diagonal(polydata)
    cam_pos = _place_camera(
        mesh_renderer, cell_centroid, cell_normal, diag,
        polydata=None if _cam_pos_pre is not None else polydata,
        cam_pos_override=_cam_pos_pre,
    )
    mesh_renderer.ResetCameraClippingRange()

    cam_dist = ((cam_pos[0] - cell_centroid[0])**2 +
                (cam_pos[1] - cell_centroid[1])**2 +
                (cam_pos[2] - cell_centroid[2])**2) ** 0.5 or diag
    sphere_src.SetRadius(cam_dist * 0.0048)

    # ── Camera-aligned scale bars ─────────────────────────────────────────────
    # Two L-shaped scale bars drawn in world space along the camera's own right
    # and up axes — NOT global X/Y/Z.  This means the bars always appear
    # horizontal and vertical in the rendered image regardless of how the
    # geometry is oriented in world space.
    #
    # Bar length is a nice 1-2-5 rounded number ≈ 20 % of the world-space
    # viewport width at the focal distance, so it rescales automatically when
    # the camera is closer to or farther from the geometry.
    _cam_obj = mesh_renderer.GetActiveCamera()
    _cam_c   = np.array(_cam_obj.GetPosition(),   dtype=float)
    _foc_c   = np.array(_cam_obj.GetFocalPoint(),  dtype=float)
    _up_c    = np.array(_cam_obj.GetViewUp(),      dtype=float)

    _vd_c  = _foc_c - _cam_c
    _vd_l  = float(np.linalg.norm(_vd_c))
    _vd_c  = _vd_c / _vd_l if _vd_l > 0 else np.array([0.0, 0.0, -1.0])

    _r_c   = np.cross(_vd_c, _up_c)
    _r_l   = float(np.linalg.norm(_r_c))
    _r_c   = _r_c / _r_l if _r_l > 0 else np.array([1.0, 0.0, 0.0])

    _tu_c  = np.cross(_r_c, _vd_c)           # corrected camera-up (perp. to right & view)
    _tu_l  = float(np.linalg.norm(_tu_c))
    _tu_c  = _tu_c / _tu_l if _tu_l > 0 else np.array([0.0, 1.0, 0.0])

    # World-space half-extents of the mesh viewport at the focal plane
    _fd = _vd_l or float(diag)
    if _cam_obj.GetParallelProjection():
        _half_h = float(_cam_obj.GetParallelScale())
    else:
        _half_h = _fd * math.tan(math.radians(_cam_obj.GetViewAngle() / 2.0))
    _half_w = _half_h * (image_size[0] * _MESH_VP / image_size[1])

    # Nice 1-2-5 bar length ≈ 20 % of world viewport width
    _tgt = _half_w * 0.35
    if _tgt > 0 and math.isfinite(_tgt):
        _mag = 10 ** math.floor(math.log10(_tgt))
        _bar = min([_mag * m for m in (1, 2, 2.5, 5, 10)],
                   key=lambda s: abs(s - _tgt))
    else:
        _bar = float(diag) * 0.2

    # Anchor: bottom-left corner of the focal-plane viewport
    _anc = _foc_c - _half_w * 0.80 * _r_c - _half_h * 0.80 * _tu_c
    _tk  = _half_h * 0.04          # end-tick arm length
    _lbl = f"{_bar:.4g} m"

    # ── Scale-bar overlay renderer ────────────────────────────────────────────
    # Layer 1 renders after the mesh layer.  Clearing the depth buffer for this
    # layer (but preserving the colour buffer) means scale bars are ALWAYS drawn
    # in front of the geometry, no matter where they sit in 3-D world space.
    _overlay_r = vtk.vtkRenderer()
    _overlay_r.SetViewport(0.0, 0.0, _MESH_VP, 1.0)
    _overlay_r.SetLayer(1)
    _overlay_r.SetErase(1)              # clear buffers for this layer
    _overlay_r.SetPreserveColorBuffer(1)  # …but keep the mesh colours
    # depth buffer IS cleared (default) → scale bars always win depth test

    # Build a single-segment VTK line actor
    def _sbar_seg(p0, p1, lw):
        _pts = vtk.vtkPoints()
        _pts.InsertNextPoint(float(p0[0]), float(p0[1]), float(p0[2]))
        _pts.InsertNextPoint(float(p1[0]), float(p1[1]), float(p1[2]))
        _ca = vtk.vtkCellArray()
        _ca.InsertNextCell(2)
        _ca.InsertCellPoint(0)
        _ca.InsertCellPoint(1)
        _pd = vtk.vtkPolyData()
        _pd.SetPoints(_pts)
        _pd.SetLines(_ca)
        _mp = vtk.vtkPolyDataMapper()
        _mp.SetInputData(_pd)
        _ac = vtk.vtkActor()
        _ac.SetMapper(_mp)
        _ac.GetProperty().SetColor(0.0, 0.0, 0.0)
        _ac.GetProperty().SetLineWidth(lw)
        _overlay_r.AddActor(_ac)

    def _sbar_lbl(pos, text):
        _t = vtk.vtkBillboardTextActor3D()
        _t.SetInput(text)
        _t.SetPosition(float(pos[0]), float(pos[1]), float(pos[2]))
        _tp = _t.GetTextProperty()
        _tp.SetColor(0.0, 0.0, 0.0)
        _tp.SetFontSize(12)
        _tp.BoldOn()
        _tp.ItalicOff()
        _tp.SetBackgroundColor(1.0, 1.0, 1.0)
        _tp.SetBackgroundOpacity(0.75)
        _overlay_r.AddActor(_t)

    # Horizontal bar (camera-right direction)
    _h0 = _anc.copy()
    _h1 = _h0 + _bar * _r_c
    _sbar_seg(_h0, _h1, 2.5)
    _sbar_seg(_h0 - _tk * _tu_c, _h0 + _tk * _tu_c, 1.5)   # left tick
    _sbar_seg(_h1 - _tk * _tu_c, _h1 + _tk * _tu_c, 1.5)   # right tick
    _sbar_lbl((_h0 + _h1) * 0.5 - _tk * 2.2 * _tu_c, _lbl)

    # Vertical bar (camera-up direction)
    _v0 = _anc.copy()
    _v1 = _v0 + _bar * _tu_c
    _sbar_seg(_v0, _v1, 2.5)
    _sbar_seg(_v0 - _tk * _r_c, _v0 + _tk * _r_c, 1.5)     # bottom tick
    _sbar_seg(_v1 - _tk * _r_c, _v1 + _tk * _r_c, 1.5)     # top tick
    _sbar_lbl((_v0 + _v1) * 0.5 - _tk * 2.2 * _r_c, _lbl)

    # Share the exact same camera so world-space bar positions match the mesh view
    _overlay_r.SetActiveCamera(mesh_renderer.GetActiveCamera())

    # ── Render window (offscreen) — MSAA disabled for speed ───────────────────
    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetMultiSamples(0)
    render_window.SetSize(*image_size)
    render_window.SetNumberOfLayers(2)
    render_window.AddRenderer(mesh_renderer)
    render_window.AddRenderer(sbar_renderer)
    render_window.AddRenderer(_overlay_r)
    render_window.Render()

    # ── Save PNG ──────────────────────────────────────────────────────────────
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(render_window)
    w2i.Update()

    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(out_path))
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()

    render_window.Finalize()
    return True


# ── Internal helpers ──────────────────────────────────────────────────────────

def _cell_centroid(polydata: vtk.vtkPolyData, cid: int) -> tuple[float, float, float]:
    """Return the arithmetic mean of a cell's point coordinates."""
    id_list = vtk.vtkIdList()
    polydata.GetCellPoints(cid, id_list)
    pts = polydata.GetPoints()
    cx = cy = cz = 0.0
    n = id_list.GetNumberOfIds()
    for k in range(n):
        x, y, z = pts.GetPoint(id_list.GetId(k))
        cx += x; cy += y; cz += z
    if n:
        cx /= n; cy /= n; cz /= n
    return cx, cy, cz


def _robust_normal(
    polydata: vtk.vtkPolyData,
    cid: int,
) -> tuple[float, float, float]:
    """
    Return an area-weighted average normal over the 1-ring neighbourhood of
    *cid* (the cell itself plus all cells sharing a point with it).

    Area-weighting is implicit: the cross product magnitude equals twice the
    triangle area, so larger triangles contribute proportionally more to the
    averaged direction.  This gives a smooth, outlier-resistant normal even
    when the max cell is a small or nearly-degenerate triangle.
    """
    pt_ids  = vtk.vtkIdList()
    nbr_ids = vtk.vtkIdList()
    pts     = polydata.GetPoints()

    polydata.GetCellPoints(cid, pt_ids)
    seen: set = {cid}
    ring_cids: list[int] = [cid]
    for k in range(pt_ids.GetNumberOfIds()):
        polydata.GetPointCells(pt_ids.GetId(k), nbr_ids)
        for m in range(nbr_ids.GetNumberOfIds()):
            ncid = nbr_ids.GetId(m)
            if ncid not in seen:
                seen.add(ncid)
                ring_cids.append(ncid)

    nx_sum = ny_sum = nz_sum = 0.0
    cell_pt_ids = vtk.vtkIdList()
    for c in ring_cids:
        polydata.GetCellPoints(c, cell_pt_ids)
        if cell_pt_ids.GetNumberOfIds() < 3:
            continue
        p0 = np.asarray(pts.GetPoint(cell_pt_ids.GetId(0)))
        p1 = np.asarray(pts.GetPoint(cell_pt_ids.GetId(1)))
        p2 = np.asarray(pts.GetPoint(cell_pt_ids.GetId(2)))
        n  = np.cross(p1 - p0, p2 - p0)   # magnitude = 2 × area → natural weighting
        nx_sum += n[0]; ny_sum += n[1]; nz_sum += n[2]

    length = (nx_sum**2 + ny_sum**2 + nz_sum**2) ** 0.5
    if length < 1e-10:
        return _cell_normal(polydata, cid)   # degenerate neighbourhood — fall back
    return (nx_sum / length, ny_sum / length, nz_sum / length)


def _cell_normal(polydata: vtk.vtkPolyData, cid: int) -> tuple[float, float, float]:
    """
    Return the surface normal of *cid* via cross product of two cell edges.
    O(1) -- does NOT run vtkPolyDataNormals on the whole mesh.
    Falls back to (0, 0, 1) if the cell is degenerate.
    """
    id_list = vtk.vtkIdList()
    polydata.GetCellPoints(cid, id_list)
    if id_list.GetNumberOfIds() < 3:
        return (0.0, 0.0, 1.0)
    pts = polydata.GetPoints()
    p0 = np.asarray(pts.GetPoint(id_list.GetId(0)))
    p1 = np.asarray(pts.GetPoint(id_list.GetId(1)))
    p2 = np.asarray(pts.GetPoint(id_list.GetId(2)))
    n  = np.cross(p1 - p0, p2 - p0)
    length = float(np.linalg.norm(n))
    if length < 1e-10:
        return (0.0, 0.0, 1.0)
    n = n / length
    return (float(n[0]), float(n[1]), float(n[2]))


def _bbox_diagonal(polydata: vtk.vtkPolyData) -> float:
    bounds = polydata.GetBounds()          # (xmin, xmax, ymin, ymax, zmin, zmax)
    dx = bounds[1] - bounds[0]
    dy = bounds[3] - bounds[2]
    dz = bounds[5] - bounds[4]
    return (dx**2 + dy**2 + dz**2) ** 0.5 or 1.0


def _place_camera(
    renderer: vtk.vtkRenderer,
    focal: tuple[float, float, float],
    normal: tuple[float, float, float],
    diag: float,
    polydata: vtk.vtkPolyData | None = None,
    cam_pos_override: tuple[float, float, float] | None = None,
) -> tuple[float, float, float]:
    """
    Position camera along *normal* from *focal*.
    Returns the camera position so callers can use the distance for scaling.

    If *cam_pos_override* is supplied (pre-computed), use it directly.
    Otherwise if *polydata* is supplied a ray is cast to find the blocking wall.
    Falls back to 1.5 x diag if no blocker is found.
    """
    nx, ny, nz = normal
    default_dist = diag * 1.5

    if cam_pos_override is not None:
        cam_pos = cam_pos_override
    elif polydata is not None:
        cam_pos = _camera_pos_with_raycast(polydata, focal, normal, diag, default_dist)
    else:
        cam_pos = (focal[0] + nx * default_dist,
                   focal[1] + ny * default_dist,
                   focal[2] + nz * default_dist)

    axes = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    dot_abs = [abs(nx * ax[0] + ny * ax[1] + nz * ax[2]) for ax in axes]
    view_up = axes[dot_abs.index(min(dot_abs))]

    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(*focal)
    camera.SetPosition(*cam_pos)
    camera.SetViewUp(*view_up)
    renderer.ResetCameraClippingRange()
    return cam_pos


def _camera_pos_with_raycast(
    polydata: vtk.vtkPolyData,
    focal: tuple[float, float, float],
    normal: tuple[float, float, float],
    diag: float,
    default_dist: float,
    n_sample: int = 8,
    sample_radius_frac: float = 0.02,
) -> tuple[float, float, float]:
    """
    Multi-ray disk sampling camera placement.

    Casts rays from the centroid plus *n_sample* equally-spaced points on a
    circle of radius ``diag * sample_radius_frac`` centred on *focal*, all
    parallel to *normal*.  Takes the **minimum** hit distance across all rays
    so that a wall close to any sample point blocks the camera — preventing
    a single on-axis ray from slipping through a narrow gap between panels.

    Falls back to *default_dist* if no ray hits anything.
    """
    nx, ny, nz = normal
    n_arr      = np.array([nx, ny, nz], dtype=float)
    foc_arr    = np.array(focal,        dtype=float)
    offset     = diag * 0.01       # small push above the surface
    ray_reach  = diag * 2.0        # maximum ray length

    # ── Build tangent frame in the plane perpendicular to normal ──────────────
    # Pick the world axis least parallel to normal as the first tangent seed.
    t_seed = np.array([1.0, 0.0, 0.0]) if abs(nx) < 0.9 else np.array([0.0, 1.0, 0.0])
    t1 = t_seed - np.dot(t_seed, n_arr) * n_arr
    t1_len = float(np.linalg.norm(t1))
    t1 = t1 / t1_len if t1_len > 1e-10 else np.array([0.0, 1.0, 0.0])
    t2 = np.cross(n_arr, t1)

    # ── Sample origins: centre + ring ─────────────────────────────────────────
    radius = diag * sample_radius_frac
    origins = [foc_arr]   # centre ray always included
    for i in range(n_sample):
        angle = 2.0 * math.pi * i / n_sample
        origins.append(foc_arr + radius * (math.cos(angle) * t1
                                           + math.sin(angle) * t2))

    # ── Ray-cast ──────────────────────────────────────────────────────────────
    locator = vtk.vtkCellLocator()
    locator.SetDataSet(polydata)
    locator.BuildLocator()

    hit_dists: list[float] = []
    t       = vtk.reference(0.0)
    x       = [0.0, 0.0, 0.0]
    pcoords = [0.0, 0.0, 0.0]
    sub_id  = vtk.reference(0)
    cell_id = vtk.reference(-1)

    for sp in origins:
        ray_start = (sp + n_arr * offset).tolist()
        ray_end   = (sp + n_arr * ray_reach).tolist()
        hit = locator.IntersectWithLine(
            ray_start, ray_end, 1e-6, t, x, pcoords, sub_id, cell_id,
        )
        if hit:
            dist = float(np.linalg.norm(np.array(x) - foc_arr))
            hit_dists.append(dist)

    if not hit_dists:
        return (focal[0] + nx * default_dist,
                focal[1] + ny * default_dist,
                focal[2] + nz * default_dist)

    # Minimum hit distance — catches the closest wall even if some rays slip
    # through gaps between panels.
    cam_dist = max(float(min(hit_dists)), offset * 2)
    return (focal[0] + nx * cam_dist,
            focal[1] + ny * cam_dist,
            focal[2] + nz * cam_dist)
