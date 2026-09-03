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

    With no OUTPUT_* ancestor the three levels are derived from the tail of the
    path itself — see _fallback_case_scenario.
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
        output_name, case, scenario = _fallback_case_scenario(parts)
    return output_name, case, scenario


def _fallback_case_scenario(parts: tuple) -> tuple[str, str, str]:
    """(output_name, case, scenario) for a path with no OUTPUT_* ancestor.

    This used to return the literal "snapshots" as the output_name, which then
    leaked into every derived path — a coordinate transform of an assembly
    merge landed in "transformed/snapshots/assembly/", which reads as a bug
    because nothing there is a snapshot. The name is now taken from the path.
    """
    # Drop drive/root components ("d:\", "\\", "/") so they can never become
    # a folder name in a derived output path.
    names = [p for p in parts
             if p not in ("/", "\\") and ":" not in p and p.strip(" \\/")]

    # The tool's own assembly-merge output: .../post_processed/assembly/<name>.
    # Group those under "assembly" so a transform of an assembled mesh mirrors
    # the input layout as transformed/assembly/<name>/.
    if len(names) >= 2 and names[-2].lower() == "assembly":
        return "assembly", names[-1], names[-1]

    scenario = names[-1] if names else "unknown"
    case     = names[-2] if len(names) >= 2 else scenario
    # One level above case, so the three levels stay distinct instead of
    # inventing a name that is not on the path at all.
    output_name = names[-3] if len(names) >= 3 else case
    return output_name, case, scenario
