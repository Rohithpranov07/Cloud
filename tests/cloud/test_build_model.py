"""Tests for the cloud/ image foundations — T12.1 (Addendum §11.2, NFR-A4, NFR-A5).

Offline: no network, no AWS credentials, no real AWS calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import pytest

from cloud import common
from cloud.build_model import build_model
from src.classifier.archetype_classifier import add_proxy_features, classify, train_classifier
from src.config import REPO_ROOT
from src.trace_gen.generator import _default_config, generate_trace

# --------------------------------------------------------------------------------
# build_model — the image's classifier must equal the simulation's (seed 42)
# --------------------------------------------------------------------------------


def test_build_model_predictions_equal_the_simulation_classifier(tmp_path: Path) -> None:
    """100% agreement with classify() using the seed-42 features and seed-42 model.

    That is the model run_comparison.simulate() trains before any recalibration
    (simulate(recalibrate=False) arrives in T12.4; this is the documented fallback).
    """
    model_path = tmp_path / "model" / "classifier.joblib"
    build_model(model_path)
    assert model_path.is_file()

    cfg = _default_config()
    assert cfg.seed == 42
    featured = add_proxy_features(generate_trace(cfg), seed=cfg.seed)
    expected = classify(featured, train_classifier(featured, seed=cfg.seed))["predicted_archetype"]
    actual = classify(featured, joblib.load(model_path))["predicted_archetype"]

    agreement = float((actual.to_numpy() == expected.to_numpy()).mean())
    assert agreement == 1.0, f"image classifier agrees with the simulation on {agreement:.2%} only"


def test_build_model_is_deterministic(tmp_path: Path) -> None:
    first = build_model(tmp_path / "a.joblib")
    second = build_model(tmp_path / "b.joblib")
    assert first.holdout_accuracy_ == second.holdout_accuracy_
    assert first.holdout_accuracy_ > 0.90  # TRD §6 classifier gate still holds


# --------------------------------------------------------------------------------
# requirements-lambda.txt — same pins as requirements.txt, nothing extra
# --------------------------------------------------------------------------------


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, version = stripped.split("==")
        pins[name.lower()] = version
    return pins


def test_lambda_requirements_match_the_repo_pins_exactly() -> None:
    lambda_pins = _pins(REPO_ROOT / "cloud" / "requirements-lambda.txt")
    repo_pins = _pins(REPO_ROOT / "requirements.txt")
    cloud_pins = _pins(REPO_ROOT / "requirements-cloud.txt")

    runtime = ("numpy", "pandas", "scipy", "scikit-learn", "statsmodels", "pyyaml")
    assert set(lambda_pins) == {*runtime, "boto3"}
    for name in runtime:
        assert lambda_pins[name] == repo_pins[name], f"{name} pin drifted from requirements.txt"
    assert lambda_pins["boto3"] == cloud_pins["boto3"], "boto3 pin drifted from requirements-cloud.txt"


def test_lambda_requirements_exclude_dev_and_plotting_tools() -> None:
    lambda_pins = _pins(REPO_ROOT / "cloud" / "requirements-lambda.txt")
    assert not {"matplotlib", "pytest", "mypy"} & set(lambda_pins)


# --------------------------------------------------------------------------------
# emit_emf — one valid EMF line per call
# --------------------------------------------------------------------------------


def test_emit_emf_writes_one_valid_emf_document() -> None:
    lines: list[str] = []
    document = common.emit_emf(
        "SemAuto",
        {"policy": "aware", "predicted_archetype": "agentic_tool_using"},
        {"Requests": 1},
        units={"Requests": "Count"},
        now=lambda: 1_700_000_000.123,
        write=lines.append,
    )

    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == document
    directive = parsed["_aws"]["CloudWatchMetrics"][0]
    assert parsed["_aws"]["Timestamp"] == 1_700_000_000_123  # epoch milliseconds
    assert directive["Namespace"] == "SemAuto"
    assert directive["Dimensions"] == [["policy", "predicted_archetype"]]
    assert directive["Metrics"] == [{"Name": "Requests", "Unit": "Count"}]
    # Dimension and metric values live on the root node, per the spec.
    assert parsed["policy"] == "aware"
    assert parsed["Requests"] == 1.0


def test_emit_emf_unit_defaults_to_none() -> None:
    document = common.emit_emf("SemAuto", {"pool": "p"}, {"Units": 3}, write=lambda _: None)
    assert document["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "Units", "Unit": "None"}]


@pytest.mark.parametrize(
    ("dimensions", "metrics", "match"),
    [
        ({}, {"Requests": 1}, "at least one dimension"),
        ({"pool": "p"}, {}, "metrics"),
        ({"pool": 3}, {"Requests": 1}, "non-empty string"),
        ({"pool": "p"}, {"Requests": float("nan")}, "finite"),
        ({"Requests": "x"}, {"Requests": 1}, "both dimension and metric"),
        ({f"d{i}": "v" for i in range(31)}, {"Requests": 1}, "at most 30"),
    ],
)
def test_emit_emf_rejects_documents_cloudwatch_would_drop(
    dimensions: dict, metrics: dict, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        common.emit_emf("SemAuto", dimensions, metrics, write=lambda _: None)


# --------------------------------------------------------------------------------
# load_settings — environment loading
# --------------------------------------------------------------------------------


def _full_env() -> dict[str, str]:
    env = {
        "COUNTS_TABLE": "semauto-counts",
        "DECISIONS_TABLE": "semauto-decisions",
        "RESULTS_TABLE": "semauto-results",
    }
    for pool in common.POOLS:
        env[f"QUEUE_URL_{pool.upper()}"] = f"https://sqs.example/{pool}"
        env[f"ESM_UUID_{pool.upper()}"] = f"uuid-{pool}"
    return env


def test_load_settings_reads_every_pool_and_applies_template_defaults() -> None:
    settings = common.load_settings(_full_env())
    assert set(settings.queue_urls) == set(common.POOLS) == set(settings.esm_uuids)
    assert len(common.POOLS) == 5
    assert settings.slots_per_unit == 2
    assert settings.time_scale == 1.0
    assert settings.enforce_governance is False


def test_load_settings_parses_overrides() -> None:
    env = _full_env() | {"SLOTS_PER_UNIT": "4", "TIME_SCALE": "2.5", "ENFORCE_GOVERNANCE": "TRUE"}
    settings = common.load_settings(env)
    assert (settings.slots_per_unit, settings.time_scale, settings.enforce_governance) == (4, 2.5, True)


def test_load_settings_fails_loudly_on_a_missing_variable() -> None:
    env = _full_env()
    del env["QUEUE_URL_EKS_GPU_RESERVED"]
    with pytest.raises(KeyError, match="QUEUE_URL_EKS_GPU_RESERVED"):
        common.load_settings(env)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"SLOTS_PER_UNIT": "1"}, "SLOTS_PER_UNIT"),
        ({"TIME_SCALE": "0"}, "TIME_SCALE"),
        ({"ENFORCE_GOVERNANCE": "yes"}, "ENFORCE_GOVERNANCE"),
    ],
)
def test_load_settings_rejects_invalid_values(override: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        common.load_settings(_full_env() | override)


# --------------------------------------------------------------------------------
# client() — lazy and cached, with no network access at creation time
# --------------------------------------------------------------------------------


def test_client_is_created_once_per_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    common.client.cache_clear()
    try:
        assert common.client("sqs") is common.client("sqs")
        assert common.client("sqs") is not common.client("dynamodb")
    finally:
        common.client.cache_clear()
