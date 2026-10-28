"""
transform_vtp.py
----------------
Apply a Z-rotation + XYZ translation to a vtkPolyData (.vtp / .vtm) file and
write the result as a new .vtp.

The transform mirrors the logic in transform_reference_frame.py so that the
VTP and CSV outputs of Tab 2 describe exactly the same points:

  1) Rotate around Z by angle_deg
  2) Translate by dx, dy, dz
  3) Multiply the result by coord_scale (input-unit -> output-unit conversion)

The same export options the CSV path honours are applied to the cell data:

  * export_area   - attach a computed ``Area`` cell array (in file units)
  * export_power  - keep ``Deposited_Power_W``
  * export_pload  - keep ``Power_Density_W_m2`` (derived from power/area when
                    the array is absent, exactly as the CSV exporter does)
  * mult          - factor applied to the power arrays, including the
                    per-species ``..._H+`` / ``..._H0`` / ``..._H-`` variants
                    that only exist in the mesh; StdErr arrays scale with
                    ``mult``, SumSq with ``mult**2``
  * ignore_zeros  - drop cells whose selected power values are all ~0

The total deposited power (summed over *all* cells, before any zero filtering,
with ``mult`` applied) is stored in field data as ``total_deposited_power_W``,
matching the CSV column of the same name.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

POWER_ARRAY       = "Deposited_Power_W"
DENSITY_ARRAY     = "Power_Density_W_m2"
AREA_ARRAY        = "Area"
STDERR_ARRAY         = "Deposited_Power_StdErr_W"
DENSITY_STDERR_ARRAY = "Power_Density_StdErr_W_m2"
SUMSQ_ARRAY          = "Deposited_Power_SumSq_W2"
TOTAL_POWER_FIELD = "total_deposited_power_W"

# Scratch cell array used to drive the zero-cell threshold; never written out.
_MASK_ARRAY = "_keep_mask"

# Same threshold the CSV exporter uses to decide a row is "zero".
ZERO_TOL = 1e-12

# Conversion: 1 mm = MM_TO_M metres
MM_TO_M = 1e-3


def mm_to_m(v: float) -> float:
    return v * MM_TO_M


def _read_mesh(input_path: Path) -> vtk.vtkPolyData:
    """Read a .vtp or .vtm and return a single vtkPolyData.

    MultiBlock datasets are flattened by appending every vtkPolyData leaf,
    the same way extract_cells_to_csv() handles them.
    """
    ext = os.path.splitext(str(input_path))[1].lower()
    if ext == ".vtm":
        reader = vtk.vtkXMLMultiBlockDataReader()
    else:
        reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(input_path))
    reader.Update()
    raw = reader.GetOutput()

    if isinstance(raw, vtk.vtkMultiBlockDataSet):
        appender = vtk.vtkAppendPolyData()
        it = raw.NewIterator()
        it.InitTraversal()
        while not it.IsDoneWithTraversal():
            block = it.GetCurrentDataObject()
            if isinstance(block, vtk.vtkPolyData):
                appender.AddInputData(block)
            it.GoToNextItem()
        appender.Update()
        return appender.GetOutput()
    return raw


def _cell_areas(mesh: vtk.vtkPolyData) -> np.ndarray | None:
    """Return per-cell areas as a numpy array, or None if unavailable."""
    csf = vtk.vtkCellSizeFilter()
    csf.SetInputData(mesh)
    csf.ComputeAreaOn()
    csf.ComputeLengthOff()
    csf.ComputeVolumeOff()
    csf.ComputeVertexCountOff()
    csf.Update()
    arr = csf.GetOutput().GetCellData().GetArray(AREA_ARRAY)
    return vtk_to_numpy(arr).astype(float) if arr is not None else None


def _companion_power_arrays(cd: vtk.vtkCellData) -> list[str]:
    """Names of the per-species power arrays, e.g. Deposited_Power_W_H+.

    These sit alongside the two main arrays and hold the same quantity broken
    down by beam species, so they follow the same scaling.
    """
    out: list[str] = []
    for i in range(cd.GetNumberOfArrays()):
        name = cd.GetArrayName(i)
        if not name or name in (POWER_ARRAY, DENSITY_ARRAY):
            continue
        if name.startswith((POWER_ARRAY, DENSITY_ARRAY)):
            out.append(name)
    return out


def _error_arrays(cd: vtk.vtkCellData, mult: float) -> list[tuple[str, float]]:
    """(name, factor) for the BeamOnTarget error arrays, species variants included."""
    out: list[tuple[str, float]] = []
    for i in range(cd.GetNumberOfArrays()):
        name = cd.GetArrayName(i)
        if not name:
            continue
        if name.startswith((STDERR_ARRAY, DENSITY_STDERR_ARRAY)):
            out.append((name, mult))
        elif name.startswith(SUMSQ_ARRAY):
            out.append((name, mult * mult))
    return out


def _set_cell_array(mesh: vtk.vtkPolyData, name: str, values: np.ndarray) -> None:
    """Replace (or add) the cell array called *name*.

    The storage type of an array that already exists is preserved, matching
    _scale_polydata_array() in modules/vtk/vtk_io.py.  Writing everything back
    as float64 would silently promote the float32 power arrays and inflate
    these meshes by roughly a quarter for no accuracy the source ever had -
    the values arrive as float32, and the CSV path rounds to %.6e anyway.
    """
    cd = mesh.GetCellData()
    existing = cd.GetArray(name)
    vtk_type = existing.GetDataType() if existing is not None else vtk.VTK_DOUBLE
    arr = numpy_to_vtk(np.ascontiguousarray(values, dtype=np.float64),
                       deep=True, array_type=vtk_type)
    arr.SetName(name)
    cd.RemoveArray(name)
    cd.AddArray(arr)


def _extract_cells(mesh: vtk.vtkPolyData, keep: np.ndarray) -> vtk.vtkPolyData:
    """Return a polydata holding only the cells flagged True in *keep*.

    Implemented as a threshold on a temporary mask array: vtkExtractCells
    only takes ids as a vtkIdList or a Python tuple, either of which means a
    per-cell Python loop over meshes with millions of cells.  vtkThreshold
    does the same selection entirely in C++ and keeps the cells in source
    order, so the survivors line up 1:1 with the rows the CSV exporter keeps.
    """
    mask = numpy_to_vtk(
        np.ascontiguousarray(keep, dtype=np.uint8), deep=True,
        array_type=vtk.VTK_UNSIGNED_CHAR,
    )
    mask.SetName(_MASK_ARRAY)
    # ShallowCopy would share the cell-data object with *mesh*, so the scratch
    # array would leak back into the caller's polydata.
    work = vtk.vtkPolyData()
    work.SetPoints(mesh.GetPoints())
    work.SetVerts(mesh.GetVerts())
    work.SetLines(mesh.GetLines())
    work.SetPolys(mesh.GetPolys())
    work.SetStrips(mesh.GetStrips())
    work.GetPointData().PassData(mesh.GetPointData())
    work.GetCellData().PassData(mesh.GetCellData())
    work.GetCellData().AddArray(mask)

    thr = vtk.vtkThreshold()
    thr.SetInputData(work)
    thr.SetInputArrayToProcess(
        0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS, _MASK_ARRAY)
    # THRESHOLD_UPPER keeps values above the *upper* threshold.
    thr.SetUpperThreshold(0.5)
    thr.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_UPPER)
    thr.Update()

    # vtkThreshold yields an unstructured grid - convert back to polydata.
    geom = vtk.vtkGeometryFilter()
    geom.SetInputData(thr.GetOutput())
    geom.Update()

    out = geom.GetOutput()
    out.GetCellData().RemoveArray(_MASK_ARRAY)
    return out


def transform_vtp_file(
    input_path: Path,
    output_path: Path,
    angle_deg: float,
    dx: float,
    dy: float,
    dz: float,
    coord_scale: float = 1.0,
    export_area: bool = True,
    export_power: bool = True,
    export_pload: bool = True,
    mult: float = 1.0,
    ignore_zeros: bool = False,
) -> None:
    """Transform *input_path* into *output_path* (.vtp).

    Points are rotated around Z by *angle_deg*, translated by (dx, dy, dz),
    then multiplied by *coord_scale*.  dx/dy/dz must share the unit of the
    file coordinates; the scale is applied afterwards, exactly as the CSV
    path does, so both outputs stay in agreement.

    Cell data is filtered and scaled according to the export flags.  Arrays
    other than the power/area ones are carried over unchanged.
    """
    mesh = _read_mesh(Path(input_path))

    n_cells = mesh.GetNumberOfCells()
    if n_cells == 0:
        raise ValueError(f"No cells found in '{input_path}'")

    out = vtk.vtkPolyData()
    out.DeepCopy(mesh)
    cd = out.GetCellData()

    dep_arr  = cd.GetArray(POWER_ARRAY)
    dens_arr = cd.GetArray(DENSITY_ARRAY)

    dep  = vtk_to_numpy(dep_arr).astype(float)  if dep_arr  is not None else None
    dens = vtk_to_numpy(dens_arr).astype(float) if dens_arr is not None else None

    # Areas are needed for the Area array and as the fallback for a missing
    # density array.  Computed on the *untransformed* mesh, so the values stay
    # in input units - the CSV exporter does the same.
    areas = None
    if export_area or (export_pload and dens is None):
        areas = _cell_areas(out)

    # Derive density from power/area when the array is absent (CSV parity).
    if dens is None and dep is not None and areas is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            dens = np.where(areas > 0.0, dep / areas, 0.0)

    # Total power over every cell, before any zero filtering - matches the
    # CSV's total_deposited_power_W column.
    total_power = float(dep.sum() * mult) if dep is not None else 0.0

    dep_scaled  = dep * mult  if dep  is not None else None
    dens_scaled = dens * mult if dens is not None else None

    # -- Apply the export selection to the cell arrays -------------------------
    if export_power and dep_scaled is not None:
        _set_cell_array(out, POWER_ARRAY, dep_scaled)
    else:
        cd.RemoveArray(POWER_ARRAY)

    if export_pload and dens_scaled is not None:
        _set_cell_array(out, DENSITY_ARRAY, dens_scaled)
    else:
        cd.RemoveArray(DENSITY_ARRAY)

    if export_area and areas is not None:
        _set_cell_array(out, AREA_ARRAY, areas)
    else:
        cd.RemoveArray(AREA_ARRAY)

    # Per-species companions (Deposited_Power_W_H-, Power_Density_W_m2_H0, ...)
    # are the same physical quantity split by beam species.  Scale them by the
    # same factor, otherwise a file would mix scaled and raw power arrays and
    # the species arrays would no longer sum to the total.
    # The statistical error arrays must follow, or the error bars no longer
    # belong to the scaled power: StdErr scales with mult, the sum of squares
    # with mult**2.  Relative_Error is a ratio and stays as it is.
    if mult != 1.0:
        for name in _companion_power_arrays(cd):
            arr = cd.GetArray(name)
            _set_cell_array(out, name, vtk_to_numpy(arr).astype(float) * mult)
        for name, fac in _error_arrays(cd, mult):
            arr = cd.GetArray(name)
            _set_cell_array(out, name, vtk_to_numpy(arr).astype(float) * fac)

    if export_pload and dens_scaled is not None:
        cd.SetActiveScalars(DENSITY_ARRAY)
    elif export_power and dep_scaled is not None:
        cd.SetActiveScalars(POWER_ARRAY)

    # -- Drop zero-power cells -------------------------------------------------
    if ignore_zeros:
        power_vals = []
        if export_power and dep_scaled is not None:
            power_vals.append(dep_scaled)
        if export_pload and dens_scaled is not None:
            power_vals.append(dens_scaled)
        if power_vals:
            zero = np.ones(n_cells, dtype=bool)
            for vals in power_vals:
                zero &= np.abs(vals) <= ZERO_TOL
            if zero.any():
                field_data = out.GetFieldData()
                out = _extract_cells(out, ~zero)
                # vtkThreshold/vtkGeometryFilter drop the source field data
                # (source_filename and friends) - carry the provenance over.
                out.GetFieldData().PassData(field_data)

    # -- Transform the point coordinates ---------------------------------------
    pts = out.GetPoints()
    if pts is not None and pts.GetNumberOfPoints() > 0:
        theta = math.radians(angle_deg)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)

        xyz = vtk_to_numpy(pts.GetData()).astype(float)
        x, y, z = xyz[:, 0].copy(), xyz[:, 1].copy(), xyz[:, 2]
        xyz[:, 0] = (cos_t * x - sin_t * y + dx) * coord_scale
        xyz[:, 1] = (sin_t * x + cos_t * y + dy) * coord_scale
        xyz[:, 2] = (z + dz) * coord_scale

        new_pts = vtk.vtkPoints()
        new_pts.SetData(numpy_to_vtk(np.ascontiguousarray(xyz), deep=True))
        out.SetPoints(new_pts)

    # Record the total deposited power alongside the mesh.
    total_arr = vtk.vtkDoubleArray()
    total_arr.SetName(TOTAL_POWER_FIELD)
    total_arr.SetNumberOfTuples(1)
    total_arr.SetValue(0, total_power)
    out.GetFieldData().AddArray(total_arr)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(output_path))
    writer.SetInputData(out)
    writer.SetDataModeToAppended()    # raw binary - fastest write/read
    writer.EncodeAppendedDataOff()
    writer.SetCompressorTypeToNone()
    writer.Write()
