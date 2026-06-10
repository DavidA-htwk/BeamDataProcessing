"""
snapshot_max.py
---------------
Offscreen VTK snapshot of the cell with the highest scalar value in a
vtkPolyData.

Camera strategy
~~~~~~~~~~~~~~~
1. Find the cell with the maximum value of *array_name*.
2. Compute that cell's centroid and surface normal (via vtkPolyDataNormals).
3. Cast a ray from the centroid along the normal up to 2 x bounding-box
   diagonal.  If it hits geometry (an opposing wall - e.g. the far side of
   a duct) the camera is placed 1 % before the hit point, sitting inside
   the cavity with the max cell in direct view.  If the ray hits nothing the
   camera falls back to the default 1.5 x diagonal distance.
4. Set the view-up vector to whichever world axis is least parallel to the
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
    normal   = _cell_normal(polydata, max_cid)
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
        cell_normal   = _cell_normal(polydata, max_cid)
        _cam_pos_pre  = None
        diag          = None

    # ── Colour map ────────────────────────────────────────────────────────────
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

    # ── Max-point sphere marker ───────────────────────────────────────────────
    sphere_src = vtk.vtkSphereSource()
    sphere_src.SetCenter(*cell_centroid)
    sphere_src.SetPhiResolution(12)
    sphere_src.SetThetaResolution(12)
    sphere_mapper = vtk.vtkPolyDataMapper()
    sphere_mapper.SetInputConnection(sphere_src.GetOutputPort())
    sphere_actor = vtk.vtkActor()
    sphere_actor.SetMapper(sphere_mapper)
    sphere_actor.GetProperty().SetColor(0.498, 0.0, 1.0)   # #7F00FF violet

    # ── Max value label (top-left of mesh viewport) ───────────────────────────
    # Show the scaled value (color_max) so label matches the CSV and scalar bar.
    max_label = vtk.vtkTextActor()
    max_label.SetInput(f"[*] Max: {color_max:.6g}")
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

    # ── Left renderer: mesh + axes (75% of image width) ──────────────────────
    _MESH_VP = 0.75   # fraction of total width given to the mesh
    mesh_renderer = vtk.vtkRenderer()
    mesh_renderer.SetViewport(0.0, 0.0, _MESH_VP, 1.0)
    mesh_renderer.SetBackground(1, 1, 1)
    mesh_renderer.AddActor(actor)
    mesh_renderer.AddActor(sphere_actor)
    mesh_renderer.AddActor2D(max_label)

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
    sbar_title.SetInput(array_name)
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
) -> tuple[float, float, float]:
    """
    Cast a ray from *focal* along *normal* up to 2 × diag.
    If it hits geometry (a wall on the other side) place the camera 1 %
    before that hit so it ends up inside the cavity looking at the cell.
    If no blocker is found, use *default_dist*.
    """
    nx, ny, nz = normal
    ray_end = (focal[0] + nx * diag * 2,
               focal[1] + ny * diag * 2,
               focal[2] + nz * diag * 2)

    locator = vtk.vtkCellLocator()
    locator.SetDataSet(polydata)
    locator.BuildLocator()

    t       = vtk.reference(0.0)
    x       = [0.0, 0.0, 0.0]
    pcoords = [0.0, 0.0, 0.0]
    sub_id  = vtk.reference(0)
    cell_id = vtk.reference(-1)

    # Small offset so the ray starts just above the source cell surface
    offset = diag * 0.01
    ray_start = (focal[0] + nx * offset,
                 focal[1] + ny * offset,
                 focal[2] + nz * offset)

    hit = locator.IntersectWithLine(
        list(ray_start), list(ray_end),
        1e-6,
        t, x, pcoords, sub_id, cell_id,
    )

    if hit:
        # Distance from focal to the blocking wall.
        # Place camera right at the wall (clamped to at least offset from focal)
        # so it is as far from the hot cell as possible.
        hit_dist = ((x[0] - focal[0])**2 +
                    (x[1] - focal[1])**2 +
                    (x[2] - focal[2])**2) ** 0.5
        cam_dist = max(hit_dist, offset * 2)
        return (focal[0] + nx * cam_dist,
                focal[1] + ny * cam_dist,
                focal[2] + nz * cam_dist)

    return (focal[0] + nx * default_dist,
            focal[1] + ny * default_dist,
            focal[2] + nz * default_dist)
