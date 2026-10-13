"""Tests for cloud/template.yaml — T12.5 (Addendum §11.5, rules C2 and A3).

Parses the SAM template offline with a CloudFormation-tag-aware YAML loader.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from cloud.common import POOLS
from src.classifier.archetype_classifier import add_proxy_features
from src.evaluation.latency_model import service_seconds
from src.trace_gen.generator import _default_config, generate_trace

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "cloud" / "template.yaml"
PROJECT_TAG = ("project", "semantic-autoscaling")


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that turns !Ref / !GetAtt / !Sub ... into their JSON-form dicts."""


def _cfn_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    return {name: value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=CfnLoader)  # noqa: S506 (SafeLoader subclass)


def _resources(template: dict[str, Any], type_: str) -> dict[str, dict[str, Any]]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == type_}


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _has_project_tag(tags: Any) -> bool:
    if isinstance(tags, dict):  # SAM map form
        return tags.get(PROJECT_TAG[0]) == PROJECT_TAG[1]
    if isinstance(tags, list):  # CloudFormation list form
        return {"Key": PROJECT_TAG[0], "Value": PROJECT_TAG[1]} in tags
    return False


# --------------------------------------------------------------------------------
# The five assertions the task specifies
# --------------------------------------------------------------------------------


def test_every_function_is_arm64(template: dict[str, Any]) -> None:
    globals_arch = template["Globals"]["Function"]["Architectures"]
    functions = _resources(template, "AWS::Serverless::Function")
    assert len(functions) == 7  # router, controller, 5 workers
    for name, resource in functions.items():
        assert resource["Properties"].get("Architectures", globals_arch) == ["arm64"], name


TAGGABLE = {
    "AWS::Serverless::Function",
    "AWS::Serverless::HttpApi",
    "AWS::SQS::Queue",
    "AWS::DynamoDB::Table",
    "AWS::SNS::Topic",
    "AWS::Lambda::EventSourceMapping",
}
NOT_TAGGABLE = {"AWS::SNS::Subscription", "AWS::CloudWatch::Dashboard"}  # no Tags property


def test_every_taggable_resource_carries_the_project_tag(template: dict[str, Any]) -> None:
    globals_tags = template["Globals"]["Function"]["Tags"]
    assert _has_project_tag(globals_tags)
    assert template["Globals"]["Function"]["PropagateTags"] is True  # also tags SAM-generated roles
    for name, resource in template["Resources"].items():
        type_ = resource["Type"]
        assert type_ in TAGGABLE | NOT_TAGGABLE, f"{name}: decide whether {type_} is taggable"
        if type_ not in TAGGABLE:
            continue
        tags = resource["Properties"].get("Tags", globals_tags if type_ == "AWS::Serverless::Function" else None)
        assert _has_project_tag(tags), f"{name} ({type_}) is missing project=semantic-autoscaling"


def test_no_reserved_concurrency_anywhere(template: dict[str, Any]) -> None:
    keys = {key for key, _ in _walk(template)}
    assert "ReservedConcurrentExecutions" not in keys
    assert "ProvisionedConcurrencyConfig" not in keys
    assert "ProvisionedPollerConfig" not in keys  # also mutually exclusive with ScalingConfig


def test_every_mapping_starts_at_batch_one_and_two_slots(template: dict[str, Any]) -> None:
    mappings = _resources(template, "AWS::Lambda::EventSourceMapping")
    assert len(mappings) == 5
    for name, resource in mappings.items():
        props = resource["Properties"]
        assert props["BatchSize"] == 1, name
        assert props["ScalingConfig"] == {"MaximumConcurrency": 2}, name
        # Without this the worker's batchItemFailures are ignored and failed messages deleted.
        assert props["FunctionResponseTypes"] == ["ReportBatchItemFailures"], name


def test_no_ec2_sagemaker_eks_or_nat_resources(template: dict[str, Any]) -> None:
    for name, resource in template["Resources"].items():
        type_ = resource["Type"]
        assert not type_.startswith(("AWS::EC2::", "AWS::SageMaker::", "AWS::EKS::")), f"{name}: {type_} (rule C2)"
        assert "Nat" not in type_, f"{name}: {type_} (rule C2)"


# --------------------------------------------------------------------------------
# Drift guards: the template must stay consistent with the code and the data
# --------------------------------------------------------------------------------


def test_worker_timeout_follows_the_trace_and_queues_hold_six_times_it(template: dict[str, Any]) -> None:
    cfg = _default_config()
    max_service_s = float(service_seconds(add_proxy_features(generate_trace(cfg), seed=cfg.seed)).max())
    expected_timeout = math.ceil(max_service_s) + 10

    workers = {n: r for n, r in _resources(template, "AWS::Serverless::Function").items() if n.endswith("Worker")}
    queues = _resources(template, "AWS::SQS::Queue")
    assert len(workers) == len(queues) == 5
    for name, worker in workers.items():
        props = worker["Properties"]
        assert props["Timeout"] == expected_timeout, f"{name}: ceil({max_service_s:.4f}) + 10"
        assert props["Runtime"] == "python3.14" and props["MemorySize"] == 128 and props["Handler"] == "worker.handler"
    for name, queue in queues.items():
        assert queue["Properties"]["VisibilityTimeout"] >= 6 * expected_timeout, name


def test_one_queue_worker_and_mapping_per_pool(template: dict[str, Any]) -> None:
    queue_names = {r["Properties"]["QueueName"] for r in _resources(template, "AWS::SQS::Queue").values()}
    assert queue_names == {f"semauto-{pool}" for pool in POOLS}
    for mapping in _resources(template, "AWS::Lambda::EventSourceMapping").values():
        queue_ref = mapping["Properties"]["EventSourceArn"]["Fn::GetAtt"][0]
        worker_ref = mapping["Properties"]["FunctionName"]["Ref"]
        assert queue_ref.removesuffix("Queue") == worker_ref.removesuffix("Worker")


def test_router_and_controller_get_every_env_var_load_settings_needs(template: dict[str, Any]) -> None:
    globals_env = template["Globals"]["Function"]["Environment"]["Variables"]
    functions = _resources(template, "AWS::Serverless::Function")
    required = {"COUNTS_TABLE", "DECISIONS_TABLE", "RESULTS_TABLE", "TIME_SCALE", "SLOTS_PER_UNIT", "ENFORCE_GOVERNANCE"}
    required |= {f"QUEUE_URL_{p.upper()}" for p in POOLS} | {f"ESM_UUID_{p.upper()}" for p in POOLS}
    for name in ("RouterFunction", "ControllerFunction"):
        env = set(globals_env) | set(functions[name]["Properties"]["Environment"]["Variables"])
        assert required <= env, f"{name} missing {sorted(required - env)}"
    assert "SNS_TOPIC_ARN" in functions["ControllerFunction"]["Properties"]["Environment"]["Variables"]


def test_router_and_controller_share_one_image_with_different_commands(template: dict[str, Any]) -> None:
    functions = _resources(template, "AWS::Serverless::Function")
    router, controller = functions["RouterFunction"], functions["ControllerFunction"]
    assert router["Metadata"] == controller["Metadata"] == {
        "Dockerfile": "cloud/Dockerfile", "DockerContext": "..", "DockerTag": "python3.14-v1",
    }
    assert router["Properties"]["PackageType"] == controller["Properties"]["PackageType"] == "Image"
    assert controller["Properties"]["ImageConfig"]["Command"] == ["cloud.handlers.controller.handler"]
    assert (router["Properties"]["MemorySize"], router["Properties"]["Timeout"]) == (1024, 10)
    assert (controller["Properties"]["MemorySize"], controller["Properties"]["Timeout"]) == (1024, 60)
    assert controller["Properties"]["Events"]["LiveTick"]["Properties"]["State"] == "DISABLED"
    assert "Events" not in controller["Properties"] or all(
        e["Type"] != "HttpApi" for e in controller["Properties"]["Events"].values()
    ), "the controller must have no API route"


def test_tables_are_on_demand_with_ttl_and_the_section_11_3_keys(template: dict[str, Any]) -> None:
    tables = {r["Properties"]["TableName"]: r["Properties"] for r in _resources(template, "AWS::DynamoDB::Table").values()}
    expected_sort_key = {"semauto-counts": "minute", "semauto-decisions": "minute", "semauto-results": "request_id"}
    assert set(tables) == set(expected_sort_key)
    for name, props in tables.items():
        assert props["BillingMode"] == "PAY_PER_REQUEST" and "ProvisionedThroughput" not in props, name
        assert props["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}, name
        assert props["KeySchema"] == [
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": expected_sort_key[name], "KeyType": "RANGE"},
        ], name


def test_parameters_and_outputs(template: dict[str, Any]) -> None:
    params = template["Parameters"]
    assert "Default" not in params["AlertEmail"]
    assert (params["TimeScale"]["Default"], params["SlotsPerUnit"]["Default"]) == (1, 2)
    assert params["EnforceGovernance"]["Default"] == "false"
    expected_outputs = {"ApiUrl", "ControllerFunctionName", "CountsTableName", "DecisionsTableName", "ResultsTableName"}
    expected_outputs |= {
        "BedrockOnDemandQueueUrl", "SagemakerEndpointQueueUrl", "EksGpuReservedQueueUrl",
        "EksGpuSpotQueueUrl", "HomogeneousPoolQueueUrl",
    }
    assert set(template["Outputs"]) == expected_outputs


def test_email_subscription_is_a_standalone_resource_so_teardown_removes_it(template: dict[str, Any]) -> None:
    """Inline AWS::SNS::Topic subscriptions survive stack deletion (CloudFormation docs)."""
    topic = _resources(template, "AWS::SNS::Topic")["AlertTopic"]
    assert "Subscription" not in topic["Properties"]
    subscription = _resources(template, "AWS::SNS::Subscription")["AlertSubscription"]["Properties"]
    assert subscription == {"TopicArn": {"Ref": "AlertTopic"}, "Protocol": "email", "Endpoint": {"Ref": "AlertEmail"}}


def test_dashboard_body_is_valid_json_with_all_required_widgets(template: dict[str, Any]) -> None:
    body = template["Resources"]["Dashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
    widgets = json.loads(re.sub(r"\$\{[^}]+\}", "X", body))["widgets"]
    titles = " | ".join(w["properties"]["title"] for w in widgets)
    for needle in ("Requests per predicted archetype", "Forecast peak", "Capacity units", "spend vs budget",
                   "ApproximateNumberOfMessagesVisible", "ApproximateAgeOfOldestMessage", "ConcurrentExecutions"):
        assert needle in titles, needle
    stacked = [w for w in widgets if "Requests per predicted archetype" in w["properties"]["title"]][0]
    assert stacked["properties"]["stacked"] is True


def test_no_account_ids_or_emails_are_committed() -> None:
    """Rule A3 / NFR-A6."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    samconfig = (TEMPLATE_PATH.parent / "samconfig.toml").read_text(encoding="utf-8")
    for content in (text, samconfig):
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", content), "looks like an AWS account ID"
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", content), "looks like an email address"
