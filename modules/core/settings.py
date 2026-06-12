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
