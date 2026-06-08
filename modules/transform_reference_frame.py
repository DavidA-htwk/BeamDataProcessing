#!/usr/bin/env python3
"""Transform point coordinates into a new reference frame.

Transform order (applied per point):
1) Rotate around Z by -116 deg
2) Translate by +11.410436 m along X
3) Translate by +26.617882 m along Y
4) Translate by +0.920 m along Z
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable, TextIO


DEFAULT_ANGLE_DEG = -116.0
DEFAULT_DX = 11.410436
DEFAULT_DY = 26.617882
DEFAULT_DZ = 0.920

DEFAULT_COLUMN_CANDIDATES = [
    ("X", "Y", "Z"),
    ("x", "y", "z"),
    ("pos_x", "pos_y", "pos_z"),
]


def find_header_line_and_columns(path: Path) -> tuple[int, list[str]]:
    """Return (header_line_index, columns) where index is 0-based."""
    with path.open("r", newline="", encoding="utf-8") as f:
        for idx, raw in enumerate(f):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            columns = [c.strip() for c in stripped.split(",")]
            return idx, columns
    raise ValueError(f"Could not find CSV header in {path}")


def detect_xyz_columns(columns: Iterable[str]) -> tuple[str, str, str]:
    colset = set(columns)
    for x_col, y_col, z_col in DEFAULT_COLUMN_CANDIDATES:
        if {x_col, y_col, z_col}.issubset(colset):
            return x_col, y_col, z_col
    raise ValueError(
        "Could not auto-detect coordinate columns. Use --x-col/--y-col/--z-col."
    )


def write_preamble_lines(inp: TextIO, out: TextIO, header_line_index: int) -> None:
    """Copy all lines before header unchanged (comments/metadata)."""
    inp.seek(0)
    for _ in range(header_line_index):
        out.write(inp.readline())


def transform_xyz(x: float, y: float, z: float, cos_t: float, sin_t: float, dx: float, dy: float, dz: float) -> tuple[float, float, float]:
    """Apply Z rotation then XYZ translations."""
    x_rot = cos_t * x - sin_t * y
    y_rot = sin_t * x + cos_t * y
    z_rot = z

    return x_rot + dx, y_rot + dy, z_rot + dz


def process_file(
    input_path: Path,
    output_path: Path,
    x_col: str | None,
    y_col: str | None,
    z_col: str | None,
    angle_deg: float,
    dx: float,
    dy: float,
    dz: float,
    coord_scale: float = 1.0,
) -> None:
    header_line_index, columns = find_header_line_and_columns(input_path)

    if x_col is None or y_col is None or z_col is None:
        x_col, y_col, z_col = detect_xyz_columns(columns)

    missing = [c for c in (x_col, y_col, z_col) if c not in columns]
    if missing:
        raise ValueError(f"Missing coordinate columns in input CSV: {missing}")

    theta = math.radians(angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    with input_path.open("r", newline="", encoding="utf-8") as inp, output_path.open(
        "w", newline="", encoding="utf-8"
    ) as out:
        write_preamble_lines(inp, out, header_line_index)

        for _ in range(header_line_index):
            next(inp)

        reader = csv.DictReader(inp)
        writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
        writer.writeheader()

        if reader.fieldnames is None:
            raise ValueError("CSV header could not be read.")

        for row_idx, row in enumerate(reader, start=1):
            try:
                x_val = float(row[x_col])
                y_val = float(row[y_col])
                z_val = float(row[z_col])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid numeric coordinate at data row {row_idx} in {input_path}"
                ) from exc

            x_new, y_new, z_new = transform_xyz(
                x=x_val,
                y=y_val,
                z=z_val,
                cos_t=cos_t,
                sin_t=sin_t,
                dx=dx,
                dy=dy,
                dz=dz,
            )

            row[x_col] = f"{x_new * coord_scale:.12g}"
            row[y_col] = f"{y_new * coord_scale:.12g}"
            row[z_col] = f"{z_new * coord_scale:.12g}"
            writer.writerow(row)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = SCRIPT_DIR / "input"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate XYZ around Z then translate to a new reference frame."
    )
    parser.add_argument(
        "input_csv",
        type=Path,
        nargs="?",
        default=None,
        help="Path to input CSV file (omit to process all CSVs in the 'input' folder)",
    )
    parser.add_argument(
        "output_csv",
        type=Path,
        nargs="?",
        default=None,
        help="Path to output CSV file (omit to write to the 'output' folder)",
    )

    parser.add_argument("--x-col", default=None, help="Name of X column")
    parser.add_argument("--y-col", default=None, help="Name of Y column")
    parser.add_argument("--z-col", default=None, help="Name of Z column")

    parser.add_argument("--angle-deg", type=float, default=DEFAULT_ANGLE_DEG)
    parser.add_argument("--dx", type=float, default=DEFAULT_DX)
    parser.add_argument("--dy", type=float, default=DEFAULT_DY)
    parser.add_argument("--dz", type=float, default=DEFAULT_DZ)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.input_csv is not None:
        # Single-file mode: both paths supplied explicitly
        output_path = args.output_csv if args.output_csv is not None else DEFAULT_OUTPUT_DIR / args.input_csv.name
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        process_file(
            input_path=args.input_csv,
            output_path=output_path,
            x_col=args.x_col,
            y_col=args.y_col,
            z_col=args.z_col,
            angle_deg=args.angle_deg,
            dx=args.dx,
            dy=args.dy,
            dz=args.dz,
        )
        print(f"Wrote transformed CSV: {output_path}")
    else:
        # Batch mode: process all CSVs in the input folder
        input_files = sorted(DEFAULT_INPUT_DIR.glob("*.csv"))
        if not input_files:
            print(f"No CSV files found in {DEFAULT_INPUT_DIR}")
            return
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for input_path in input_files:
            output_path = DEFAULT_OUTPUT_DIR / input_path.name
            process_file(
                input_path=input_path,
                output_path=output_path,
                x_col=args.x_col,
                y_col=args.y_col,
                z_col=args.z_col,
                angle_deg=args.angle_deg,
                dx=args.dx,
                dy=args.dy,
                dz=args.dz,
            )
            print(f"Wrote transformed CSV: {output_path}")


if __name__ == "__main__":
    main()
