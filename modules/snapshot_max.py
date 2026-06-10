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

import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy


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

    # ── Scalar bar ────────────────────────────────────────────────────────────
    # Height 0.44 + bottom at 0.25 = top at 0.69.
    # The top tick label renders ~0.04 above the bar edge, so the background
    # panel (which ends at 0.69) needs a gap -- we leave room by starting at
    # 0.22 and keeping height 0.44 so the panel top is at 0.66 and the label
    # at 0.69 still sits inside the panel's visual extent.
    # Simplest reliable fix: use SetTextPad to add internal padding.
    scalar_bar = vtk.vtkScalarBarActor()
    scalar_bar.SetLookupTable(lut)
    scalar_bar.SetTitle("")              # title drawn as a separate rotated actor below
    scalar_bar.SetNumberOfLabels(5)
    scalar_bar.SetWidth(0.09)
    scalar_bar.SetHeight(0.50)
    scalar_bar.SetPosition(0.82, 0.22)
    try:
        scalar_bar.SetTextPad(4)         # pixels of padding inside background (VTK >= 9.1)
    except AttributeError:
        pass

    label_prop = scalar_bar.GetLabelTextProperty()
    label_prop.SetColor(0.0, 0.0, 0.0)
    label_prop.SetFontSize(12)
    label_prop.SetFontFamilyToTimes()
    label_prop.ItalicOff()
    label_prop.BoldOff()
    scalar_bar.UnconstrainedFontSizeOn()

    # Frosted white panel behind bar + labels
    scalar_bar.DrawBackgroundOn()
    scalar_bar.DrawFrameOn()
    sb_bg = scalar_bar.GetBackgroundProperty()
    sb_bg.SetColor(1.0, 1.0, 1.0)
    sb_bg.SetOpacity(0.80)
    sb_frame = scalar_bar.GetFrameProperty()
    sb_frame.SetColor(0.6, 0.6, 0.6)
    sb_frame.SetOpacity(1.0)

    # ── Scalar bar title: 90° rotated, anchored at bar bottom ─────────────────
    # vtkTextActor with SetBackgroundColor gives a reliable frosted rectangle.
    # Position x is just right of the bar+label column; y matches bar bottom.
    sbar_title = vtk.vtkTextActor()
    sbar_title.SetInput(array_name)
    sbt = sbar_title.GetTextProperty()
    sbt.SetColor(0.0, 0.0, 0.0)
    sbt.SetFontSize(18)
    sbt.SetFontFamilyToTimes()
    sbt.ItalicOn()
    sbt.BoldOff()
    sbt.SetOrientation(90)
    sbt.SetBackgroundColor(1.0, 1.0, 1.0)
    sbt.SetBackgroundOpacity(0.80)
    sbar_title.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    sbar_title.SetPosition(0.96, 0.22)   # matches bar bottom

    # ── Max value: plain vtkTextActor (reliable background) ───────────────────
    # vtkLegendBoxActor background is unreliable; vtkTextActor.GetTextProperty
    # SetBackgroundColor/Opacity is composited correctly in offscreen renders.
    max_label = vtk.vtkTextActor()
    max_label.SetInput(f"[*] Max: {max_val:.6g}")  # [*] = ASCII stand-in for the max marker
    ml = max_label.GetTextProperty()
    ml.SetColor(0.498, 0.0, 1.0)          # violet to match sphere
    ml.SetFontSize(18)
    ml.SetFontFamilyToTimes()
    ml.BoldOn()
    ml.ItalicOff()
    ml.SetBackgroundColor(1.0, 1.0, 1.0)
    ml.SetBackgroundOpacity(0.85)
    max_label.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    max_label.SetPosition(0.02, 0.94)

    # ── Max-point marker (violet sphere) ─────────────────────────────────────────────────
    sphere_src = vtk.vtkSphereSource()
    sphere_src.SetCenter(*cell_centroid)
    sphere_src.SetPhiResolution(12)
    sphere_src.SetThetaResolution(12)
    sphere_mapper = vtk.vtkPolyDataMapper()
    sphere_mapper.SetInputConnection(sphere_src.GetOutputPort())
    sphere_actor = vtk.vtkActor()
    sphere_actor.SetMapper(sphere_mapper)
    sphere_actor.GetProperty().SetColor(0.498, 0.0, 1.0)   # #7F00FF violet

    # ── Renderer ──────────────────────────────────────────────────────────────
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1, 1, 1)
    renderer.AddActor(actor)
    renderer.AddActor(sphere_actor)
    renderer.AddActor2D(scalar_bar)
    renderer.AddActor2D(sbar_title)
    renderer.AddActor2D(max_label)

    # ── Axes grid (vtkCubeAxesActor — equivalent of ParaView AxesGrid) ────────
    bounds = polydata.GetBounds()   # (xmin, xmax, ymin, ymax, zmin, zmax)
    cube_axes = vtk.vtkCubeAxesActor()
    cube_axes.SetBounds(bounds)
    cube_axes.SetFlyModeToStaticEdges()   # axis labels stay on outer edges
    cube_axes.DrawXGridlinesOn()
    cube_axes.DrawYGridlinesOn()
    cube_axes.DrawZGridlinesOn()
    # Black grid lines & axes
    _black = (0.0, 0.0, 0.0)
    for prop in (cube_axes.GetXAxesGridlinesProperty(),
                 cube_axes.GetYAxesGridlinesProperty(),
                 cube_axes.GetZAxesGridlinesProperty()):
        prop.SetColor(*_black)
    # Label and title text properties — VTK 9.x Python API uses GetLabelTextProperty(i)
    # and GetTitleTextProperty(i) where i=0/1/2 for X/Y/Z.  The old per-axis named
    # methods (GetXAxisLabelTextProperty etc.) do not exist in vtkmodules.
    for axis_idx in range(3):
        for tp in (cube_axes.GetLabelTextProperty(axis_idx),
                   cube_axes.GetTitleTextProperty(axis_idx)):
            tp.SetColor(*_black)
            tp.SetFontSize(10)
            tp.BoldOff()
            tp.ItalicOff()
    cube_axes.GetXAxesLinesProperty().SetColor(*_black)
    cube_axes.GetYAxesLinesProperty().SetColor(*_black)
    cube_axes.GetZAxesLinesProperty().SetColor(*_black)
    cube_axes.XAxisMinorTickVisibilityOff()
    cube_axes.YAxisMinorTickVisibilityOff()
    cube_axes.ZAxisMinorTickVisibilityOff()
    renderer.AddActor(cube_axes)

    # ── Camera placement ──────────────────────────────────────────────────────
    if diag is None:
        diag = _bbox_diagonal(polydata)
    cam_pos = _place_camera(
        renderer, cell_centroid, cell_normal, diag,
        polydata=None if _cam_pos_pre is not None else polydata,
        cam_pos_override=_cam_pos_pre,
    )

    # Wire cube_axes to the now-configured camera
    cube_axes.SetCamera(renderer.GetActiveCamera())

    # Scale sphere radius proportional to camera distance (0.48 % of distance)
    cam_dist = ((cam_pos[0] - cell_centroid[0])**2 +
                (cam_pos[1] - cell_centroid[1])**2 +
                (cam_pos[2] - cell_centroid[2])**2) ** 0.5 or diag
    sphere_src.SetRadius(cam_dist * 0.0048)

    # ── Render window (offscreen) -- MSAA disabled for speed ───────────────────
    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetMultiSamples(0)   # disable MSAA -- cuts render time ~40%
    render_window.SetSize(*image_size)
    render_window.AddRenderer(renderer)
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
