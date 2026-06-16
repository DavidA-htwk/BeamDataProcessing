"""
modules/settings.py
-------------------
Application-wide constants and settings persistence (JSON config files).
"""

from __future__ import annotations

import json
from pathlib import Path

# ── Array / physics constants ─────────────────────────────────────────────────
ARRAY_NAME    = "Power_Density_W_m2"
POWER_ARRAY   = "Deposited_Power_W"
FEATURE_ANGLE = 30.0   # degrees

# Spatial radius for proximity-based point flagging (mesh coordinate units).
# Any input point within this distance of a detected feature-edge point is also
# flagged, catching cells on small steps or closely parallel edges that fall
# below FEATURE_ANGLE.  Set to 0.0 to disable.
SMOOTH_PROXIMITY_RADIUS = 0.03

# Smart-smooth AUTO mode parameters.
# SPIKE_SIGMA    — local z-score threshold: a cell is a candidate if its value
#                  exceeds local_neighbor_mean + SPIKE_SIGMA * local_neighbor_std.
# MIN_NEIGHBORS  — minimum point-connected neighbors required for reliable local
#                  statistics; cells with fewer are skipped.
# SMOOTH_K_RING  — topological k-ring radius for the per-candidate patch used to
#                  classify edge vs. spike via local vtkFeatureEdges.
SPIKE_SIGMA    = 2.0
MIN_NEIGHBORS  = 3
SMOOTH_K_RING  = 3
# Ratio filter: candidate must also satisfy val > max(neighbor_vals) * SPIKE_RATIO.
# Eliminates gradient cells (which are only slightly above their peak neighbor) and
# keeps only true isolated needles.  Set to 0.0 to disable (sigma only).
# Typical useful range: 1.5 – 3.0.  With 1.5, a cell must be ≥ 50 % above its
# single highest neighbor to qualify — gradient slopes never satisfy this.
# Not exposed in the GUI; set per-component in the config JSON if needed.
SPIKE_RATIO    = 0.0
# Secondary edge-direct pass: edge-adjacent cells above this global percentile
# of all non-zero values are added as candidates regardless of local z-score.
# Catches tight clusters of 2–3 hot cells at an edge whose mutual elevation
# inflates each other's local mean, defeating the sigma threshold.
# 99.9 → top 0.1 % of non-zero values; for 300 k non-zero cells that is ~300 seeds.
EDGE_TOP_PERCENTILE = 99.9

# Absolute path to the ParaView executable used to generate "Open in ParaView"
# launcher scripts alongside the CSV.  Set to "" to disable launcher generation.
PARAVIEW_EXE: str = (
    r"C:\users\attelnd\Work Folders\Desktop"
    r"\ParaView-6.1.0-Windows-Python3.12-msvc2017-AMD64\bin\paraview.exe"
)

# Settings file lives at project root / config / (two levels above modules/core/).
SETTINGS_FILE: Path = Path(__file__).resolve().parent.parent.parent / "config" / "data_handling_settings.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val, default: float) -> float:
    """Parse *val* as float, returning *default* on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def load_settings() -> dict:
    """Load settings from SETTINGS_FILE, following last_config_path if set."""
    base: dict = {}
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            pass

    last = base.get("last_config_path", "")
    if last and last != str(SETTINGS_FILE):
        p = Path(last)
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["last_config_path"] = last
                return cfg
            except Exception:
                pass
    return base


def save_settings(cfg: dict) -> None:
    """Persist *cfg* to SETTINGS_FILE, preserving last_config_path."""
    try:
        existing: dict = {}
        if SETTINGS_FILE.exists():
            try:
                with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        if "last_config_path" in existing:
            cfg = {**cfg, "last_config_path": existing["last_config_path"]}
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save settings: {e}")


def remember_cfg_path(path: str) -> None:
    """Write last_config_path into SETTINGS_FILE so it survives restarts."""
    try:
        existing: dict = {}
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        existing["last_config_path"] = path
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass
