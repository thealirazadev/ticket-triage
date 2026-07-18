"""Eval runner: metric math, confusion, baseline gate, and CLI exit codes."""

import json

import pytest

from app.config import get_settings
from evals import run

# --- pure metric math ---------------------------------------------------------


def test_field_metrics_perfect():
    pairs = [("billing", "billing"), ("bug", "bug"), ("refund", "refund")]
    result = run.field_metrics(pairs, ("billing", "bug", "refund", "other"))
    assert result["accuracy"] == 1.0
    assert result["macro_f1"] == 1.0
    assert result["labels"]["billing"]["precision"] == 1.0


def test_field_metrics_hand_computed():
    # gold vs pred: one billing mislabeled as refund.
    pairs = [
        ("billing", "billing"),
        ("billing", "refund"),
        ("refund", "refund"),
        ("bug", "bug"),
    ]
    result = run.field_metrics(pairs, ("billing", "bug", "refund"))
    assert result["accuracy"] == 0.75  # 3 of 4 correct
    billing = result["labels"]["billing"]
    assert billing["precision"] == 1.0  # 1 tp, 0 fp
    assert billing["recall"] == 0.5  # 1 tp, 1 fn
    assert billing["f1"] == pytest.approx(2 / 3)
    refund = result["labels"]["refund"]
    assert refund["precision"] == 0.5  # 1 tp, 1 fp
    assert refund["recall"] == 1.0


def test_field_metrics_failure_counts_as_wrong():
    pairs = [("billing", None), ("bug", "bug")]
    result = run.field_metrics(pairs, ("billing", "bug"))
    assert result["accuracy"] == 0.5
    assert result["labels"]["billing"]["recall"] == 0.0


def test_confusion_top_orders_by_frequency():
    pairs = [
        ("refund", "billing"),
        ("refund", "billing"),
        ("how_to", "other"),
        ("bug", "bug"),
    ]
    top = run.confusion_top(pairs)
    assert top[0] == (("refund", "billing"), 2)
    assert (("how_to", "other"), 1) in top


# --- baseline comparison ------------------------------------------------------


def _metrics(intent_acc, intent_f1):
    return {
        "intent": {"accuracy": intent_acc, "macro_f1": intent_f1},
        "priority": {"accuracy": 0.9, "macro_f1": 0.9},
        "sentiment": {"accuracy": 0.9, "macro_f1": 0.9},
    }


def _baseline():
    return {
        "metrics": {
            "intent": {"accuracy": 0.9, "macro_f1": 0.9},
            "priority": {"accuracy": 0.9, "macro_f1": 0.9},
            "sentiment": {"accuracy": 0.9, "macro_f1": 0.9},
        },
        "parse_failure_rate": 0.0,
    }


def test_compare_baseline_within_threshold_passes():
    regressed, _, _ = run.compare_baseline(_metrics(0.89, 0.89), _baseline(), 0.02, 0.0)
    assert regressed is False


def test_compare_baseline_regression_fails():
    regressed, label, delta = run.compare_baseline(_metrics(0.80, 0.80), _baseline(), 0.02, 0.0)
    assert regressed is True
    assert "intent" in label
    assert delta < -0.02


def test_compare_baseline_parse_failure_increase_fails():
    regressed, label, _ = run.compare_baseline(_metrics(0.9, 0.9), _baseline(), 0.02, 0.2)
    assert regressed is True
    assert label == "parse_failure_rate"


# --- CLI exit codes (offline via fixtures) ------------------------------------


@pytest.fixture
def eval_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "example-model-id")
    monkeypatch.setenv("EVAL_REGRESSION_THRESHOLD", "0.02")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_main_passes_against_committed_baseline(eval_env, capsys):
    code = run.main(["--fixtures", "evals/fixtures.jsonl"])
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict: PASS" in out


def test_main_missing_key_without_fixtures_exits_2(eval_env, capsys):
    code = run.main([])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error:")


def test_main_limit_skips_gate(eval_env, capsys):
    code = run.main(["--fixtures", "evals/fixtures.jsonl", "--limit", "5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "gate skipped" in out


def test_main_regression_exits_1(eval_env, tmp_path, capsys):
    bad = tmp_path / "bad_fixtures.jsonl"
    with open("evals/fixtures.jsonl") as src, open(bad, "w") as dst:
        for line in src:
            obj = json.loads(line)
            content = json.loads(obj["content"])
            content["intent"] = "other"  # wreck intent accuracy
            obj["content"] = json.dumps(content)
            dst.write(json.dumps(obj) + "\n")
    code = run.main(["--fixtures", str(bad)])
    out = capsys.readouterr().out
    assert code == 1
    assert "verdict: FAIL" in out


def test_main_dataset_hash_mismatch_exits_2(eval_env, tmp_path, capsys):
    modified = tmp_path / "dataset.jsonl"
    with open("evals/dataset.jsonl") as src:
        lines = src.readlines()
    obj = json.loads(lines[0])
    obj["subject"] = "a different subject that changes the hash"
    lines[0] = json.dumps(obj) + "\n"
    modified.write_text("".join(lines))
    code = run.main(["--fixtures", "evals/fixtures.jsonl", "--dataset", str(modified)])
    err = capsys.readouterr().err
    assert code == 2
    assert "does not match baseline" in err


def test_main_update_baseline_writes_file(eval_env, tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    code = run.main(
        ["--fixtures", "evals/fixtures.jsonl", "--baseline", str(baseline), "--update-baseline"]
    )
    assert code == 0
    written = json.loads(baseline.read_text())
    assert written["prompt_version"] == "1"
    assert set(written["metrics"]) == {"intent", "priority", "sentiment"}
    assert "dataset_sha256" in written
