# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for VKG reload Lambda handler."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda" / "vkg-reload"))

ENV = {
    "CLUSTER_ARN": "arn:aws:ecs:us-west-2:111:cluster/vkg",
    "RESOURCE_PREFIX": "coa-dev",
    "CLOUD_MAP_NAMESPACE_ID": "ns-abc123",
    "VKG_TASK_ROLE_ARN": "arn:aws:iam::111:role/task",
    "VKG_EXECUTION_ROLE_ARN": "arn:aws:iam::111:role/exec",
    "ONTOLOGY_BUCKET": "ontology-bucket",
    "PRIVATE_SUBNET_IDS": "subnet-a,subnet-b",
    "ECS_SECURITY_GROUP_ID": "sg-123",
    "AWS_REGION": "us-west-2",
    "VKG_IMAGE_PARAM_NAME": "/coa/vkg/container-image",
    # Set by the Lambda runtime; the sweep invokes this function by it.
    "AWS_LAMBDA_FUNCTION_NAME": "coa-dev-vkg-reload",
}


def _make_event(namespace="test-ns", version="v1"):
    return {"detail": {"namespace": namespace, "version": version}}


# Depth-1 files in ``latest/`` after a deploy, and what ``clear_latest`` leaves behind.
SERVABLE_LATEST = ("ontology.ttl", "mappings.r2rml", "schema.sql", "examples.json")
EMPTIED_LATEST = ("examples.json",)


def _answer_latest(mock_s3, by_namespace=None, default=SERVABLE_LATEST):
    """Make the S3 listing of ``ontologies/{ns}/latest/`` return ``by_namespace[ns]`` (else ``default``)."""
    by_namespace = by_namespace or {}

    def paginate(Bucket, Prefix, Delimiter):
        ns = Prefix.split("/")[1]
        return [{"Contents": [{"Key": Prefix + name} for name in by_namespace.get(ns, default)]}]

    mock_s3.get_paginator.return_value.paginate.side_effect = paginate


def _service(desired, status="ACTIVE"):
    return {"services": [{"status": status, "desiredCount": desired, "taskDefinition": "arn:td:current"}]}


class _StatefulVkg:
    """S3 + ECS fakes with state: ``describe_services`` reflects the last ``update_service``.

    ``before_update`` hooks run one per ``update_service`` call, before that call lands,
    to model another writer (a deploy, a delete, or a whole concurrent invocation)
    finishing in between.
    """

    def __init__(self, mock_ecs, mock_s3, latest, desired, before_update=()):
        self.latest, self.desired = latest, desired
        self.before_update = list(before_update)
        mock_ecs.describe_services.side_effect = lambda cluster, services: _service(self.desired)
        mock_ecs.update_service.side_effect = self._update_service
        mock_s3.get_paginator.return_value.paginate.side_effect = self._list_latest

    def _list_latest(self, Bucket, Prefix, Delimiter):
        return [{"Contents": [{"Key": Prefix + name} for name in self.latest]}]

    def _update_service(self, **kwargs):
        if self.before_update:
            self.before_update.pop(0)(self)
        if "desiredCount" in kwargs:
            self.desired = kwargs["desiredCount"]
        return {"service": {"deployments": [{"id": "d"}]}}


def _lands(latest):
    """A ``before_update`` hook: another writer replaces ``latest/`` meanwhile."""
    return lambda vkg: setattr(vkg, "latest", latest)


def _vkg_service_pages(*pages):
    """A ListServices paginator with one page of service ARNs per tuple of service names."""
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"serviceArns": [f"arn:aws:ecs:us-west-2:111:service/vkg/{name}" for name in page]} for page in pages
    ]
    return paginator


def _dispatched(index):
    """``[(FunctionName, InvocationType, payload)]`` of every Invoke the handler made."""
    return [
        (c.kwargs["FunctionName"], c.kwargs["InvocationType"], json.loads(c.kwargs["Payload"]))
        for c in index.lambda_client.invoke.call_args_list
    ]


def _reload_failed_for(index):
    """Namespaces that got a dimensioned ReloadFailed, in emit order."""
    return [
        entry["Dimensions"][0]["Value"]
        for c in index.cloudwatch.put_metric_data.call_args_list
        for entry in c.kwargs["MetricData"]
        if entry["MetricName"] == "ReloadFailed" and entry.get("Dimensions")
    ]


def _scaling_trace(calls):
    """``[(call name, MinCapacity or desiredCount)]`` in call order."""
    return [(name, kw.get("MinCapacity", kw.get("desiredCount"))) for name, _, kw in calls.mock_calls]


@pytest.fixture(autouse=True)
def mock_s3(_mock_metric_and_autoscaling_clients, monkeypatch):
    """The S3 client the handler gets on ``importlib.reload(index)``.

    ``latest/`` holds a loadable ontology + mappings unless a test says otherwise,
    which is the state every plain reload test assumes. Depends on the conftest
    fixture so this wraps its boto3 patch rather than being replaced by it.
    """
    s3 = MagicMock()
    _answer_latest(s3)
    real_client = boto3.client
    monkeypatch.setattr(boto3, "client", lambda name, *a, **kw: s3 if name == "s3" else real_client(name, *a, **kw))
    return s3


class TestHandler:
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_with_new_image_registers_task_def(self, mock_ssm, mock_sd, mock_ecs):
        """When SSM image differs from current, register new task def before deploying."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {
            "services": [{"status": "ACTIVE", "desiredCount": 2, "taskDefinition": "arn:td:old"}]
        }
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        mock_ecs.register_task_definition.assert_called_once()
        # A running service keeps its auto-scaled count: no desiredCount override.
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-test-ns",
            taskDefinition="arn:td:ns-rev2",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_same_image_skips_task_def_registration(self, mock_ssm, mock_sd, mock_ecs):
        """When SSM image matches current, just forceNewDeployment without new task def."""
        same_image = "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-same"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        mock_ecs.describe_services.return_value = {
            "services": [{"status": "ACTIVE", "desiredCount": 1, "taskDefinition": "arn:td:current"}]
        }
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        mock_ecs.register_task_definition.assert_not_called()
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-test-ns",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    # ── serving follows the VKG files in latest/, not the event's version ────

    @pytest.mark.parametrize(
        ("names", "servable"),
        [
            pytest.param(SERVABLE_LATEST, True, id="deployed"),
            pytest.param(("ontology.owl", "mappings.obda"), True, id="owl-obda"),
            pytest.param(("ontology.ttl", "schema.sql"), False, id="no-mappings"),
            pytest.param(("mappings.r2rml", "schema.sql"), False, id="no-ontology"),
            pytest.param(EMPTIED_LATEST, False, id="cleared"),
            pytest.param((), False, id="empty"),
        ],
    )
    @patch.dict(os.environ, ENV)
    def test_latest_servable_mirrors_the_vkg_entrypoint(self, mock_s3, names, servable):
        """Servable means what entrypoint.sh loads: a depth-1 *.ttl/*.owl AND a
        *.obda/*.r2rml. A non-empty prefix is not enough: clear_latest keeps examples.json."""
        import importlib

        import index

        importlib.reload(index)
        _answer_latest(mock_s3, default=names)

        assert index._latest_servable("test-ns") is servable
        mock_s3.get_paginator.assert_called_once_with("list_objects_v2")
        mock_s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket=ENV["ONTOLOGY_BUCKET"], Prefix="ontologies/test-ns/latest/", Delimiter="/"
        )

    @pytest.mark.parametrize(
        ("describe_response", "reason"),
        [
            pytest.param({"services": [], "failures": [{"reason": "MISSING"}]}, "nothing to serve", id="missing"),
            pytest.param(_service(0, status="INACTIVE"), "nothing to serve", id="inactive"),
            pytest.param(_service(0, status="DRAINING"), "nothing to serve", id="draining"),
            pytest.param(_service(0), "already scaled to zero", id="active-at-zero"),
        ],
    )
    @pytest.mark.parametrize("version", [None, "v3"], ids=["null-event", "real-event"])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_unservable_latest_never_provisions_or_restores(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3, version, describe_response, reason
    ):
        """With no loadable files in latest/ (domain ontology deleted) there is nothing
        to serve, whatever the event's version: a missing, inactive or draining service
        is never provisioned (that would orphan it, e.g. racing a namespace delete), and
        a service already at zero stays there."""
        _answer_latest(mock_s3, default=EMPTIED_LATEST)
        mock_ecs.describe_services.return_value = describe_response

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version=version), None)

        assert result == {"status": "skipped", "namespace": "test-ns", "reason": reason}
        mock_ecs.update_service.assert_not_called()
        mock_ecs.create_service.assert_not_called()
        mock_ecs.register_task_definition.assert_not_called()
        mock_sd.create_service.assert_not_called()
        mock_autoscaling.register_scalable_target.assert_not_called()
        mock_autoscaling.put_scaling_policy.assert_not_called()

    @pytest.mark.parametrize("version", [None, "v3"], ids=["null-event", "real-event"])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_unservable_latest_scales_running_service_to_zero(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3, version
    ):
        """A redeploy from the emptied latest/ would boot a task that fails /health and
        rolls back, leaving the old task serving the deleted ontology. Instead lower
        MinCapacity to 0 (so auto scaling cannot undo it), then scale to zero. A late
        or retried real event (deploy, then delete) must not undo that."""
        _answer_latest(mock_s3, default=EMPTIED_LATEST)
        mock_ecs.describe_services.return_value = _service(1)
        calls = MagicMock()
        calls.attach_mock(mock_autoscaling.register_scalable_target, "register_scalable_target")
        calls.attach_mock(mock_ecs.update_service, "update_service")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version=version), None)

        assert result["status"] == "scaled_to_zero"
        assert [c[0] for c in calls.mock_calls] == ["register_scalable_target", "update_service"]
        target = mock_autoscaling.register_scalable_target.call_args.kwargs
        assert target["MinCapacity"] == 0
        assert target["MaxCapacity"] == 3  # same ceiling as provisioning
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"], service="coa-dev-vkg-test-ns", desiredCount=0
        )
        mock_ecs.register_task_definition.assert_not_called()
        mock_ecs.create_service.assert_not_called()
        mock_sd.create_service.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_scale_to_zero_proceeds_when_min_capacity_update_fails(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3
    ):
        """Auto-scaling errors are logged, not raised: with no tasks there is no CPU
        metric, so nothing scales the service back out even at the old MinCapacity."""
        _answer_latest(mock_s3, default=EMPTIED_LATEST)
        mock_ecs.describe_services.return_value = _service(1)
        mock_autoscaling.register_scalable_target.side_effect = RuntimeError("autoscaling API error")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version=None), None)

        assert result["status"] == "scaled_to_zero"
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"], service="coa-dev-vkg-test-ns", desiredCount=0
        )

    @pytest.mark.parametrize("current_image", ["img:same", "img:old"], ids=["image-unchanged", "image-updated"])
    @pytest.mark.parametrize("desired", [1, 0], ids=["running", "at-zero"])
    @pytest.mark.parametrize("version", [None, "v2"], ids=["null-event", "real-event"])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_servable_latest_reloads_and_restores_a_zero_count_service(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3, version, desired, current_image
    ):
        """With loadable files in latest/ the service is reloaded, whatever the event's
        version (a retried null event after the next deploy must not scale it down). A
        service at zero comes back to one task, MinCapacity 1 first; a running one keeps
        its auto-scaled count."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = _service(desired)
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": current_image}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d1"}]}}
        calls = MagicMock()
        calls.attach_mock(mock_autoscaling.register_scalable_target, "register_scalable_target")
        calls.attach_mock(mock_ecs.update_service, "update_service")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version=version), None)

        assert result["status"] == "reload_triggered"
        restored = desired == 0
        expected_calls = ["register_scalable_target", "update_service"] if restored else ["update_service"]
        assert [c[0] for c in calls.mock_calls] == expected_calls
        if restored:
            target = mock_autoscaling.register_scalable_target.call_args.kwargs
            assert (target["MinCapacity"], target["MaxCapacity"]) == (1, 3)
        update = mock_ecs.update_service.call_args.kwargs
        assert update.get("desiredCount") == (1 if restored else None)
        assert update["forceNewDeployment"] is True
        assert update["deploymentConfiguration"] == index._DEPLOY_CONFIG
        assert ("taskDefinition" in update) is (current_image != "img:same")

    @pytest.mark.parametrize(
        ("start", "landing", "trace", "final_desired"),
        [
            pytest.param(
                SERVABLE_LATEST,
                EMPTIED_LATEST,
                [("update_service", None), ("register_scalable_target", 0), ("update_service", 0)],
                0,
                id="deleted-mid-reload",
            ),
            pytest.param(
                EMPTIED_LATEST,
                SERVABLE_LATEST,
                [
                    ("register_scalable_target", 0),
                    ("update_service", 0),
                    ("register_scalable_target", 1),
                    ("update_service", 1),
                ],
                1,
                id="deployed-mid-scale-down",
            ),
        ],
    )
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_latest_changed_while_acting_is_followed(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3, start, landing, trace, final_desired
    ):
        """A deploy or delete can land while this invocation acts (events are async and
        run concurrently). latest/ is read again after every action until it matches the
        last one, so the invocation that writes last also reads last."""
        vkg = _StatefulVkg(mock_ecs, mock_s3, latest=start, desired=1, before_update=[_lands(landing)])
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        calls = MagicMock()
        calls.attach_mock(mock_autoscaling.register_scalable_target, "register_scalable_target")
        calls.attach_mock(mock_ecs.update_service, "update_service")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version="v3"), None)

        assert result["status"] == ("scaled_to_zero" if final_desired == 0 else "reload_triggered")
        # MinCapacity always moves before desiredCount: 0 then 0 down, 1 then 1 up.
        assert _scaling_trace(calls) == trace
        assert vkg.desired == final_desired
        assert mock_s3.get_paginator.return_value.paginate.call_count == 3  # one read per action, plus the first

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_last_writer_reads_latest_after_a_concurrent_delete(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3
    ):
        """X scales down, a deploy lands, and X flips to restoring the service. While X's
        restore is in flight a delete lands and its null event Y runs end to end (already
        at zero: nothing to do). X's desiredCount=1 then lands last, so X must read
        latest/ again after that write and scale back down."""

        def delete_lands_and_y_runs(vkg):
            vkg.latest = EMPTIED_LATEST
            assert index.handler(_make_event(version=None), None)["reason"] == "already scaled to zero"

        vkg = _StatefulVkg(
            mock_ecs,
            mock_s3,
            latest=EMPTIED_LATEST,
            desired=1,
            before_update=[_lands(SERVABLE_LATEST), delete_lands_and_y_runs],
        )
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version="v3"), None)

        assert result["status"] == "scaled_to_zero"
        assert vkg.desired == 0
        assert [c.kwargs.get("desiredCount") for c in mock_ecs.update_service.call_args_list] == [0, 1, 0]

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_latest_that_keeps_changing_fails_for_a_later_retry(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3
    ):
        """The re-read loop is bounded: still flapping after the last round, the reload
        fails with ReloadFailed so the async retry reads latest/ again later."""
        _StatefulVkg(
            mock_ecs,
            mock_s3,
            latest=SERVABLE_LATEST,
            desired=1,
            before_update=[_lands(EMPTIED_LATEST), _lands(SERVABLE_LATEST)] * 2,
        )
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        with pytest.raises(RuntimeError, match="kept changing"):
            index.handler(_make_event(version="v3"), None)

        assert mock_ecs.update_service.call_count == index._MAX_RECONCILE_ROUNDS == 4
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_real_event_on_inactive_zero_count_service_provisions_without_restoring_it(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3
    ):
        """An INACTIVE service (deleted) also reports desiredCount 0, but it is not a
        scaled-to-zero service: no scalable target is registered against it. The deploy
        provisions a fresh service, which configures auto scaling itself."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = _service(0, status="INACTIVE")
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotActiveException("not active")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(version="v2"), None)

        assert result["status"] == "provisioned"
        assert "desiredCount" not in mock_ecs.update_service.call_args.kwargs
        mock_ecs.create_service.assert_called_once()
        mock_autoscaling.register_scalable_target.assert_called_once()  # by provisioning only
        assert mock_autoscaling.register_scalable_target.call_args.kwargs["MinCapacity"] == 1

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_describe_services_failure_fails_the_reload(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3):
        """Without the desired count a zero-count service would be force-deployed and
        stay at zero, so a DescribeServices error fails (and retries) the reload."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.side_effect = RuntimeError("ThrottlingException")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        with pytest.raises(RuntimeError, match="ThrottlingException"):
            index.handler(_make_event(version="v2"), None)

        mock_ecs.update_service.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_s3_listing_failure_fails_without_acting(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3):
        """If latest/ cannot be listed, neither reload nor scale down: emit ReloadFailed
        and raise so the event is retried."""
        mock_s3.get_paginator.return_value.paginate.side_effect = RuntimeError("AccessDenied")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        with pytest.raises(RuntimeError, match="AccessDenied"):
            index.handler(_make_event(version="v2"), None)

        mock_ecs.describe_services.assert_not_called()
        mock_ecs.update_service.assert_not_called()
        mock_autoscaling.register_scalable_target.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_uses_latest_image_from_ssm(self, mock_ssm, mock_sd, mock_ecs):
        """The reload path must read image from SSM, not from a running service."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-new123"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev3"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-2"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        mock_ssm.get_parameter.assert_called_with(Name="/coa/vkg/container-image")
        td_call = mock_ecs.register_task_definition.call_args
        container_defs = td_call[1]["containerDefinitions"]
        assert container_defs[0]["image"] == "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-new123"

    @pytest.mark.parametrize("version", ["v1", None], ids=["real-event", "null-event"])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_when_service_not_found(self, mock_ssm, mock_sd, mock_ecs, version):
        """A missing service is provisioned while latest/ is servable, even for a null event."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-abc"}
        }
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(version=version), None)
        assert result["status"] == "provisioned"
        mock_ecs.create_service.assert_called_once()
        assert mock_ecs.create_service.call_args[1]["serviceName"] == "coa-dev-vkg-test-ns"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_invalid_namespace_rejected(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(namespace="../evil"), None)
        assert result["status"] == "skipped"
        assert "invalid" in result["reason"]
        mock_ecs.update_service.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_missing_namespace_rejected(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"detail": {"version": "v1"}}, None)
        assert result["status"] == "skipped"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_race_condition_handles_not_idempotent(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = [
            mock_ecs.exceptions.ServiceNotFoundException("not found"),
            {"service": {"deployments": [{"id": "deploy-2"}]}},
        ]
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_ecs.create_service.side_effect = mock_ecs.exceptions.InvalidParameterException(
            "Creation of service was not idempotent."
        )
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_service_not_active_triggers_provision(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotActiveException("not active")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"

    @patch.dict(os.environ, {**ENV, "CLOUD_MAP_NAMESPACE_ID": "", "PRIVATE_SUBNET_IDS": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_skipped_when_env_vars_missing(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "skipped"
        assert "env vars" in result["reason"]

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_fails_when_ssm_param_not_configured(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        assert "container image" in result["reason"]

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_provision_configures_auto_scaling(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs):
        """New namespace provision must configure auto-scaling (min=1, max=3, CPU 70%)."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"
        mock_autoscaling.register_scalable_target.assert_called_once()
        target_call = mock_autoscaling.register_scalable_target.call_args[1]
        assert target_call["MinCapacity"] == 1
        assert target_call["MaxCapacity"] == 3
        mock_autoscaling.put_scaling_policy.assert_called_once()
        policy_call = mock_autoscaling.put_scaling_policy.call_args[1]
        assert policy_call["TargetTrackingScalingPolicyConfiguration"]["TargetValue"] == 70.0

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_provision_succeeds_even_if_autoscaling_fails(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs):
        """Auto-scaling failure must not prevent service provisioning."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})
        mock_autoscaling.register_scalable_target.side_effect = Exception("autoscaling API error")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"

    def test_scaling_policy_access_denied_logged_as_warning_with_error_code(self, capsys):
        """A denied PutScalingPolicy must surface as a WARNING carrying the AWS error code."""
        import index
        from botocore.exceptions import ClientError

        mock_autoscaling = MagicMock()
        mock_autoscaling.put_scaling_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
            "PutScalingPolicy",
        )
        with patch.object(index, "autoscaling", mock_autoscaling):
            index._configure_auto_scaling("arn:aws:ecs:us-west-2:111:cluster/vkg", "coa-dev-vkg-test-ns")

        log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert log["action"] == "autoscaling_failed"
        assert log["level"] == "WARNING"
        assert log["error_code"] == "AccessDeniedException"
        assert "PutScalingPolicy" in log["error"]

    def test_autoscaling_failure_logging_never_raises(self, capsys):
        """An exception whose ``response`` is not a botocore dict must still be swallowed."""
        import index

        class _OddError(Exception):
            response = object()

        mock_autoscaling = MagicMock()
        mock_autoscaling.put_scaling_policy.side_effect = _OddError("boom")
        with patch.object(index, "autoscaling", mock_autoscaling):
            index._configure_auto_scaling("arn:aws:ecs:us-west-2:111:cluster/vkg", "coa-dev-vkg-test-ns")

        log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert log["action"] == "autoscaling_failed"
        assert log["error_code"] is None

    # ── circuit breaker + reload-outcome metrics ────────────────

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_enables_deployment_circuit_breaker(self, mock_ssm, mock_sd, mock_ecs):
        """A reload deploys with the ECS circuit breaker + rollback so a load that
        never goes healthy rolls back instead of leaving the namespace broken."""
        same_image = "img:same"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        cfg = mock_ecs.update_service.call_args.kwargs["deploymentConfiguration"]
        assert cfg["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_successful_reload_emits_reload_triggered_metric(self, mock_ssm, mock_sd, mock_ecs):
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadTriggered" in metrics

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_unresolvable_image_emits_reload_failed_metric(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @pytest.mark.parametrize("latest", [SERVABLE_LATEST, EMPTIED_LATEST], ids=["reload", "scale-to-zero"])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_reload_exception_emits_reload_failed_and_reraises(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3, latest
    ):
        _answer_latest(mock_s3, default=latest)
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = {
            "services": [{"status": "ACTIVE", "desiredCount": 1, "taskDefinition": "arn:td:current"}]
        }
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.side_effect = RuntimeError("ecs boom")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        with pytest.raises(RuntimeError, match="ecs boom"):
            index.handler(_make_event(), None)

        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_success_emits_reload_triggered_metric(self, mock_ssm, mock_sd, mock_ecs):
        """A first-time provision (create_service) must emit ReloadTriggered, like the update path."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadTriggered" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_unresolvable_image_emits_reload_failed_metric(self, mock_ssm, mock_sd, mock_ecs):
        """The provision path's unresolvable-image failure must emit ReloadFailed, like its reload-path twin."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index._provision_and_deploy("test-ns", "coa-dev-vkg-test-ns", ENV["CLUSTER_ARN"], "coa-dev", "v1")
        assert result["status"] == "failed"
        assert "cannot resolve container image" in result["reason"]
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    def test_emit_metric_publishes_dimensioned_and_undimensioned_series(self):
        """Each metric is published twice in one call: once dimensioned by
        Namespace (drill-down) and once undimensioned (the cluster-wide roll-up
        the ReloadFailedAlarm evaluates — a metric alarm can't use SEARCH())."""
        import importlib

        import index

        importlib.reload(index)
        index.cloudwatch = MagicMock()

        index._emit_metric("ReloadFailed", "some-ns")

        index.cloudwatch.put_metric_data.assert_called_once()
        call = index.cloudwatch.put_metric_data.call_args
        assert call.kwargs["Namespace"] == "COA/VKG"
        entries = call.kwargs["MetricData"]
        assert len(entries) == 2
        assert all(e["MetricName"] == "ReloadFailed" for e in entries)
        dimensioned = [e for e in entries if e.get("Dimensions")]
        undimensioned = [e for e in entries if not e.get("Dimensions")]
        assert len(dimensioned) == 1
        assert dimensioned[0]["Dimensions"] == [{"Name": "Namespace", "Value": "some-ns"}]
        # The alarm reads this undimensioned roll-up; it must exist.
        assert len(undimensioned) == 1


class TestSweep:
    """Scheduled-sweep mode: reconcile every per-namespace VKG service."""

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_fans_out_one_async_invocation_per_vkg_service(self, mock_ssm, mock_sd, mock_ecs, mock_s3):
        """{"sweep": true} only lists services and invokes this function once per
        <prefix>-vkg-* service, so the run's length no longer grows with each
        namespace's reconcile; non-vkg services are skipped."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        mock_ecs.get_paginator.return_value = _vkg_service_pages(
            ("coa-dev-vkg-ns1", "coa-dev-vkg-ns2", "coa-dev-notvkg")
        )

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "sweep_dispatched"
        assert result["total"] == 2  # the non-vkg service is filtered out
        assert result["dispatched"] == 2
        assert _dispatched(index) == [
            ("coa-dev-vkg-reload", "Event", {"sweep_namespace": "ns1"}),
            ("coa-dev-vkg-reload", "Event", {"sweep_namespace": "ns2"}),
        ]
        # Reconciling is left to the per-namespace invocations.
        mock_ecs.describe_services.assert_not_called()
        mock_ecs.update_service.assert_not_called()
        mock_s3.get_paginator.return_value.paginate.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_continues_after_a_failed_dispatch(self, mock_ssm, mock_sd, mock_ecs):
        """A namespace whose Invoke fails is recorded with ReloadFailed and the rest
        are still dispatched; the sweep does not raise (a retry would re-dispatch all)."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        mock_ecs.get_paginator.return_value = _vkg_service_pages(
            ("coa-dev-vkg-ns1", "coa-dev-vkg-ns2", "coa-dev-vkg-ns3")
        )

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.lambda_client.invoke.side_effect = [{}, RuntimeError("TooManyRequestsException"), {}]

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "sweep_dispatched"
        assert result["total"] == 3
        assert result["dispatched"] == 2
        assert {r["namespace"]: r["status"] for r in result["results"]} == {
            "ns1": "dispatched",
            "ns2": "failed",
            "ns3": "dispatched",
        }
        assert [p["sweep_namespace"] for _, _, p in _dispatched(index)] == ["ns1", "ns2", "ns3"]
        assert _reload_failed_for(index) == ["ns2"]

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_fails_fast_when_image_unresolvable(self, mock_ssm, mock_sd, mock_ecs):
        """If the SSM image can't be resolved, the sweep fails without listing or dispatching."""
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "failed"
        assert "container image" in result["reason"]
        mock_ecs.get_paginator.assert_not_called()
        index.lambda_client.invoke.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_no_services_is_a_clean_noop(self, mock_ssm, mock_sd, mock_ecs):
        """A cluster with no VKG services completes as an empty sweep (nothing dispatched)."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        mock_ecs.get_paginator.return_value = _vkg_service_pages(())

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["status"] == "sweep_dispatched"
        assert result["total"] == 0
        assert result["dispatched"] == 0
        index.lambda_client.invoke.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_walks_paginated_list_services(self, mock_ssm, mock_sd, mock_ecs):
        """_list_vkg_namespaces must dispatch services across every ListServices page."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        mock_ecs.get_paginator.return_value = _vkg_service_pages(("coa-dev-vkg-ns1",), ("coa-dev-vkg-ns2",))

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["total"] == 2
        assert [p["sweep_namespace"] for _, _, p in _dispatched(index)] == ["ns1", "ns2"]

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_handles_list_services_failure(self, mock_ssm, mock_sd, mock_ecs):
        """A ListServices failure ends the sweep cleanly (failed + ReloadFailed), not an uncaught raise."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.side_effect = RuntimeError("Throttling: Rate exceeded")
        mock_ecs.get_paginator.return_value = paginator

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["status"] == "failed"
        assert "list services failed" in result["reason"]
        index.lambda_client.invoke.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_sweep_namespace_reconciles_serving_with_latest(
        self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs, mock_s3
    ):
        """A {"sweep_namespace": ns} invocation applies the same latest/-based rule as
        an event, so a lost or out-of-order event is corrected within a week: servable
        at zero is restored, unservable but running is scaled down, servable and
        running is refreshed to the SSM image, and unservable at zero is left alone."""
        desired = {"restore": 0, "scale-down": 1, "refresh": 1, "idle": 0}
        _answer_latest(mock_s3, {"scale-down": EMPTIED_LATEST, "idle": EMPTIED_LATEST})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        mock_ecs.describe_services.side_effect = lambda cluster, services: _service(
            desired[services[0].removeprefix("coa-dev-vkg-")]
        )
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        statuses = {ns: index.handler({"sweep_namespace": ns}, None)["status"] for ns in desired}

        assert statuses == {
            "restore": "reload_triggered",
            "scale-down": "scaled_to_zero",
            "refresh": "reload_triggered",
            "idle": "skipped",
        }
        updates = {
            c.kwargs["service"].removeprefix("coa-dev-vkg-"): c.kwargs for c in mock_ecs.update_service.call_args_list
        }
        assert set(updates) == {"restore", "scale-down", "refresh"}
        assert updates["restore"]["desiredCount"] == 1
        assert updates["restore"]["forceNewDeployment"] is True
        assert updates["scale-down"] == {
            "cluster": ENV["CLUSTER_ARN"],
            "service": "coa-dev-vkg-scale-down",
            "desiredCount": 0,
        }
        assert "desiredCount" not in updates["refresh"]
        registered = {
            c.kwargs["family"].removeprefix("coa-dev-vkg-"): c.kwargs["containerDefinitions"][0]["image"]
            for c in mock_ecs.register_task_definition.call_args_list
        }
        assert registered == {"restore": "img:new", "refresh": "img:new"}
        min_capacity = {
            c.kwargs["ResourceId"].removeprefix("service/vkg/coa-dev-vkg-"): c.kwargs["MinCapacity"]
            for c in mock_autoscaling.register_scalable_target.call_args_list
        }
        assert min_capacity == {"restore": 1, "scale-down": 0}
        index.lambda_client.invoke.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_namespace_failure_emits_reload_failed_and_raises(self, mock_ssm, mock_sd, mock_ecs, mock_s3):
        """A namespace whose latest/ cannot be listed fails only its own invocation:
        ReloadFailed for it, no ECS action, and a raise so Lambda retries just it."""
        mock_s3.get_paginator.return_value.paginate.side_effect = RuntimeError("AccessDenied")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        with pytest.raises(RuntimeError, match="AccessDenied"):
            index.handler({"sweep_namespace": "ns1"}, None)

        mock_ecs.update_service.assert_not_called()
        assert _reload_failed_for(index) == ["ns1"]

    @pytest.mark.parametrize("ns", ["../etc", "", None])
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_namespace_rejects_invalid_namespace(self, mock_ssm, mock_sd, mock_ecs, mock_s3, ns):
        """A malformed sweep_namespace is skipped like a malformed event, without touching ECS or S3."""
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep_namespace": ns}, None)

        assert result["status"] == "skipped"
        mock_ecs.describe_services.assert_not_called()
        mock_s3.get_paginator.return_value.paginate.assert_not_called()

    @patch.dict(
        os.environ,
        {**ENV, "VKG_TASK_CPU": "2048", "VKG_TASK_MEMORY": "4096", "VKG_ONTOP_JAVA_ARGS": "-Xmx3072m -Xms1024m"},
    )
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_task_sizing_env_threaded_into_task_definition(self, mock_ssm, mock_sd, mock_ecs):
        """#149 cause B/C: reload reads VKG_TASK_CPU/MEMORY/ONTOP_JAVA_ARGS and

        threads them into the registered task def, so a reloaded task matches the
        CDK-provisioned initial one instead of the old hardcoded 512/1024 with a
        dead JAVA_OPTS. Guards against a regression that would silently
        under-provision and never apply the heap.
        """
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["cpu"] == "2048"
        assert td_kwargs["memory"] == "4096"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx3072m -Xms1024m"
        # The dead variable must NOT be emitted — that was the #149 cause C no-op.
        assert "JAVA_OPTS" not in env

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_task_sizing_defaults_match_cdk_when_env_unset(self, mock_ssm, mock_sd, mock_ecs):
        """With the sizing env unset, the reload falls back to defaults that match

        the CDK stack (cpu=1024/memory=2048, -Xmx1536m -Xms512m), so an
        un-parameterised reload is still safe rather than the old 512/1024.
        """
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["cpu"] == "1024"
        assert td_kwargs["memory"] == "2048"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx1536m -Xms512m"

    @patch.dict(os.environ, {**ENV, "VKG_TASK_MEMORY": "8192"}, clear=False)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_heap_derived_from_task_memory_when_java_args_unset(self, mock_ssm, mock_sd, mock_ecs):
        """When VKG_ONTOP_JAVA_ARGS is unset, the heap is derived from
        VKG_TASK_MEMORY (max heap ~=75%, initial ~=25%) so bumping memory alone
        scales the heap in step rather than leaving a stale hardcoded -Xmx.
        """
        # Ensure no explicit heap override leaks in from a broader ENV.
        os.environ.pop("VKG_ONTOP_JAVA_ARGS", None)
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["memory"] == "8192"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        # 8192 * 3//4 = 6144, 8192 // 4 = 2048
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx6144m -Xms2048m"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_register_task_definition_failure_fails_cleanly_without_creating_service(self, mock_ssm, mock_sd, mock_ecs):
        """If register_task_definition raises during a provision, the handler

        returns failed and never calls create_service, rather than letting the
        exception propagate and orphan the just-created Cloud Map entry.
        """
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        same_image = "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-abc"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        # Same image so the reload path takes the unchanged branch (no task-def
        # registration there); update_service then raises ServiceNotFound so the
        # handler routes to the provision path, where _register_task_definition
        # is called and we force it to fail.
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})
        mock_ecs.register_task_definition.side_effect = RuntimeError("ThrottlingException")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        assert "register_task_definition failed" in result["reason"]
        mock_ecs.create_service.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics
