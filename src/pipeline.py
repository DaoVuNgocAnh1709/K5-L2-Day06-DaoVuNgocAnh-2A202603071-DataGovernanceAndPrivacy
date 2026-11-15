"""PoC Pipeline for Track T04: Sensor Sync Guard (Camera & IMU Synchronization).

5-step Contract:
    01 load      Verify manifest and load camera/IMU streams (supports both standard
                 feed and shock/ clock-reset feed).
    02 process   Repair camera clock resets, estimate per-segment linear drift rate
                 and initial time offset via cross-correlation between camera
                 motion_score and IMU |az - 9.81|, and perform nearest-timestamp
                 join with tolerance-based dropout detection (HOLD).
    03 rank      Prioritize top-B frames with highest uncertainty/residual into
                 review_queue.csv and render offline HTML contact_sheet.html.
    04 review    Human-in-the-loop: apply reviewer decisions/overrides from
                 review_queue_reviewed.csv via apply_overrides and record
                 immutable audit_log.jsonl.
    05 decide    Automated quality gate: evaluate overall sync accuracy and sensor
                 dropout rate to issue GO / HOLD decision with reason_code & owner.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import (
    apply_overrides,
    load_review_decisions,
    make_review_queue,
    write_review_queue,
)
from .metrics.tracks import get_track


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    if ctx.track != "t04":
        raise NotImplementedError(
            f"Track {ctx.track} is not implemented in this pipeline. "
            f"This implementation is specifically designed for Track t04 (Sensor Sync Guard)."
        )

    manifest = ctx.input_dir / "manifest.csv"
    if not manifest.exists() and (ctx.input_dir.parent / "manifest.csv").exists():
        manifest = ctx.input_dir.parent / "manifest.csv"

    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")

    cam_path = ctx.input_dir / "camera.csv"
    imu_path = ctx.input_dir / "imu.csv"
    if not cam_path.exists():
        cam_path = ctx.input_dir / "camera_clock_reset.csv"
        imu_path = ctx.input_dir / "imu_clock_reset.csv"

    if not cam_path.exists() or not imu_path.exists():
        raise FileNotFoundError(f"Missing required sensor feeds in {ctx.input_dir}")

    cam = pd.read_csv(cam_path)
    imu = pd.read_csv(imu_path)

    seg_path = ctx.input_dir / "segments.csv"
    if not seg_path.exists() and (ctx.input_dir.parent / "segments.csv").exists():
        seg_path = ctx.input_dir.parent / "segments.csv"
    segments = pd.read_csv(seg_path) if seg_path.exists() else None

    shock_cam = None
    shock_imu = None
    shock_cam_path = ctx.input_dir / "shock" / "camera_clock_reset.csv"
    shock_imu_path = ctx.input_dir / "shock" / "imu_clock_reset.csv"
    if shock_cam_path.exists() and shock_imu_path.exists():
        shock_cam = pd.read_csv(shock_cam_path)
        shock_imu = pd.read_csv(shock_imu_path)

    return {
        "manifest": pd.read_csv(manifest) if manifest.exists() else None,
        "camera": cam,
        "imu": imu,
        "segments": segments,
        "shock_camera": shock_cam,
        "shock_imu": shock_imu,
    }


# ---------------------------------------------------------------- Helper: sync stream
def _estimate_segment_drift_and_offset(
    t_cam_rel: np.ndarray,
    motion_score: np.ndarray,
    imu_t_rel: np.ndarray,
    imu_feat: np.ndarray,
    t_seg0: float,
    dt_grid: float = 0.001,
) -> tuple[float, float]:
    """Estimates linear clock drift (s/s) and initial time offset (s) for a 20s segment.

    1. Estimates linear drift rate from median normal frame interval vs nominal 1/30s.
    2. De-drifts camera timestamps relative to segment start `t_seg0`.
    3. Computes cross-correlation on a 1ms grid between camera `motion_score` and
       IMU vertical acceleration feature `|az - 9.81|` to recover initial offset.
    """
    if len(t_cam_rel) < 10 or len(imu_t_rel) < 30:
        return 0.0, 0.0

    dts = np.diff(t_cam_rel)
    normal_dts = dts[(dts > 0.030) & (dts < 0.038)]
    if len(normal_dts) > 10:
        raw_drift = float((np.median(normal_dts) - (1.0 / 30.0)) * 30.0)
        # Snap near-zero numerical precision jitter (< 0.05 ms/s) to 0.0
        est_drift = 0.0 if abs(raw_drift) < 5e-5 else round(raw_drift, 4)
    else:
        est_drift = 0.0

    t_c_dedrift = t_seg0 + (t_cam_rel - t_seg0) / (1.0 + est_drift)

    grid = np.arange(t_seg0 + 0.5, t_seg0 + 19.5, dt_grid)
    if len(grid) < 50:
        return est_drift, 0.0

    c_sig = np.interp(grid, t_c_dedrift, motion_score)
    c_sig = c_sig - np.mean(c_sig)

    i_sig = np.interp(grid, imu_t_rel, imu_feat)
    i_sig = i_sig - np.mean(i_sig)

    if np.std(c_sig) * np.std(i_sig) < 1e-6:
        return est_drift, 0.0

    corr = signal.correlate(c_sig, i_sig, mode="full")
    lags = signal.correlation_lags(len(c_sig), len(i_sig), mode="full")
    max_lag_steps = int(0.5 / dt_grid)
    valid = np.abs(lags) <= max_lag_steps
    best_lag = int(lags[valid][np.argmax(corr[valid])])
    offset0_s = float(best_lag * dt_grid)

    return est_drift, offset0_s


def _synchronize_streams(
    ctx: RunContext,
    cam: pd.DataFrame,
    imu: pd.DataFrame,
    tol_ms: float,
    log_events: bool = True,
) -> pd.DataFrame:
    t_cam = cam["t_cam_s"].values.astype(float).copy()
    imu_ts = imu["t_imu_ms"].values.astype(float)
    t0_s = imu_ts[0] / 1000.0

    # 1. Detect and repair camera clock resets (e.g., shock test mid-stream jump)
    diffs = np.diff(t_cam)
    reset_indices = np.where(diffs < -1000.0)[0]
    for r_idx in reset_indices:
        local_steps = diffs[max(0, r_idx - 20):r_idx]
        step = float(np.median(local_steps[local_steps > 0])) if np.any(local_steps > 0) else (1.0 / 30.0)
        time_jump = (t_cam[r_idx] + step) - t_cam[r_idx + 1]
        t_cam[r_idx + 1:] += time_jump
        if log_events:
            ctx.audit.log(
                actor="pipeline",
                action="clock_reset_repaired",
                item_id=str(cam.iloc[r_idx + 1]["frame_id"]),
                reason=f"Repaired clock jump of {time_jump:.2f}s at index {r_idx + 1}",
            )

    # 2. Reconstruct true frame counter k_true (robust to camera frame dropouts in any segment)
    k_true = np.zeros(len(t_cam), dtype=int)
    for i in range(1, len(t_cam)):
        dt = t_cam[i] - t_cam[i - 1]
        if (k_true[i - 1] + 1) % 600 == 0:
            k_true[i] = k_true[i - 1] + 1
        else:
            dk = max(1, int(round(dt * 30.0)))
            k_true[i] = k_true[i - 1] + dk

    segments = k_true // 600
    t_cam_rel = t_cam - t0_s
    imu_t_rel = (imu_ts - imu_ts[0]) / 1000.0
    imu_feat = np.abs(imu["az"].values - 9.81)

    matched_imu = np.full(len(t_cam), np.nan, dtype=float)
    scores = np.zeros(len(t_cam), dtype=float)
    residuals = np.zeros(len(t_cam), dtype=float)
    reasons = [""] * len(t_cam)
    actions = [""] * len(t_cam)
    slices = [""] * len(t_cam)

    for seg_idx in sorted(np.unique(segments)):
        idx = np.where(segments == seg_idx)[0]
        t_seg0 = float(seg_idx * 20.0)
        t_c = t_cam_rel[idx]
        m_score = cam["motion_score"].values[idx]

        est_drift, offset0_s = _estimate_segment_drift_and_offset(
            t_c, m_score, imu_t_rel, imu_feat, t_seg0=t_seg0, dt_grid=0.001
        )
        drift_ms_per_s = round(est_drift * 1000.0, 1)
        slice_label = f"segment={seg_idx},drift={drift_ms_per_s}"

        # De-drift and subtract initial offset to map camera timestamps to IMU reference clock
        t_corr_s = t_seg0 + (t_c - offset0_s - t_seg0) / (1.0 + est_drift)
        aligned_ts_ms = imu_ts[0] + t_corr_s * 1000.0

        pos = np.clip(np.searchsorted(imu_ts, aligned_ts_ms), 0, len(imu_ts) - 1)
        left = np.clip(pos - 1, 0, len(imu_ts) - 1)
        choose_left = np.abs(aligned_ts_ms - imu_ts[left]) < np.abs(aligned_ts_ms - imu_ts[pos])
        best_idx = np.where(choose_left, left, pos)

        seg_matched = imu_ts[best_idx]
        seg_res = np.abs(aligned_ts_ms - seg_matched)

        for local_i, global_i in enumerate(idx):
            res = float(seg_res[local_i])
            residuals[global_i] = res
            slices[global_i] = slice_label
            if res > tol_ms:
                matched_imu[global_i] = np.nan
                scores[global_i] = 1000.0 + res
                reasons[global_i] = "imu_dropout_gap"
                actions[global_i] = "hold"
            else:
                matched_imu[global_i] = float(seg_matched[local_i])
                scores[global_i] = res
                reasons[global_i] = "synchronized"
                actions[global_i] = "approve"

    evidence_img = "demo/sensor_sync_demo.png" if Path("demo/sensor_sync_demo.png").exists() else ""

    return pd.DataFrame({
        "frame_id": cam["frame_id"].values,
        "imu_t_ms": matched_imu,
        "score": np.round(scores, 4),
        "reason": reasons,
        "suggested_action": actions,
        "slice": slices,
        "evidence_path": [evidence_img] * len(cam),
        "residual_ms": residuals,
    })


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    tol_ms = float(ctx.param("tol_ms", 50.0))
    result = _synchronize_streams(ctx, data["camera"], data["imu"], tol_ms=tol_ms, log_events=True)

    # Also process shock stream if available in fixture directory
    if data.get("shock_camera") is not None and data.get("shock_imu") is not None:
        shock_res = _synchronize_streams(
            ctx, data["shock_camera"], data["shock_imu"], tol_ms=tol_ms, log_events=True
        )
        shock_out = ctx.out("pairs_shock.csv")
        shock_res[["frame_id", "imu_t_ms"]].to_csv(shock_out, index=False)
        if ctx.output_dir.parent.name == "out":
            shock_res[["frame_id", "imu_t_ms"]].to_csv(ctx.output_dir.parent / "pairs_shock.csv", index=False)

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
        reason_col="reason",
        action_col="suggested_action",
        slice_col="slice",
        evidence_col="evidence_path",
    )
    write_review_queue(q, ctx.out("review_queue.csv"))
    render_contact_sheet(
        q,
        ctx.out("contact_sheet.html"),
        image_root=Path("."),
        title="T04 Sensor Sync Guard - Review Queue",
    )
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Applies human review decisions if review_queue_reviewed.csv is provided."""
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if not reviewed_path.exists() and ctx.output_dir.resolve() == Path("out/poc").resolve():
        fallback_path = Path("reports/review_queue.csv")
        if fallback_path.exists():
            reviewed_path = fallback_path

    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(reviewed)

        # Apply standard value overrides via common.review.apply_overrides
        scored = apply_overrides(
            scored,
            reviewed,
            id_col="frame_id",
            value_col="imu_t_ms",
            audit=ctx.audit,
        )
        scored["imu_t_ms"] = pd.to_numeric(scored["imu_t_ms"], errors="coerce")

        for _, row in reviewed.iterrows():
            fid = str(row["item_id"])
            dec = str(row.get("reviewer_decision", "")).strip().lower()
            reviewer = str(row.get("reviewer", "unknown"))
            override = row.get("override_value")
            has_override = pd.notna(override) and str(override).strip() != ""

            if dec in ("reject", "hold", "skip") and not has_override:
                scored.loc[scored["frame_id"] == fid, "imu_t_ms"] = np.nan
                ctx.audit.log(
                    actor=reviewer,
                    action="override_hold",
                    item_id=fid,
                    reason=str(row.get("review_note", "Manual rejection to HOLD")),
                )
            elif dec == "approve":
                ctx.audit.log(
                    actor=reviewer,
                    action="approve",
                    item_id=fid,
                    reason=str(row.get("review_note", "Manual approval")),
                )

        # Persist reviewed queue columns to review_queue.csv for auditable submission
        write_review_queue(reviewed, ctx.out("review_queue.csv"))

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
            evidence=[str(ctx.out("pairs.csv"))],
        )

    # 2. HOLD if median synchronization residual exceeds 20 ms
    if median_res > 20.0:
        return Decision(
            status="HOLD",
            reason_code="HIGH_RESIDUAL_DESYNC",
            reason=f"Median synchronization residual ({median_res:.1f}ms) exceeds tolerance threshold (20.0ms)",
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("pairs.csv"))],
        )

    # 3. Otherwise, pass with GO
    return Decision(
        status="GO",
        reason_code="SENSOR_SYNC_VERIFIED",
        reason=f"Sync verified: median residual={median_res:.1f}ms, p95={p95_res:.1f}ms, dropout held={n_held} frames ({hold_ratio:.1%})",
        owner=owner,
        scope=ctx.track,
        evidence=[str(ctx.out("pairs.csv"))],
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
