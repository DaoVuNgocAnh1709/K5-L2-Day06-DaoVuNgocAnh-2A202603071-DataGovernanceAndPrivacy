"""Unit tests for Track T04: Sensor Sync Guard pipeline."""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from src import main as runner
from src.common.io import read_json


def test_t04_pipeline_e2e_contract_success(tmp_path):
    """Verifies that the T04 pipeline runs end-to-end under offline conditions,

    producing valid schema outputs, unique frame IDs, and formal GO decision.
    """
    out_dir = tmp_path / "poc"
    rc = runner.main([
        "--track", "t04",
        "--input", "fixtures/t04_sensor_sync",
        "--output", str(out_dir),
        "--offline",
        "--seed", "42"
    ])
    assert rc == 0, f"Expected exit code 0, got {rc}"

    # Verify pairs.csv
    pred_path = out_dir / "pairs.csv"
    assert pred_path.exists(), "out/pairs.csv must exist"
    pred = pd.read_csv(pred_path)
    assert list(pred.columns) == ["frame_id", "imu_t_ms"]
    assert len(pred) == 3570
    assert pred["frame_id"].is_unique

    # Verify timestamps are ordered for valid pairs
    valid = pred["imu_t_ms"].dropna()
    assert len(valid) > 3000
    assert valid.is_monotonic_increasing

    # Verify review_queue.csv
    queue_path = out_dir / "review_queue.csv"
    assert queue_path.exists()
    q = pd.read_csv(queue_path)
    assert len(q) == 20

    # Verify decision.json
    dec_path = out_dir / "decision.json"
    assert dec_path.exists()
    dec = read_json(dec_path)
    assert dec["status"] == "GO"
    assert dec["reason_code"] == "SENSOR_SYNC_VERIFIED"
    assert "Dao Vu Ngoc Anh" in dec["owner"]


def test_t04_pipeline_detects_and_holds_sensor_dropout(tmp_path):
    """Verifies that sensor dropout periods are strictly put on HOLD (empty imu_t_ms).

    Ensures that frames in known gap [1700000044990, 1700000046500] are flagged.
    """
    out_dir = tmp_path / "dropout_test"
    rc = runner.main([
        "--track", "t04",
        "--input", "fixtures/t04_sensor_sync",
        "--output", str(out_dir),
        "--offline",
        "--seed", "42"
    ])
    assert rc == 0

    pred = pd.read_csv(out_dir / "pairs.csv")
    held_frames = pred[pred["imu_t_ms"].isna()]
    
    # Must hold at least 30 frames in the 1.5s dropout window
    assert len(held_frames) >= 30, f"Expected >= 30 held frames, got {len(held_frames)}"
    
    # Key dropout frames must be among held
    assert "f01372" in held_frames["frame_id"].values
    assert "f01373" in held_frames["frame_id"].values

    # Review queue must prioritize dropout frames
    q = pd.read_csv(out_dir / "review_queue.csv")
    assert any(q["reason"] == "imu_dropout_gap")


def test_t04_human_review_and_audit_override(tmp_path):
    """Verifies that human review decisions in review_queue_reviewed.csv

    are properly applied and logged to audit_log.jsonl.
    """
    out_dir = tmp_path / "review_test"
    # First run to generate review_queue.csv
    rc1 = runner.main([
        "--track", "t04",
        "--input", "fixtures/t04_sensor_sync",
        "--output", str(out_dir),
        "--offline"
    ])
    assert rc1 == 0

    # Human reviewer provides decisions
    reviewed = pd.DataFrame({
        "rank": [1],
        "item_id": ["f01372"],
        "score": [1741.0],
        "reason": ["imu_dropout_gap"],
        "suggested_action": [""],
        "slice": [""],
        "evidence_path": ["fixtures/t04_sensor_sync/camera.csv"],
        "reviewer": ["Dao Vu Ngoc Anh"],
        "reviewer_decision": ["reject"],
        "override_value": [""],
        "review_note": ["Confirmed hardware drop"],
        "reviewed_at": ["2026-11-14T16:22:00Z"]
    })
    reviewed.to_csv(out_dir / "review_queue_reviewed.csv", index=False)

    # Second run to apply review
    rc2 = runner.main([
        "--track", "t04",
        "--input", "fixtures/t04_sensor_sync",
        "--output", str(out_dir),
        "--offline"
    ])
    assert rc2 == 0

    audit_path = out_dir / "audit_log.jsonl"
    assert audit_path.exists()
    content = audit_path.read_text(encoding="utf-8")
    assert "override_hold" in content
    assert "Dao Vu Ngoc Anh" in content
    assert "f01372" in content


def test_t04_shock_clock_reset_recovery(tmp_path):
    """Verifies that mid-stream camera clock reset in shock/ is repaired seamlessly."""
    out_dir = tmp_path / "shock_test"
    rc = runner.main([
        "--track", "t04",
        "--input", "fixtures/t04_sensor_sync/shock",
        "--output", str(out_dir),
        "--offline",
        "--seed", "42",
    ])
    assert rc == 0

    pred = pd.read_csv(out_dir / "pairs.csv")
    assert len(pred) == 1200
    assert pred["imu_t_ms"].notna().all()
    assert pred["imu_t_ms"].is_monotonic_increasing

    audit_text = (out_dir / "audit_log.jsonl").read_text(encoding="utf-8")
    assert "clock_reset_repaired" in audit_text
    assert "f00750" in audit_text

