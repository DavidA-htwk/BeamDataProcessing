"""
snapshot_max.py
---------------
Offscreen VTK snapshot of the cell with the highest scalar value in a
vtkPolyData.

Camera strategy
~~~~~~~~~~~~~~~
1. Find the cell with the maximum value of *array_name*.
2. Compute that cell's centroid and surface normal (via vtkPolyDataNormals).
3. Place the camera *behind* the normal (i.e. looking along -normal toward the
   centroid) at a distance proportional to the bounding-box diagonal.
4. Set the view-up vector to whichever world axis is least parallel to the
   normal (to avoid gimbal lock).

The polydata is coloured by *array_name* using a blue→red (cool-to-warm)
colour map.  The colour range is fixed to [0, global_max] when *vmax* is
supplied, otherwise per-file.
"""

from __future__ import annotations

from pathlib import Path

import vtk


# ── Public entry point ────────────────────────────────────────────────────────

def save_max_snapshot(
    polydata: vtk.vtkPolyData,
    array_name: str,
    out_path: Path,
    vmax: float | None = None,
    image_size: tuple[int, int] = (1200, 900),
) -> bool:
    """
    Render *polydata* coloured by *array_name* and save a PNG to *out_path*.

    Parameters
    ----------
    polydata   : source geometry (cell data must contain *array_name*)
    array_name : scalar array used for colouring and max detection
    out_path   : full path for the output PNG (parent dir must exist)
    vmax       : upper bound of colour range; if None uses the file maximum
    image_size : (width, height) in pixels

    Returns True on success, False if the array was not found.
    """
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return False

    n = arr.GetNumberOfTuples()
    if n == 0:
        return False

    # ── Find max cell ─────────────────────────────────────────────────────────
    max_val = arr.GetValue(0)
    max_cid = 0
    for i in range(1, n):
        v = arr.GetValue(i)
        if v > max_val:
            max_val = v
            max_cid = i

    cell_centroid = _cell_centroid(polydata, max_cid)
    cell_normal   = _cell_normal(polydata, max_cid)

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
    scalar_bar = vtk.vtkScalarBarActor()
    scalar_bar.SetLookupTable(lut)
    scalar_bar.SetTitle(array_name)
    scalar_bar.SetNumberOfLabels(5)
    scalar_bar.SetWidth(0.10)
    scalar_bar.SetHeight(0.6)
    scalar_bar.SetPosition(0.84, 0.2)

    # Black text for title and labels (white background)
    title_prop = scalar_bar.GetTitleTextProperty()
    title_prop.SetColor(0.0, 0.0, 0.0)
    title_prop.SetFontSize(20)
    title_prop.SetFontFamilyToTimes()
    title_prop.ItalicOn()
    title_prop.BoldOff()

    label_prop = scalar_bar.GetLabelTextProperty()
    label_prop.SetColor(0.0, 0.0, 0.0)
    label_prop.SetFontSize(12)
    label_prop.SetFontFamilyToTimes()
    label_prop.ItalicOff()
    label_prop.BoldOff()

    # Honour the font sizes above instead of auto-scaling them to fit
    scalar_bar.UnconstrainedFontSizeOn()

    # Push title up so it sits above the bar instead of overlapping it
    scalar_bar.SetVerticalTitleSeparation(8)

    # ── Top-right legend: violet dot + max value ───────────────────────────────
    legend = vtk.vtkLegendBoxActor()
    legend.SetNumberOfEntries(1)
    legend_sphere = vtk.vtkSphereSource()
    legend_sphere.SetPhiResolution(12)
    legend_sphere.SetThetaResolution(12)
    legend_sphere.Update()
    legend.SetEntry(0, legend_sphere.GetOutput(), f"Max: {max_val:.6g}", [0.498, 0.0, 1.0])
    legend.GetEntryTextProperty().SetColor(0.0, 0.0, 0.0)
    legend.GetEntryTextProperty().SetFontSize(13)
    legend.GetEntryTextProperty().SetFontFamilyToTimes()
    legend.GetEntryTextProperty().ItalicOff()
    legend.GetEntryTextProperty().BoldOff()
    legend.SetPosition(0.72, 0.88)
    legend.SetPosition2(0.27, 0.09)
    legend.BorderOff()
    legend.SetBackgroundColor(1.0, 1.0, 1.0)
    legend.SetBackgroundOpacity(1.0)

    # ── Max-point marker (violet sphere) ─────────────────────────────────────────────────
    sphere_src = vtk.vtkSphereSource()
    sphere_src.SetCenter(*cell_centroid)
    diag = _bbox_diagonal(polydata)
    sphere_src.SetRadius(diag * 0.008)
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
    renderer.AddActor2D(legend)

    # ── Camera placement ──────────────────────────────────────────────────────
    _place_camera(renderer, cell_centroid, cell_normal, diag)

    # ── Render window (offscreen) ─────────────────────────────────────────────
    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
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
    Return the surface normal of *cid* via vtkPolyDataNormals.
    Falls back to (0, 0, 1) if cell normals cannot be computed.
    """
    normals_filter = vtk.vtkPolyDataNormals()
    normals_filter.SetInputData(polydata)
    normals_filter.ComputeCellNormalsOn()
    normals_filter.ComputePointNormalsOff()
    normals_filter.SplittingOff()
    normals_filter.Update()

    cell_normals = normals_filter.GetOutput().GetCellData().GetNormals()
    if cell_normals is None or cid >= cell_normals.GetNumberOfTuples():
        return (0.0, 0.0, 1.0)

    nx, ny, nz = cell_normals.GetTuple3(cid)
    length = (nx**2 + ny**2 + nz**2) ** 0.5
    if length < 1e-10:
        return (0.0, 0.0, 1.0)
    return nx / length, ny / length, nz / length


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
) -> None:
    """
    Position camera along *normal* from *focal*, at distance ~1.5 * diag.
    Choose view-up as the world axis least parallel to *normal*.
    """
    dist = diag * 1.5
    nx, ny, nz = normal
    cam_pos = (focal[0] + nx * dist,
               focal[1] + ny * dist,
               focal[2] + nz * dist)

    # Pick view-up: world axis most orthogonal to the normal
    axes = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    dot_abs = [abs(nx * ax[0] + ny * ax[1] + nz * ax[2]) for ax in axes]
    view_up = axes[dot_abs.index(min(dot_abs))]

    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(*focal)
    camera.SetPosition(*cam_pos)
    camera.SetViewUp(*view_up)
    renderer.ResetCameraClippingRange()
