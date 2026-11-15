"""Baseline for Track T04 (Sensor Sync Guard).

A naive baseline that performs nearest-timestamp join WITHOUT offset estimation,
drift correction, or dropout detection. Serves as the comparison anchor for PoC.

Run with:
    python -m src.main --track t04 --pipeline src.examples.t04_baseline --output out/baseline --run-name baseline --offline
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..common.manifest import verify_manifest
from ..common.review import make_review_queue, write_review_queue
from ..metrics.tracks import get_track


def run(ctx: RunContext) -> None:
    spec = get_track("t04")
    manifest = ctx.input_dir / "manifest.csv"
    if manifest.exists():
        problems = verify_manifest(manifest)
        if problems and ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed: {problems[:3]}")

    cam = pd.read_csv(ctx.input_dir / "camera.csv")
    imu = pd.read_csv(ctx.input_dir / "imu.csv")
    ctx.stats["n_items"] = len(cam)

    # Naive join: camera seconds * 1000 to IMU milliseconds
    cam_ts_ms = cam["t_cam_s"].values * 1000.0
    imu_ts = imu["t_imu_ms"].values

    # Nearest neighbor matching using searchsorted
    idx = np.searchsorted(imu_ts, cam_ts_ms)
    idx = np.clip(idx, 0, len(imu_ts) - 1)
    
    # Consider left neighbor if closer
    left_idx = np.clip(idx - 1, 0, len(imu_ts) - 1)
    choose_left = np.abs(cam_ts_ms - imu_ts[left_idx]) < np.abs(cam_ts_ms - imu_ts[idx])
    matched_idx = np.where(choose_left, left_idx, idx)
    
    matched_imu_t = imu_ts[matched_idx]
    residuals = np.abs(cam_ts_ms - matched_imu_t)

    out = pd.DataFrame({
        "frame_id": cam["frame_id"],
        "imu_t_ms": matched_imu_t,
        "score": residuals,
        "reason": "naive_join_residual"
    })

    budget = int(ctx.param("budget", 20))
    q = make_review_queue(out, id_col="frame_id", score_col="score", budget=budget, reason_col="reason")
    write_review_queue(q, ctx.out("review_queue.csv"))

    # Required output columns: frame_id, imu_t_ms
    out[["frame_id", "imu_t_ms"]].to_csv(ctx.out(spec.pred_file), index=False)

    write_decision(
        Decision(
            status="HOLD",
            reason_code="BASELINE_ONLY",
            reason="Naive nearest-neighbor baseline: no offset compensation applied",
            owner=ctx.config.get("owner", "baseline"),
            scope=ctx.track,
            evidence=[str(ctx.out(spec.pred_file))]
        ),
        ctx.out("decision.json"),
        ctx.out("decision_log.csv")
    )
