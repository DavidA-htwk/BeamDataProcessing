"""
modules/vtk_io.py
-----------------
Low-level VTK file I/O and polydata helpers.
"""

from __future__ import annotations

from pathlib import Path

import vtk
import numpy as np
from vtk.util.numpy_support import vtk_to_numpy, numpy_to_vtk

from modules.core.settings import ARRAY_NAME, POWER_ARRAY


# ── VTP read / write ──────────────────────────────────────────────────────────

def read_vtp(filepath) -> vtk.vtkPolyData:
    """Read a VTP file and return the vtkPolyData."""
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(filepath))
    reader.Update()
    return reader.GetOutput()


def _write_vtp(polydata: vtk.vtkPolyData, path: Path) -> None:
    """Serialize *polydata* to a VTP file (used for inter-process transfer).

    Uses raw-binary appended mode (no compression, no base64 encoding) which
    is 5-10x faster than the default ASCII/compressed mode for large meshes.
    """
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(polydata)
    writer.SetDataModeToAppended()    # raw binary — fastest write/read
    writer.EncodeAppendedDataOff()    # skip base64, write raw bytes
    writer.SetCompressorTypeToNone()  # no zlib overhead
    writer.Write()


# ── Scalar queries ────────────────────────────────────────────────────────────

def find_total(polydata: vtk.vtkPolyData, array_name: str) -> float | None:
    """Return the sum of *array_name* in cell data, or None if absent."""
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return None
    n = arr.GetNumberOfTuples()
    if n == 0:
        return None
    return float(vtk_to_numpy(arr).sum())


def find_max(polydata: vtk.vtkPolyData, array_name: str) -> float | None:
    """Return the maximum value of *array_name* in cell data, or None if absent."""
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None:
        return None
    n = arr.GetNumberOfTuples()
    if n == 0:
        return None
    return float(vtk_to_numpy(arr).max())


# ── Array scaling ─────────────────────────────────────────────────────────────

def _scale_polydata_array(
        polydata: vtk.vtkPolyData,
        array_name: str,
        factor: float,
) -> vtk.vtkPolyData:
    """Return a copy of *polydata* with *array_name* in cell data multiplied by *factor*.

    Scaling the data (rather than clipping the colour range via vmax) means the
    colour map always spans [0, scaled_max] without saturation.  When factor==1.0
    the original object is returned unchanged (no copy).
    """
    arr = polydata.GetCellData().GetArray(array_name)
    if arr is None or factor == 1.0:
        return polydata
    scaled_vals = vtk_to_numpy(arr).astype(float) * factor
    new_arr = numpy_to_vtk(scaled_vals, deep=True, array_type=arr.GetDataType())
    new_arr.SetName(array_name)
    pd2 = vtk.vtkPolyData()
    pd2.DeepCopy(polydata)
    pd2.GetCellData().RemoveArray(array_name)
    pd2.GetCellData().AddArray(new_arr)
    pd2.GetCellData().SetActiveScalars(array_name)
    return pd2
