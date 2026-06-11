"""
modules/path_utils.py
---------------------
Helpers for parsing OUTPUT_* folder hierarchies into (output_name, case, scenario).
"""

from __future__ import annotations

import re
from pathlib import Path


def _looks_like_scenario(name: str) -> bool:
    """Return True if *name* looks like a scenario code.

    Scenarios contain digits (beam/tilt/orientation parameters);
    case/group identifiers like 'DNB_ALL', 'DNB_BTR_COMPARE', 'DNB_BOTTOM' do not.
    """
    return bool(re.search(r'\d', name))


def extract_case_scenario(folder: str) -> tuple[str, str, str]:
    """Find the OUTPUT_* ancestor and return (output_name, case, scenario).

    Handles any depth of nesting below OUTPUT_* with a bottom-up rule:
      - 0 sub-levels  → case = scenario = output_suffix
      - 1 sub-level   → case = suffix if it looks like a scenario, else sub[0];
                         scenario = sub[0]
      - 2 sub-levels  → case = sub[0] unless sub[0] looks like a scenario
                         (in which case case = output_suffix); scenario = sub[0]
      - 3+ sub-levels → case = sub[-2], scenario = sub[-1]
                         (ignores intermediate grouping folders like 'DNB_ALL')

    Storage-only terminal folder names (e.g. "SMOOTHED") are stripped from the
    tail of sub before depth logic is applied, so files sitting inside a SMOOTHED
    sub-folder resolve identically to files in the parent scenario folder.

    Falls back to ("snapshots", parts[-2], parts[-1]) if no OUTPUT_* is found.
    """
    # Folder names that are pure storage artefacts and carry no case/scenario info
    _STORAGE_FOLDERS = {"SMOOTHED", "RAW", "RESULTS", "OUTPUT"}

    parts = Path(folder).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p.upper().startswith("OUTPUT_"))
        output_name   = parts[idx]
        output_suffix = output_name[len("OUTPUT_"):]
        sub = parts[idx + 1:]   # everything after OUTPUT_*

        # Strip trailing storage-only folder names
        while sub and sub[-1].upper() in _STORAGE_FOLDERS:
            sub = sub[:-1]

        if len(sub) == 0:
            case = output_suffix or "unknown"
            scenario = case
        elif len(sub) == 1:
            if _looks_like_scenario(sub[0]):
                case = output_suffix or sub[0]
            else:
                case = sub[0]
            scenario = sub[0]
        elif len(sub) == 2:
            if _looks_like_scenario(sub[0]):
                case     = output_suffix or sub[0]
                scenario = sub[0]
            else:
                case     = sub[0]
                scenario = sub[1]
        else:
            # 3+ levels: skip intermediate grouping folders; use last two
            case     = sub[-2]
            scenario = sub[-1]
    except StopIteration:
        output_name = "snapshots"
        case        = parts[-2] if len(parts) >= 2 else "unknown"
        scenario    = parts[-1] if len(parts) >= 1 else "unknown"
    return output_name, case, scenario
