"""
transform_vtp.py
----------------
Apply a Z-rotation + XYZ translation to all point coordinates in a
vtkPolyData (.vtp) file and write the result to a new file.

The transform mirrors the logic in transform_reference_frame.py:
  1) Rotate around Z by angle_deg
  2) Translate by dx, dy, dz

All dx/dy/dz values must be in the same unit as the file coordinates.
Use the unit_scale helper to convert mm values to m when needed.
"""

from __future__ import annotations

import math
from pathlib import Path

import vtk

# Conversion: 1 mm = MM_TO_M metres
MM_TO_M = 1e-3


def mm_to_m(v: float) -> float:
    return v * MM_TO_M


def transform_vtp_file(
    input_path: Path,
    output_path: Path,
    angle_deg: float,
    dx: float,
    dy: float,
    dz: float,
) -> None:
    """
    Read *input_path* (.vtp), rotate all points around Z by *angle_deg*
    degrees, translate by (dx, dy, dz), and write to *output_path*.

    All spatial parameters must share the same unit as the file coordinates.
    Cell data / point data arrays are carried over unchanged.
    """
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(input_path))
    reader.Update()
    src = reader.GetOutput()

    theta = math.radians(angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    src_pts = src.GetPoints()
    n = src_pts.GetNumberOfPoints()

    new_pts = vtk.vtkPoints()
    new_pts.SetNumberOfPoints(n)
    for i in range(n):
        x, y, z = src_pts.GetPoint(i)
        x_r = cos_t * x - sin_t * y
        y_r = sin_t * x + cos_t * y
        new_pts.SetPoint(i, x_r + dx, y_r + dy, z + dz)

    out = vtk.vtkPolyData()
    out.DeepCopy(src)
    out.SetPoints(new_pts)

    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(output_path))
    writer.SetInputData(out)
    writer.Write()
