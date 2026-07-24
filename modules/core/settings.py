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
# Sliver-cell power filter: cells with Deposited_Power_W below this threshold
# are treated as mesh artifacts and replaced with their area-weighted neighbour
# mean of Power_Density_W_m2.  Runs before sigma/edge smoothing and is
# completely independent of it.  Set to 0.0 to disable.
# Typical value: 1.0 (any cell depositing less than 1 W is suspect).
MIN_POWER_W    = 0.0
# Secondary edge-direct pass: edge-adjacent cells above this global percentile
# of all non-zero values are added as candidates regardless of local z-score.
# Catches tight clusters of 2–3 hot cells at an edge whose mutual elevation
# inflates each other's local mean, defeating the sigma threshold.
# 99.9 → top 0.1 % of non-zero values; for 300 k non-zero cells that is ~300 seeds.
EDGE_TOP_PERCENTILE = 99.9

# ── "True max area" guard ──────────────────────────────────────────────────
# Protects genuine, physically-real hot-spots from being smoothed away just
# because they sit on/near a feature edge or in the global top percentile.
# A lone sliver/mesh-artifact spike has essentially no support around it
# (its neighbours quickly drop to low/zero values); a real hot-spot instead
# sits inside a broader plateau of comparably high values. Both the AUTO
# candidate pipeline and EDGE mode look at a WIDER neighbourhood (more
# topological hops than the immediate 1-ring used elsewhere) before
# accepting a cell for smoothing: if enough cells within that bigger area
# are already comparably high, the candidate is left untouched.
# TRUE_MAX_GUARD_K_RING     — topological hop radius of the wider check area
#                             (bigger than the 1-ring used by the sigma/ratio
#                             tests, so a real plateau of high cells is seen).
# TRUE_MAX_GUARD_RATIO      — a neighbour counts as "supporting" the
#                             candidate's value if it is >= this fraction of
#                             the candidate's own value.
# TRUE_MAX_GUARD_MIN_SUPPORT— minimum number of supporting neighbours inside
#                             the wider area required to treat the candidate
#                             as part of a genuine high-value area (and thus
#                             skip smoothing it).
TRUE_MAX_GUARD_K_RING      = 3
TRUE_MAX_GUARD_RATIO       = 0.5
TRUE_MAX_GUARD_MIN_SUPPORT = 4

# ── Main high-value area detection (always computed, reported in CSV) ─────
# Reports the peak value that is genuinely representative of a broad,
# physically significant load area — NOT necessarily the file's plain global
# max, which can be a small stress-concentration spike (e.g. at a fillet/
# hole) that is even a literal topological SUBSET of / embedded inside the
# main load area itself. Because it can be embedded inside the main area
# (touching/surrounded by it, not a separate island elsewhere on the mesh),
# a single global threshold + connected-components pass cannot separate it:
# whatever threshold is inclusive enough to capture the real broad area also
# reconnects it to the embedded spike, merging them into one region whose
# max is trivially the spike's value again.
# Instead, cells are scanned in descending value order; each candidate is
# accepted as "the" representative peak only if a WIDE topological
# neighbourhood around it (MAIN_AREA_SUPPORT_K_RING hops — deliberately much
# bigger than the 1-ring/local checks used elsewhere) contains at least
# MAIN_AREA_SUPPORT_MIN_COUNT other cells whose value is itself >=
# MAIN_AREA_SUPPORT_RATIO * the candidate's value. A small embedded spike
# cluster (a handful to a few dozen cells) cannot manufacture that many
# comparably-high neighbours across a wide radius, so it is skipped in
# favour of the next-highest candidate, until one is found that truly has
# broad support — i.e. sits inside a large contiguous elevated area.
# MAIN_AREA_MAX_SCAN_CANDIDATES bounds the scan (the answer is normally found
# within the first few dozen candidates since spikes are rare/small); if
# nothing qualifies within the budget the plain global max is returned.
# Not exposed in the GUI — always computed and written to the CSV. These are
# heuristic knobs — tune here per-dataset if a specific mesh still needs a
# bigger/smaller neighbourhood or a stricter/looser support requirement.
MAIN_AREA_SUPPORT_K_RING        = 6
MAIN_AREA_SUPPORT_RATIO         = 0.7
MAIN_AREA_SUPPORT_MIN_COUNT     = 50
MAIN_AREA_MAX_SCAN_CANDIDATES   = 2000

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
