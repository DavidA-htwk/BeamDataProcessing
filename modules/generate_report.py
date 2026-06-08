# generate_report.py
"""
Standalone post-processing script to generate summary CSV reports from
simulation result files (.vtp/.vtm), without re-running the simulation
or smoothing.

For each directory processed, a CSV file is written with one row per
result file containing:
  - filename
  - total_deposited_power_W
  - peak_power_density_W_m2

The script can work on:
  - Raw (unsmoothed) results in an OUTPUT subfolder
  - Smoothed results in a SMOOTHED subfolder
  - A parent OUTPUT directory (processes all subfolders)

Usage examples
--------------
  # Generate reports for all subfolders in the default OUTPUT directory:
  python generate_report.py

  # Generate report for a specific directory:
  python generate_report.py -i OUTPUT/DNB_10mrad

  # Generate report for smoothed results:
  python generate_report.py -i OUTPUT/DNB_10mrad/SMOOTHED

  # Custom output filename:
  python generate_report.py -i OUTPUT/DNB_10mrad -o my_summary.csv
"""

import pyvista as pv
import numpy as np
import os
import csv
import glob
import argparse

try:
    import config
    DEFAULT_OUTPUT_DIR = config.DETAILED_OUTPUT_DIR
except (ImportError, AttributeError):
    DEFAULT_OUTPUT_DIR = "OUTPUT"

DEFAULT_REPORT_FILENAME = "summary_report.csv"


def extract_cells_to_csv(input_path, output_path):
    """
    Extract per-cell data from a single .vtp/.vtm file and write to a CSV.
    Uses vtk directly (no pyvista dependency).

    Output columns: X, Y, Z, Area, Deposited_Power_W, Power_Density_W_m2
    where X/Y/Z are cell-centre coordinates.

    Parameters
    ----------
    input_path : str
        Path to the .vtp or .vtm file to read.
    output_path : str
        Path for the output CSV file.

    Returns
    -------
    output_path : str
        The path written, same as the argument.
    """
    import vtk

    ext = os.path.splitext(str(input_path))[1].lower()
    if ext == ".vtm":
        reader = vtk.vtkXMLMultiBlockDataReader()
    else:
        reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(input_path))
    reader.Update()
    raw = reader.GetOutput()

    # Flatten MultiBlock into a single PolyData
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
        mesh = appender.GetOutput()
    else:
        mesh = raw

    n = mesh.GetNumberOfCells()
    if n == 0:
        raise ValueError(f"No cells found in '{input_path}'")

    # Cell centres
    cc_filter = vtk.vtkCellCenters()
    cc_filter.SetInputData(mesh)
    cc_filter.Update()
    cc_pts = cc_filter.GetOutput().GetPoints()

    # Cell areas
    csf = vtk.vtkCellSizeFilter()
    csf.SetInputData(mesh)
    csf.ComputeAreaOn()
    csf.ComputeLengthOff()
    csf.ComputeVolumeOff()
    csf.ComputeVertexCountOff()
    csf.Update()
    area_arr = csf.GetOutput().GetCellData().GetArray("Area")

    cd = mesh.GetCellData()
    dep_arr  = cd.GetArray("Deposited_Power_W")
    dens_arr = cd.GetArray("Power_Density_W_m2")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["X", "Y", "Z", "Area", "Deposited_Power_W", "Power_Density_W_m2"])
        for i in range(n):
            x, y, z = cc_pts.GetPoint(i)
            area = float(area_arr.GetValue(i)) if area_arr else 0.0
            dep  = float(dep_arr.GetValue(i))  if dep_arr  else 0.0
            if dens_arr:
                dens = float(dens_arr.GetValue(i))
            elif area > 0.0:
                dens = dep / area
            else:
                dens = 0.0
            writer.writerow([
                f"{x:.12g}",
                f"{y:.12g}",
                f"{z:.12g}",
                f"{area:.6e}",
                f"{dep:.6e}",
                f"{dens:.6e}",
            ])

    return output_path


def generate_summary_csv(input_dir, output_filename=None):
    """
    Reads all .vtp/.vtm files in *input_dir*, extracts total deposited power
    and peak power density from each, and writes a summary CSV.

    Parameters
    ----------
    input_dir : str
        Directory containing .vtp/.vtm result files.
    output_filename : str, optional
        Name of the CSV file to write.  Defaults to 'summary_report.csv'.
        The file is saved inside *input_dir*.

    Returns
    -------
    summary_path : str or None
        Path to the written CSV, or None if no files were found.
    """
    if output_filename is None:
        output_filename = DEFAULT_REPORT_FILENAME

    if not os.path.isdir(input_dir):
        print(f"  ERROR: Directory '{input_dir}' not found.")
        return None

    search_vtp = os.path.join(input_dir, "*.vtp")
    search_vtm = os.path.join(input_dir, "*.vtm")
    abs_input = os.path.abspath(input_dir)
    files = sorted([
        f for f in glob.glob(search_vtp) + glob.glob(search_vtm)
        if "SMOOTHED" not in os.path.basename(os.path.dirname(os.path.abspath(f)))
        or os.path.abspath(os.path.dirname(f)) == abs_input
    ])

    if not files:
        print(f"  No .vtp/.vtm files found in '{input_dir}'.")
        return None

    print(f"  Scanning {len(files)} file(s) in '{input_dir}' ...")
    rows = []

    for filepath in files:
        filename = os.path.basename(filepath)
        try:
            dataset = pv.read(filepath)

            # Handle MultiBlock datasets
            if isinstance(dataset, pv.MultiBlock):
                total_power = 0.0
                peak_density = 0.0
                for i in range(dataset.n_blocks):
                    block = dataset[i]
                    tp, pd = _extract_stats(block)
                    total_power += tp
                    peak_density = max(peak_density, pd)
            else:
                total_power, peak_density = _extract_stats(dataset)

            rows.append({
                "filename": filename,
                "total_deposited_power_W": total_power,
                "peak_power_density_W_m2": peak_density,
            })

        except Exception as e:
            print(f"    WARNING: Could not read '{filename}': {e}")
            rows.append({
                "filename": filename,
                "total_deposited_power_W": "N/A",
                "peak_power_density_W_m2": "N/A",
            })

    summary_path = os.path.join(input_dir, output_filename)
    with open(summary_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["filename", "total_deposited_power_W", "peak_power_density_W_m2"])
        for row in rows:
            tp = row["total_deposited_power_W"]
            pd = row["peak_power_density_W_m2"]
            writer.writerow([
                row["filename"],
                f"{tp:.4e}" if isinstance(tp, float) else tp,
                f"{pd:.4e}" if isinstance(pd, float) else pd,
            ])

    print(f"  Summary CSV written to: {summary_path}")
    return summary_path


def _extract_stats(mesh):
    """
    Extract total deposited power and peak power density from a single mesh.

    Tries 'Power_Density_W_m2' first (available after smoothing or simulation).
    Falls back to computing density from 'Deposited_Power_W' and cell areas.

    Returns
    -------
    total_power : float
    peak_density : float
    """
    total_power = 0.0
    peak_density = 0.0

    if "Deposited_Power_W" in mesh.cell_data:
        deposited = np.array(mesh.cell_data["Deposited_Power_W"], dtype=np.float64)
        total_power = float(np.sum(deposited))

    if "Power_Density_W_m2" in mesh.cell_data:
        density = np.array(mesh.cell_data["Power_Density_W_m2"], dtype=np.float64)
        valid = np.isfinite(density)
        if np.any(valid):
            peak_density = float(np.max(density[valid]))
    elif "Deposited_Power_W" in mesh.cell_data:
        # Compute density from power and area
        areas = mesh.compute_cell_sizes(length=False, area=True, volume=False)
        face_areas = np.array(areas.cell_data["Area"], dtype=np.float64)
        density = np.divide(
            deposited, face_areas,
            out=np.zeros_like(deposited),
            where=face_areas > 0,
        )
        valid = np.isfinite(density)
        if np.any(valid):
            peak_density = float(np.max(density[valid]))

    return total_power, peak_density


def find_subdirs_with_results(parent_dir):
    """
    Return a sorted list of subdirectories inside *parent_dir* that contain
    at least one .vtp or .vtm file.
    """
    subdirs = []
    for entry in sorted(os.listdir(parent_dir)):
        full_path = os.path.join(parent_dir, entry)
        if not os.path.isdir(full_path):
            continue
        if entry.upper() == "SMOOTHED":
            continue
        has_results = (glob.glob(os.path.join(full_path, "*.vtp")) or
                       glob.glob(os.path.join(full_path, "*.vtm")))
        if has_results:
            subdirs.append(full_path)
    return subdirs


def main():
    parser = argparse.ArgumentParser(
        description="Generate summary CSV reports from simulation result files.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "-i", "--input_dir",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Path to the output directory.  If it contains .vtp/.vtm files\n"
            "directly, a report is generated there.  If it contains subfolders\n"
            "with results, each subfolder gets its own report.\n"
            f"Defaults to '{DEFAULT_OUTPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "-o", "--output_filename",
        type=str,
        default=DEFAULT_REPORT_FILENAME,
        help=f"Name of the summary CSV file.  Defaults to '{DEFAULT_REPORT_FILENAME}'.",
    )
    parser.add_argument(
        "--smoothed", action="store_true",
        help="Generate reports for the SMOOTHED subfolder inside each result directory.",
    )
    args = parser.parse_args()

    input_dir = args.input_dir
    if not os.path.isdir(input_dir):
        print(f"FATAL ERROR: Directory '{input_dir}' not found.")
        return

    # Decide whether the directory itself contains results or has subfolders
    has_direct_results = (glob.glob(os.path.join(input_dir, "*.vtp")) or
                          glob.glob(os.path.join(input_dir, "*.vtm")))
    subdirs = find_subdirs_with_results(input_dir)

    if has_direct_results and not subdirs:
        dirs_to_process = [input_dir]
    elif subdirs:
        dirs_to_process = subdirs
        print(f"Found {len(dirs_to_process)} result subfolder(s) in '{input_dir}':")
        for d in dirs_to_process:
            print(f"  - {os.path.basename(d)}")
    else:
        print(f"No .vtp/.vtm files or result subfolders found in '{input_dir}'. Nothing to do.")
        return

    print(f"\n=== Report Generation ===")
    print(f"  Output filename  : {args.output_filename}")
    print(f"  Smoothed mode    : {args.smoothed}")
    print(f"  Directories      : {len(dirs_to_process)}")
    print(f"=========================\n")

    for i, result_dir in enumerate(dirs_to_process, 1):
        target_dir = result_dir
        if args.smoothed:
            smoothed_dir = os.path.join(result_dir, "SMOOTHED")
            if os.path.isdir(smoothed_dir):
                target_dir = smoothed_dir
            else:
                print(f"  [{i}/{len(dirs_to_process)}] No SMOOTHED subfolder in "
                      f"'{os.path.basename(result_dir)}', skipping.")
                continue

        print(f"\n[{i}/{len(dirs_to_process)}] {target_dir}")
        try:
            generate_summary_csv(target_dir, output_filename=args.output_filename)
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\n=== Report Generation Complete ===")


if __name__ == "__main__":
    main()
