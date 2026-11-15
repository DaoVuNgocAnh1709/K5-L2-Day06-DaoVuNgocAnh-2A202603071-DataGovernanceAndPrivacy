"""PoC Pipeline for Track T04: Sensor Sync Guard (Camera & IMU Synchronization).

5-step Contract:
    01 load      Verify manifest and load camera.csv, imu.csv, and segments.csv.
    02 process   Cross-correlation alignment between camera motion_score and IMU az
                 variation across 20-second segments; nearest-timestamp join with
                 tolerance-based dropout detection.
    03 rank      Prioritize top-B frames with highest uncertainty/residual into review_queue.csv.
    04 review    Human-in-the-loop: apply reviewer decisions/overrides from
                 review_queue_reviewed.csv and record immutable audit_log.jsonl.
    05 decide    Automated quality gate: evaluate overall sync accuracy and sensor
                 dropout rate to issue GO / HOLD decision with reason_code & owner.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    if ctx.track != "t04":
        raise NotImplementedError(
            f"Track {ctx.track} is not implemented in this pipeline. "
            f"This implementation is specifically designed for Track t04 (Sensor Sync Guard)."
        )

    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")

    cam_path = ctx.input_dir / "camera.csv"
    imu_path = ctx.input_dir / "imu.csv"
    if not cam_path.exists() or not imu_path.exists():
        raise FileNotFoundError(f"Missing required sensor feeds in {ctx.input_dir}")

    cam = pd.read_csv(cam_path)
    imu = pd.read_csv(imu_path)
    
    seg_path = ctx.input_dir / "segments.csv"
    segments = pd.read_csv(seg_path) if seg_path.exists() else None

    return {
        "manifest": pd.read_csv(manifest) if manifest.exists() else None,
        "camera": cam,
        "imu": imu,
        "segments": segments
    }


# ---------------------------------------------------------------- Helper: estimate offset
def _estimate_segment_offset(
    cam_sub: pd.DataFrame,
    imu_sub: pd.DataFrame,
    dt: float = 0.002,
    t_start: float = 0.0,
    t_end: float = 20.0
) -> float:
    """Estimates the time offset in milliseconds that aligns camera with IMU.
    
    Uses cross-correlation between camera `motion_score` and IMU vertical acceleration
    variation |az - 9.81| (both spike during physical vehicle bump events).
    """
    if len(cam_sub) < 10 or len(imu_sub) < 30:
        return 0.0

    # Grid for interpolation (trim 1.0s boundary to avoid edge artifacts)
    t_min = t_start + 1.0
    t_max = t_end - 1.0
    if t_max <= t_min:
        return 0.0

    t_grid = np.arange(t_min, t_max, dt)
    if len(t_grid) < 50:
        return 0.0

    # Interpolate camera motion_score
    cam_times = cam_sub["t_norm_s"].values
    cam_motion = cam_sub["motion_score"].values
    cam_sig = np.interp(t_grid, cam_times, cam_motion)
    cam_sig = cam_sig - np.mean(cam_sig)

    # Interpolate IMU vertical acceleration feature
    imu_times = imu_sub["t_norm_s"].values
    imu_feat = np.abs(imu_sub["az"].values - 9.81)
    imu_sig = np.interp(t_grid, imu_times, imu_feat)
    imu_sig = imu_sig - np.mean(imu_sig)

    std_prod = np.std(cam_sig) * np.std(imu_sig)
    if std_prod < 1e-6:
        return 0.0

    # Cross-correlation: correlate(imu, cam)
    corr = signal.correlate(imu_sig, cam_sig, mode="full")
    lags = signal.correlation_lags(len(imu_sig), len(cam_sig), mode="full")
    best_lag = lags[np.argmax(corr)]

    # Positive lag means IMU event is at t_cam + lag * dt
    offset_ms = float(best_lag * dt * 1000.0)
    return offset_ms


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    cam = data["camera"].copy()
    imu = data["imu"].copy()
    tol_ms = float(ctx.param("tol_ms", 50.0))

    # Detect and repair camera clock reset (shock test support)
    t_cam = cam["t_cam_s"].values.copy()
    diffs = np.diff(t_cam)
    reset_indices = np.where(diffs < -1000.0)[0]
    if len(reset_indices) > 0:
        reset_idx = reset_indices[0]
        # Reconstruct continuous time assuming ~30 Hz frame interval
        est_step = np.median(diffs[diffs > 0]) if np.any(diffs > 0) else 1.0 / 30.0
        time_jump = (t_cam[reset_idx] + est_step) - t_cam[reset_idx + 1]
        t_cam[reset_idx + 1:] += time_jump
        ctx.audit.log(
            actor="pipeline",
            action="clock_reset_repaired",
            item_id=str(cam.iloc[reset_idx + 1]["frame_id"]),
            note=f"Repaired clock jump of {time_jump:.2f}s at index {reset_idx + 1}"
        )

    t_base = t_cam[0]
    cam["t_norm_s"] = t_cam - t_base
    imu["t_norm_s"] = (imu["t_imu_ms"].values - t_base * 1000.0) / 1000.0

    # Determine segments: 600 frames (~20 seconds @ 30 fps) per segment
    seg_length_s = 20.0
    cam["segment"] = (cam.index // 600).astype(int)

    all_matched_imu = []
    all_scores = []
    all_reasons = []
    all_residuals = []

    imu_ts = imu["t_imu_ms"].values

    for seg_idx, cam_seg in cam.groupby("segment", sort=True):
        s_start = seg_idx * seg_length_s
        s_end = (seg_idx + 1) * seg_length_s

        # Slice IMU with a +/- 2-second buffer for robust cross-correlation
        imu_seg = imu[(imu["t_norm_s"] >= s_start - 2.0) & (imu["t_norm_s"] <= s_end + 2.0)]

        # Estimate offset via cross-correlation
        offset_ms = _estimate_segment_offset(cam_seg, imu_seg, dt=0.002, t_start=s_start, t_end=s_end)

        # Apply offset to camera timestamps
        aligned_ts_ms = t_cam[cam_seg.index] * 1000.0 + offset_ms

        # Nearest neighbor lookup in IMU timestamps
        pos = np.searchsorted(imu_ts, aligned_ts_ms)
        pos = np.clip(pos, 0, len(imu_ts) - 1)
        left = np.clip(pos - 1, 0, len(imu_ts) - 1)

        choose_left = np.abs(aligned_ts_ms - imu_ts[left]) < np.abs(aligned_ts_ms - imu_ts[pos])
        best_idx = np.where(choose_left, left, pos)

        matched_ts = imu_ts[best_idx]
        residuals = np.abs(aligned_ts_ms - matched_ts)

        for i, res in enumerate(residuals):
            # If closest IMU reading exceeds tolerance, it indicates a sensor dropout -> HOLD
            if res > tol_ms:
                all_matched_imu.append(np.nan)
                all_scores.append(1000.0 + res)
                all_reasons.append("imu_dropout_gap")
            else:
                all_matched_imu.append(matched_ts[i])
                all_scores.append(res)
                all_reasons.append("synchronized")
            all_residuals.append(res)

    result = pd.DataFrame({
        "frame_id": cam["frame_id"].values,
        "imu_t_ms": all_matched_imu,
        "score": all_scores,
        "reason": all_reasons,
        "residual_ms": all_residuals
    })

    return result


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Prioritizes top-B uncertain frames (highest residuals / dropouts) for human inspection."""
    budget = int(ctx.param("budget", 20))
    q = make_review_queue(
        scored,
        id_col="frame_id",
        score_col="score",
        budget=budget,
        reason_col="reason"
    )
    write_review_queue(q, ctx.out("review_queue.csv"))
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Applies human review decisions if review_queue_reviewed.csv is provided."""
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(reviewed)
        scored = scored.copy()
        
        for _, row in reviewed.iterrows():
            fid = row["item_id"]
            dec = str(row.get("reviewer_decision", "")).lower()
            reviewer = row.get("reviewer", "unknown")
            override = row.get("override_value")
            
            if dec in ("reject", "hold", "skip"):
                scored.loc[scored["frame_id"] == fid, "imu_t_ms"] = np.nan
                ctx.audit.log(
                    actor=str(reviewer),
                    action="override_hold",
                    item_id=str(fid),
                    note=str(row.get("review_note", "Manual rejection to HOLD"))
                )
            elif dec == "edit" and pd.notna(override) and str(override).strip() != "":
                try:
                    val = float(override)
                    scored.loc[scored["frame_id"] == fid, "imu_t_ms"] = val
                    ctx.audit.log(
                        actor=str(reviewer),
                        action="override_value",
                        item_id=str(fid),
                        after=val,
                        note=str(row.get("review_note", "Manual timestamp override"))
                    )
                except ValueError:
                    pass
            elif dec == "approve":
                ctx.audit.log(
                    actor=str(reviewer),
                    action="approve",
                    item_id=str(fid),
                    note=str(row.get("review_note", "Manual approval"))
                )
    return scored


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    """Evaluates data governance criteria to emit a formal GO / HOLD decision."""
    n_total = len(final)
    n_held = int(final["imu_t_ms"].isna().sum())
    hold_ratio = n_held / max(1, n_total)
    
    valid_residuals = final.loc[final["imu_t_ms"].notna(), "residual_ms"]
    median_res = float(valid_residuals.median()) if len(valid_residuals) > 0 else 999.0
    p95_res = float(valid_residuals.quantile(0.95)) if len(valid_residuals) > 0 else 999.0

    ctx.stats["n_held_frames"] = n_held
    ctx.stats["hold_ratio"] = hold_ratio
    ctx.stats["median_residual_ms"] = median_res
    ctx.stats["p95_residual_ms"] = p95_res

    owner = ctx.config.get("owner", "Dao Vu Ngoc Anh (Lead Data QA)")

    # Governance Policy Thresholds:
    # 1. HOLD if sensor dropout rate exceeds 25% of the total dataset
    if hold_ratio > 0.25:
        return Decision(
            status="HOLD",
            reason_code="EXCESSIVE_SENSOR_DROPOUT",
            reason=f"Sensor dropout rate ({hold_ratio:.1%}) exceeds safety threshold (25.0%)",
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("pairs.csv"))]
        )

    # 2. HOLD if median synchronization residual exceeds 20 ms
    if median_res > 20.0:
        return Decision(
            status="HOLD",
            reason_code="HIGH_RESIDUAL_DESYNC",
            reason=f"Median synchronization residual ({median_res:.1f}ms) exceeds tolerance threshold (20.0ms)",
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("pairs.csv"))]
        )

    # 3. Otherwise, pass with GO
    return Decision(
        status="GO",
        reason_code="SENSOR_SYNC_VERIFIED",
        reason=f"Sync verified: median residual={median_res:.1f}ms, p95={p95_res:.1f}ms, dropout held={n_held} frames ({hold_ratio:.1%})",
        owner=owner,
        scope=ctx.track,
        evidence=[str(ctx.out("pairs.csv"))]
    )


# ---------------------------------------------------------------- Pipeline Runner
def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    
    # Save contract predictions: out/pairs.csv
    pred_path = ctx.out(spec.pred_file)
    final[spec.pred_columns].to_csv(pred_path, index=False)
    
    # Write formal decision and decision audit log
    decision = decide(ctx, final)
    write_decision(decision, ctx.out("decision.json"), ctx.out("decision_log.csv"))
