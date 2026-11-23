import copy

import pytest
from pydantic import ValidationError

from schema import Plan

VALID_PLAN = {
    "date": "2026-07-01",
    "generated_at": "2026-07-01T08:02:11-04:00",
    "bias": "bullish",
    "spx_reference": 7465.0,
    "triggers": [
        {
            "id": "primary_long",
            "side": "long",
            "condition": {
                "price_between": [7465.0, 7472.0],
                "not_after": "15:00",
                "requires_not_filled": [],
            },
            "action": {"instrument": "SPX 0DTE 7495C", "size_pct": 50},
            "targets": [
                {"level": 7498.0, "scale_out_pct": 40},
                {"level": 7515.0, "scale_out_pct": 35},
                {"level": 7527.0, "scale_out_pct": 25},
            ],
            "stop": 7450.0,
            "one_shot": True,
        },
        {
            "id": "fade_extension",
            "side": "short",
            "condition": {
                "price_between": [7497.0, 7512.0],
                "requires_not_filled": ["primary_long"],
                "not_after": "15:00",
            },
            "action": {"instrument": "SPX 0DTE 7480P", "size_pct": 50},
            "targets": [
                {"level": 7470.0, "scale_out_pct": 60},
                {"level": 7455.0, "scale_out_pct": 40},
            ],
            "stop": 7525.0,
            "one_shot": True,
        },
    ],
    "invalidation": {"below": 7408.0, "action": "flatten_and_halt"},
    "flat_by": "15:00",
    "scheduled_events": [
        {
            "time": "10:00",
            "release": "ISM Manufacturing PMI",
            "source_id": "finnhub:ism-mfg",
            "replan": True,
        }
    ],
    "notes": "free-text rationale, not machine-read",
}


def make_plan(**overrides):
    data = copy.deepcopy(VALID_PLAN)
    data.update(overrides)
    return data


def test_valid_plan_parses():
    plan = Plan.model_validate(VALID_PLAN)
    assert plan.spx_reference == 7465.0
    assert len(plan.triggers) == 2


def test_stop_on_wrong_side_for_long_rejected():
    data = copy.deepcopy(VALID_PLAN)
    # long trigger's stop must be below the zone low (7465); put it above.
    data["triggers"][0]["stop"] = 7480.0
    with pytest.raises(ValidationError, match="stop"):
        Plan.model_validate(data)


def test_stop_on_wrong_side_for_short_rejected():
    data = copy.deepcopy(VALID_PLAN)
    # short trigger's stop must be above the zone high (7512); put it below.
    data["triggers"][1]["stop"] = 7500.0
    with pytest.raises(ValidationError, match="stop"):
        Plan.model_validate(data)


def test_scale_out_pct_not_summing_to_100_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][0]["targets"] = [
        {"level": 7498.0, "scale_out_pct": 40},
        {"level": 7515.0, "scale_out_pct": 40},
    ]
    with pytest.raises(ValidationError, match="scale_out_pct"):
        Plan.model_validate(data)


def test_scale_out_pct_within_tolerance_accepted():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][0]["targets"] = [
        {"level": 7498.0, "scale_out_pct": 50.5},
        {"level": 7515.0, "scale_out_pct": 30},
        {"level": 7527.0, "scale_out_pct": 20},
    ]
    plan = Plan.model_validate(data)
    assert plan.triggers[0].targets[0].scale_out_pct == 50.5


def test_dangling_requires_not_filled_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][1]["condition"]["requires_not_filled"] = ["does_not_exist"]
    with pytest.raises(ValidationError, match="requires_not_filled"):
        Plan.model_validate(data)


def test_requires_not_filled_self_reference_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][0]["condition"]["requires_not_filled"] = ["primary_long"]
    with pytest.raises(ValidationError, match="itself"):
        Plan.model_validate(data)


def test_duplicate_trigger_ids_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][1]["id"] = "primary_long"
    with pytest.raises(ValidationError, match="duplicate"):
        Plan.model_validate(data)


def test_price_between_low_not_less_than_high_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["triggers"][0]["condition"]["price_between"] = [7472.0, 7465.0]
    with pytest.raises(ValidationError, match="price_between"):
        Plan.model_validate(data)


def test_invalid_hhmm_rejected():
    data = copy.deepcopy(VALID_PLAN)
    data["flat_by"] = "3pm"
    with pytest.raises(ValidationError, match="HH:MM"):
        Plan.model_validate(data)


def test_invalidation_requires_at_least_one_bound():
    data = copy.deepcopy(VALID_PLAN)
    data["invalidation"] = {"action": "flatten_and_halt"}
    with pytest.raises(ValidationError):
        Plan.model_validate(data)
