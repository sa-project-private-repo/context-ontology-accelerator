# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Reload Lambda: provisions, redeploys or scales to zero a per-namespace VKG service on ontology deploy.

On every reload, registers a new task definition revision with the latest container
image (from SSM, written by CDK deploy) before forcing a new deployment. This ensures
ontology reloads always pick up the latest VKG image, not a stale one from provision time.

What to do is decided by the VKG files in ``latest/``, not by the event's
``version``: events are async and retried, so a delete's and a deploy's can land in
either order. Without loadable files the service is scaled to zero (never
provisioned); with them it is reloaded, and scaled back to one if it was at zero.
"""

from __future__ import annotations

import json
import os
import re

import boto3
from botocore.exceptions import ClientError

ecs = boto3.client("ecs")
ecr = boto3.client("ecr")
sd = boto3.client("servicediscovery")
ssm = boto3.client("ssm")
autoscaling = boto3.client("application-autoscaling")
cloudwatch = boto3.client("cloudwatch")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

_NS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# <registry>.dkr.ecr.<region>.amazonaws.com[.cn]/<repo>[:<tag>][@sha256:<digest>]
_ECR_IMAGE = re.compile(
    r"^(?P<registry>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/"
    r"(?P<repo>[a-z0-9][a-z0-9._/-]*?)(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_DIGEST_SUFFIX = re.compile(r"@(?P<digest>sha256:[0-9a-f]{64})$")

# Reload-outcome metrics; the vkg stack alarms on ReloadFailed.
_METRIC_NAMESPACE = "COA/VKG"

# Circuit breaker + rollback: paired with the container's functional /health
# probe, a broken reload never goes healthy and rolls back to the last-good task.
_DEPLOY_CONFIG = {
    "minimumHealthyPercent": 100,
    "maximumPercent": 200,
    "deploymentCircuitBreaker": {"enable": True, "rollback": True},
}

# Where VKG reads a namespace's files: ``{bucket}/{_ONTOLOGY_PREFIX}{ns}/latest/``.
_ONTOLOGY_PREFIX = "ontologies/"

# Actions _reconcile takes before giving up on a latest/ that keeps changing.
_MAX_RECONCILE_ROUNDS = 4


def _emit_metric(name, namespace):
    """Emit a reload-outcome metric. Never raises.

    Each metric is published twice: once dimensioned by Namespace (for
    per-namespace dashboards / drill-down) and once with no dimensions (a
    cluster-wide roll-up). The undimensioned series is what the vkg stack's
    ReloadFailedAlarm evaluates: CloudWatch metric alarms cannot be backed by a
    SEARCH() expression, so the alarm needs a single concrete series that
    actually receives data across all namespaces.
    """
    try:
        cloudwatch.put_metric_data(
            Namespace=_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Namespace", "Value": namespace}],
                },
                {
                    "MetricName": name,
                    "Value": 1,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as e:  # pragma: no cover - defensive
        print(json.dumps({"action": "metric_emit_failed", "metric": name, "error": str(e)}))


def handler(event, context):
    """Provision or redeploy per-namespace VKG service(s).

    Three entry modes:

    * **Per-namespace** (EventBridge ``ontology.deployed``, or the legacy
      ``ontology.published``): the event carries ``detail.namespace`` and only
      that namespace's VKG service is reconciled with its ``latest/`` files
      (see ``_reconcile``). ``detail.version`` is only logged.
    * **Sweep** (``{"sweep": true}``, sent by the weekly scheduled rule and,
      asynchronously, by the deploy-time custom resource whenever the VKG image
      changes): lists every ``<prefix>-vkg-*`` service in the cluster and
      asynchronously invokes this function once per namespace (see ``_sweep``).
      This keeps long-lived namespaces current even when their ontology is never
      redeployed (otherwise a base-image digest bump never reaches a running
      task, which is how stale VKG images accumulate), and corrects a lost or
      late event.
    * **Sweep namespace** (``{"sweep_namespace": ns}``, sent by the sweep): one
      namespace is reconciled like an event, reloaded ones against the latest
      SSM image, except that a service is left alone when its running tasks
      already have that image's digest and were created after its ``latest/``
      files were written.

    Args:
        event: EventBridge event, direct invoke or sweep payload. ``{"sweep": true}``
            selects sweep mode, ``sweep_namespace`` or ``detail.namespace`` the
            namespace to reconcile. A sweep's optional ``trigger`` field is logged
            and passed on to its per-namespace invocations.
        context: Lambda runtime context (unused).

    Returns:
        Per-namespace: a status dict (``reload_triggered`` / ``provisioned`` / ``unchanged`` /
        ``scaled_to_zero`` / ``skipped`` / ``failed``). Sweep: ``{"status": "sweep_dispatched", ...}``
        with a per-namespace ``results`` list, or ``failed`` if the image cannot
        be resolved or the services cannot be listed.

    Raises:
        Exception: In per-namespace modes, re-raised after emitting
            ``ReloadFailed`` on an unexpected error, so Lambda retries that
            namespace alone. The sweep isolates per-namespace dispatch failures
            and never raises for a single bad namespace.
    """
    cluster = os.environ["CLUSTER_ARN"]
    prefix = os.environ["RESOURCE_PREFIX"]

    if event.get("sweep") is True:
        return _sweep(cluster, prefix, str(event.get("trigger") or "schedule"))

    if "sweep_namespace" in event:
        ns, version = event["sweep_namespace"], f"sweep-{event.get('trigger') or 'schedule'}"
    else:
        ns = event.get("detail", {}).get("namespace", "unknown")
        version = event.get("detail", {}).get("version", "unknown")

    if not isinstance(ns, str) or ns == "unknown" or not _NS_PATTERN.match(ns):
        print(json.dumps({"action": "reload_skipped", "reason": "missing or invalid namespace"}))
        return {"status": "skipped", "reason": "missing or invalid namespace"}

    # A sweep leaves a service alone once its tasks run the current image digest
    # and have loaded latest/; an event restarts it anyway so the task loads the
    # newly deployed files.
    return _reconcile(ns, version, cluster, prefix, restart_if_current="sweep_namespace" not in event)


def _sweep(cluster, prefix, trigger="schedule"):
    """Fan out one async ``{"sweep_namespace": ns}`` invocation per ``<prefix>-vkg-*`` service.

    Reconciling every namespace inside this invocation outgrew the function timeout,
    and a timed-out sweep is retried from the first namespace, so the tail was
    never reached. Each namespace now runs (and is retried) in its own invocation,
    which leaves a service alone when its running tasks already have the digest
    the SSM image resolves to and have loaded its ``latest/`` (see ``_reload_one``).
    ``trigger`` goes with each invocation so its logs show which sweep ran it.
    The image is resolved first only to fail fast before dispatching anything.
    A namespace whose Invoke fails gets ``ReloadFailed`` and the sweep continues:
    raising would make Lambda retry the whole sweep and re-dispatch the rest.
    """
    print(json.dumps({"action": "sweep_start", "cluster": cluster, "trigger": trigger}))
    container_image = _resolve_container_image()
    if not container_image:
        print(json.dumps({"action": "sweep_failed", "reason": "cannot resolve container image"}))
        _emit_metric("ReloadFailed", "sweep")
        return {"status": "failed", "reason": "cannot resolve container image"}

    try:
        namespaces = _list_vkg_namespaces(cluster, prefix)
    except Exception as e:
        # A failed ListServices (throttling, IAM denial, bad cluster) must not
        # raise uncaught — surface it like every other reload failure so the
        # ReloadFailed alarm fires and the run ends cleanly.
        print(json.dumps({"action": "sweep_failed", "reason": "list_services_failed", "error": str(e)}))
        _emit_metric("ReloadFailed", "sweep")
        return {"status": "failed", "reason": f"list services failed: {e}"}

    function_name = os.environ["AWS_LAMBDA_FUNCTION_NAME"]
    results = []
    for ns in namespaces:
        try:
            lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps({"sweep_namespace": ns, "trigger": trigger}),
            )
            results.append({"namespace": ns, "status": "dispatched"})
        except Exception as e:  # one bad namespace must not abort the sweep
            print(json.dumps({"action": "sweep_dispatch_failed", "namespace": ns, "error": str(e)}))
            _emit_metric("ReloadFailed", ns)
            results.append({"namespace": ns, "status": "failed", "error": str(e)})

    dispatched = sum(1 for r in results if r["status"] == "dispatched")
    print(
        json.dumps({"action": "sweep_dispatched", "trigger": trigger, "total": len(results), "dispatched": dispatched})
    )
    return {"status": "sweep_dispatched", "total": len(results), "dispatched": dispatched, "results": results}


def _list_vkg_namespaces(cluster, prefix):
    """Return the namespace id of every ``<prefix>-vkg-*`` service in the cluster."""
    marker = f"{prefix}-vkg-"
    namespaces = []
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster):
        for arn in page.get("serviceArns", []):
            name = arn.split("/")[-1]
            if name.startswith(marker):
                namespaces.append(name[len(marker) :])
    return namespaces


def _reconcile(ns, version, cluster, prefix, restart_if_current=True):
    """Make a namespace's VKG serving match the VKG files in its ``latest/``.

    Servable files: reload (``_reload_one`` provisions a missing service and scales
    one at zero back up). None: scale to zero (``_scale_to_zero`` never provisions).
    A sweep passes ``restart_if_current=False``, so ``_reload_one`` leaves a service
    alone once its tasks run the current image digest and have loaded ``latest/``.
    ``version`` is only logged: retried events arrive out of order. A deploy or
    delete (and its own invocation) can land while this acts, so ``latest/`` is read
    again after every action until it matches the last one: whichever invocation
    writes last also reads last.

    Raises:
        RuntimeError: After emitting ``ReloadFailed`` when ``latest/`` still differs
            from the last action after ``_MAX_RECONCILE_ROUNDS`` actions; the async
            retry reads it again later.
    """
    servable = _latest_servable(ns)
    print(json.dumps({"action": "reconcile_start", "namespace": ns, "version": version, "servable": servable}))
    for _ in range(_MAX_RECONCILE_ROUNDS):
        if servable:
            result = _reload_one(ns, version, cluster, prefix, restart_if_current)
        else:
            result = _scale_to_zero(ns, version, cluster, prefix)
        now = _latest_servable(ns)
        if now == servable:
            return result
        print(json.dumps({"action": "latest_changed", "namespace": ns, "servable": now}))
        servable = now
    print(json.dumps({"action": "reconcile_failed", "namespace": ns, "reason": "latest/ kept changing"}))
    _emit_metric("ReloadFailed", ns)
    raise RuntimeError(f"latest/ kept changing for {ns} over {_MAX_RECONCILE_ROUNDS} rounds")


def _latest_servable(ns):
    """Whether ``latest/`` holds the files ``packages/vkg/entrypoint.sh`` needs to load.

    Mirrors the entrypoint: a depth-1 ``*.ttl``/``*.owl`` ontology AND a depth-1
    ``*.obda``/``*.r2rml`` mappings file. A non-empty prefix is not enough:
    ``clear_latest`` deletes only the VKG files and leaves ``examples.json``.

    Raises:
        Exception: Re-raised after emitting ``ReloadFailed`` when ``latest/`` cannot
            be listed, so the event is retried instead of acted on blind.
    """
    try:
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=os.environ["ONTOLOGY_BUCKET"], Prefix=f"{_ONTOLOGY_PREFIX}{ns}/latest/", Delimiter="/"
        )
        names = [obj["Key"].rsplit("/", 1)[-1] for page in pages for obj in page.get("Contents", [])]
    except Exception as e:
        print(
            json.dumps(
                {"action": "latest_list_failed", "namespace": ns, "error_type": type(e).__name__, "error": str(e)}
            )
        )
        _emit_metric("ReloadFailed", ns)
        raise
    has_ontology = any(n.endswith((".ttl", ".owl")) for n in names)
    has_mappings = any(n.endswith((".obda", ".r2rml")) for n in names)
    return has_ontology and has_mappings


def _reload_one(ns, version, cluster, prefix, restart_if_current=True):
    """Reconcile a single namespace's VKG service to the latest image.

    Resolves the target image from SSM. If the service's task definition names a
    different image, registers a fresh task-definition revision first. If it names
    the same image URI, forces a new deployment when ``restart_if_current`` is true
    (an ``ontology.deployed`` event must restart the task to load the new files).
    Otherwise (the sweep) it compares image digests: the service is left alone and
    reported ``unchanged`` only when every running task's digest equals the digest
    the image URI resolves to now and every running task was created after the
    newest file VKG loads from ``latest/`` was written. A reused tag with a new
    build therefore still rolls out, and so does a task that started before the
    files it should serve (its ``ontology.deployed`` reload was lost or late, and
    VKG reads ``latest/`` only at task start). When a digest or a time cannot be
    read the service is restarted as before, so a failed lookup never stops a
    rollout. If the service does not yet exist, provisions it. An ACTIVE service
    at zero (see ``_scale_to_zero``) is scaled back to one, MinCapacity first, and
    never left alone. Reload outcomes are published as
    ``ReloadTriggered``/``ReloadFailed`` CloudWatch metrics.

    Raises:
        Exception: Re-raised after emitting ``ReloadFailed`` when an unexpected
            error occurs during reload of an existing service.
    """
    service_name = f"{prefix}-vkg-{ns}"
    log = {"action": "reload_start", "namespace": ns, "version": version, "cluster": cluster, "service": service_name}
    print(json.dumps(log))

    try:
        container_image = _resolve_container_image()
        if not container_image:
            print(json.dumps({"action": "reload_failed", "namespace": ns, "reason": "cannot resolve container image"}))
            _emit_metric("ReloadFailed", ns)
            return {"status": "failed", "reason": "cannot resolve container image"}

        current_image, desired_count = _get_current_service_state(cluster, service_name)

        scale_up = {}
        if desired_count == 0:
            # MinCapacity first, mirroring _scale_to_zero.
            _configure_auto_scaling(cluster, service_name)
            scale_up = {"desiredCount": 1}

        # Never skip a service at zero: tasks still stopping can report the current digest.
        if current_image == container_image and not restart_if_current and not scale_up:
            target_digest = _resolve_image_digest(container_image)
            running_digests, oldest_task = _running_image_digests(cluster, service_name)
            if target_digest and running_digests and running_digests == {target_digest}:
                # VKG reads latest/ only when a task starts: a task created before the
                # newest file there missed that deploy's reload (a lost or late event).
                written_at = _latest_written_at(ns)
                if written_at and oldest_task and oldest_task > written_at:
                    print(
                        json.dumps(
                            {
                                "action": "image_current_skipped",
                                "namespace": ns,
                                "image": container_image,
                                "digest": target_digest,
                            }
                        )
                    )
                    return {"status": "unchanged", "namespace": ns}
                print(
                    json.dumps(
                        {
                            "action": "latest_not_loaded",
                            "namespace": ns,
                            "latest_written": written_at.isoformat() if written_at else "(unknown)",
                            "oldest_task_created": oldest_task.isoformat() if oldest_task else "(unknown)",
                        }
                    )
                )
            else:
                # Same URI but a different (or unknown) digest: the tag now points at
                # another build, or we cannot tell. Restart so ECS resolves the tag
                # again, exactly as before the skip existed.
                known = bool(target_digest and running_digests)
                print(
                    json.dumps(
                        {
                            "action": "image_digest_changed" if known else "image_digest_unknown",
                            "namespace": ns,
                            "image": container_image,
                            "target_digest": target_digest or "(unknown)",
                            "running_digests": sorted(running_digests) or ["(unknown)"],
                        }
                    )
                )
        if current_image == container_image:
            if restart_if_current:
                print(json.dumps({"action": "image_unchanged", "namespace": ns, "image": container_image}))
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
                **scale_up,
            )
        else:
            print(
                json.dumps(
                    {
                        "action": "image_updated",
                        "namespace": ns,
                        "old_image": current_image or "(unknown)",
                        "new_image": container_image,
                    }
                )
            )
            task_def_arn = _register_task_definition(ns, prefix, container_image)
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
                **scale_up,
            )

        deployment_id = resp["service"].get("deployments", [{}])[0].get("id", "unknown")
        print(
            json.dumps(
                {
                    "action": "reload_triggered",
                    "namespace": ns,
                    "version": version,
                    "service": service_name,
                    "deployment_id": deployment_id,
                    "image": container_image,
                    "image_changed": current_image != container_image,
                    "scaled_up_from_zero": bool(scale_up),
                }
            )
        )
        _emit_metric("ReloadTriggered", ns)
        return {"status": "reload_triggered", "namespace": ns, "deployment_id": deployment_id}
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        print(json.dumps({"action": "provision_start", "namespace": ns, "service": service_name}))
        return _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image)
    except Exception as e:
        print(
            json.dumps(
                {
                    "action": "reload_failed",
                    "namespace": ns,
                    "service": service_name,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            )
        )
        _emit_metric("ReloadFailed", ns)
        raise


def _scale_to_zero(ns, version, cluster, prefix):
    """Stop serving a namespace whose ``latest/`` has no loadable files (domain ontology deleted).

    A redeploy would boot a task that fails its ``/health`` check and rolls back,
    leaving the old task serving the deleted ontology. Lowers MinCapacity to 0
    first so auto scaling cannot undo the new desired count, then scales the
    service to zero. A missing, INACTIVE or DRAINING service is left alone (never
    provisioned from an empty ``latest/``), and so is one already at zero.

    Raises:
        Exception: Re-raised after emitting ``ReloadFailed`` on an unexpected error.
    """
    service_name = f"{prefix}-vkg-{ns}"
    log = {
        "action": "scale_to_zero_start",
        "namespace": ns,
        "version": version,
        "cluster": cluster,
        "service": service_name,
    }
    print(json.dumps(log))

    try:
        # A missing service comes back under ``failures``; a deleted one as INACTIVE/DRAINING.
        services = ecs.describe_services(cluster=cluster, services=[service_name]).get("services", [])
        active = [svc for svc in services if svc.get("status") == "ACTIVE"]
        if not active:
            reason = "nothing to serve"
        elif active[0].get("desiredCount") == 0:
            reason = "already scaled to zero"
        else:
            reason = None
        if reason:
            print(json.dumps({"action": "scale_to_zero_skipped", "namespace": ns, "reason": reason}))
            return {"status": "skipped", "namespace": ns, "reason": reason}
        _configure_auto_scaling(cluster, service_name, min_capacity=0)
        ecs.update_service(cluster=cluster, service=service_name, desiredCount=0)
    except Exception as e:
        print(
            json.dumps(
                {
                    "action": "scale_to_zero_failed",
                    "namespace": ns,
                    "service": service_name,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            )
        )
        _emit_metric("ReloadFailed", ns)
        raise

    print(json.dumps({"action": "scaled_to_zero", "namespace": ns, "service": service_name}))
    return {"status": "scaled_to_zero", "namespace": ns, "service": service_name}


def _get_current_service_state(cluster, service_name):
    """Return ``(image, desired_count)`` for the service and its current task definition.

    ``image`` is ``""`` when it cannot be read; ``desired_count`` is ``None`` unless
    the service is ACTIVE (an INACTIVE one also reports 0 but is not scaled down).

    Raises:
        Exception: DescribeServices failed other than NotFound/NotActive. Guessing
            would force-deploy a zero-count service and leave it at zero.
    """
    try:
        services = ecs.describe_services(cluster=cluster, services=[service_name]).get("services", [])
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        return "", None
    if not services:
        return "", None
    desired_count = services[0].get("desiredCount") if services[0].get("status") == "ACTIVE" else None
    try:
        td_resp = ecs.describe_task_definition(taskDefinition=services[0]["taskDefinition"])
        containers = td_resp["taskDefinition"].get("containerDefinitions", [])
        image = containers[0].get("image", "") if containers else ""
    except Exception as e:
        print(json.dumps({"action": "get_image_failed", "service": service_name, "error_type": type(e).__name__}))
        image = ""
    return image, desired_count


def _running_image_digests(cluster, service_name):
    """Return the image digests of the service's RUNNING tasks and when the oldest was created.

    An empty set means "unknown": no running task, a task that has not reported
    a digest yet, or an API error. The caller then restarts the service rather
    than skipping it. The creation time is None when any task's is unknown.
    """
    try:
        arns = ecs.list_tasks(cluster=cluster, serviceName=service_name, desiredStatus="RUNNING").get("taskArns", [])
        if not arns:
            return set(), None
        digests, created = set(), []
        for i in range(0, len(arns), 100):
            for task in ecs.describe_tasks(cluster=cluster, tasks=arns[i : i + 100]).get("tasks", []):
                containers = task.get("containers") or []
                digest = containers[0].get("imageDigest", "") if containers else ""
                if not digest:
                    return set(), None
                digests.add(digest)
                created.append(task.get("createdAt"))
        return digests, None if None in created else min(created, default=None)
    except Exception as e:
        print(json.dumps({"action": "running_digest_failed", "service": service_name, "error_type": type(e).__name__}))
        return set(), None


def _latest_written_at(ns):
    """Return when the newest file VKG loads from ``latest/`` was written, or None when unknown."""
    try:
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=os.environ["ONTOLOGY_BUCKET"], Prefix=f"{_ONTOLOGY_PREFIX}{ns}/latest/", Delimiter="/"
        )
        return max(
            (
                obj["LastModified"]
                for page in pages
                for obj in page.get("Contents", [])
                if obj["Key"].endswith((".ttl", ".owl", ".obda", ".r2rml", ".sql"))
            ),
            default=None,
        )
    except Exception as e:
        print(json.dumps({"action": "latest_written_lookup_failed", "namespace": ns, "error_type": type(e).__name__}))
        return None


def _resolve_image_digest(image):
    """Return the digest an image URI points at now, or "" when it cannot be resolved.

    A URI pinned with ``@sha256:`` is its own digest. A tagged ECR URI is looked
    up with ``ecr:DescribeImages``, so a tag that was moved to a new build
    resolves to the new digest. Anything else (a non-ECR registry, no tag, an
    API error) is unknown.
    """
    pinned = _DIGEST_SUFFIX.search(image or "")
    if pinned:
        return pinned.group("digest")
    match = _ECR_IMAGE.match(image or "")
    if not match or not match.group("tag"):
        print(json.dumps({"action": "image_digest_unresolvable", "image": image, "reason": "not a tagged ECR image"}))
        return ""
    region = match.group("region")
    try:
        client = ecr if region == os.environ.get("AWS_REGION", region) else boto3.client("ecr", region_name=region)
        resp = client.describe_images(
            registryId=match.group("registry"),
            repositoryName=match.group("repo"),
            imageIds=[{"imageTag": match.group("tag")}],
        )
        details = resp.get("imageDetails") or []
        return details[0].get("imageDigest", "") if details else ""
    except Exception as e:
        print(json.dumps({"action": "image_digest_lookup_failed", "image": image, "error_type": type(e).__name__}))
        return ""


def _resolve_container_image():
    """Read latest VKG image URI from SSM (written by CDK on every deploy)."""
    param_name = os.environ.get("VKG_IMAGE_PARAM_NAME", "")
    if not param_name:
        print(json.dumps({"action": "image_resolve_failed", "reason": "VKG_IMAGE_PARAM_NAME not set"}))
        return ""
    try:
        resp = ssm.get_parameter(Name=param_name)
        image = resp["Parameter"]["Value"]
        print(json.dumps({"action": "image_resolved", "image": image, "source": "ssm"}))
        return image
    except Exception as e:
        print(json.dumps({"action": "image_resolve_failed", "reason": str(e)}))
        return ""


def _register_task_definition(ns, prefix, container_image):
    """Register a new task def revision with the latest image for this namespace."""
    task_role_arn = os.environ.get("VKG_TASK_ROLE_ARN", "")
    execution_role_arn = os.environ.get("VKG_EXECUTION_ROLE_ARN", "")
    ontology_bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    region = os.environ.get("AWS_REGION", "us-west-2")

    # Task sizing and heap are read from the environment so this reload path and
    # the CDK-provisioned initial service stay in sync from a single source of
    # truth (VkgStack sets these on the reload Lambda). Previously this path
    # hardcoded cpu=512/memory=1024 while the CDK service defaulted to 1024/2048,
    # so per-namespace reloads silently ran under-provisioned and the OWL2QL
    # translation pass could never finish (#149 cause B). Defaults below match
    # the CDK stack defaults so behaviour is safe even if the env is unset.
    task_cpu = os.environ.get("VKG_TASK_CPU", "1024")
    task_memory = os.environ.get("VKG_TASK_MEMORY", "2048")
    # The Ontop launcher reads ONTOP_JAVA_ARGS (NOT JAVA_OPTS). Setting JAVA_OPTS
    # was a silent no-op (#149 cause C). When VKG_ONTOP_JAVA_ARGS is not set
    # explicitly, derive the heap from task memory (max heap ~= 75% of task
    # memory, initial heap ~= 25%) so bumping VKG_TASK_MEMORY alone scales the
    # heap in step rather than leaving a stale hardcoded -Xmx.
    ontop_java_args = os.environ.get("VKG_ONTOP_JAVA_ARGS", "")
    if not ontop_java_args:
        try:
            mem_mib = int(task_memory)
        except (TypeError, ValueError):
            mem_mib = 2048
        xmx = max(512, mem_mib * 3 // 4)
        xms = max(256, mem_mib // 4)
        ontop_java_args = f"-Xmx{xmx}m -Xms{xms}m"

    family = f"{prefix}-vkg-{ns}" if prefix else f"vkg-{ns}"
    td_resp = ecs.register_task_definition(
        family=family,
        taskRoleArn=task_role_arn,
        executionRoleArn=execution_role_arn,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=task_cpu,
        memory=task_memory,
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[
            {
                "name": "ontop",
                "image": container_image,
                "essential": True,
                "environment": [
                    {"name": "ONTOLOGY_BUCKET", "value": ontology_bucket},
                    {"name": "ONTOLOGY_PREFIX", "value": _ONTOLOGY_PREFIX},
                    {"name": "NAMESPACE", "value": ns},
                    {"name": "NAMESPACE_ID", "value": ns},
                    {"name": "ENDPOINT_PORT", "value": "8080"},
                    {"name": "ONTOP_JAVA_ARGS", "value": ontop_java_args},
                ],
                "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                "healthCheck": {
                    "command": ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"],
                    "interval": 30,
                    "timeout": 10,
                    "retries": 5,
                    "startPeriod": 180,
                },
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/vkg-{ns}",
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "vkg",
                        "awslogs-create-group": "true",
                    },
                },
            }
        ],
    )
    task_def_arn = td_resp["taskDefinition"]["taskDefinitionArn"]
    print(json.dumps({"action": "task_def_registered", "namespace": ns, "task_def_arn": task_def_arn}))
    return task_def_arn


def _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image=None):
    """Create Cloud Map entry and launch ECS service for a new namespace."""
    cloud_map_ns_id = os.environ.get("CLOUD_MAP_NAMESPACE_ID", "")
    ontology_bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    subnet_ids = [s for s in os.environ.get("PRIVATE_SUBNET_IDS", "").split(",") if s]
    sg_id = os.environ.get("ECS_SECURITY_GROUP_ID", "")

    if not all([cloud_map_ns_id, ontology_bucket, subnet_ids]):
        print(json.dumps({"action": "provision_skipped", "namespace": ns, "reason": "missing env vars"}))
        return {"status": "skipped", "reason": "provision env vars not configured"}

    if not container_image:
        container_image = _resolve_container_image()
    if not container_image:
        print(json.dumps({"action": "provision_failed", "namespace": ns, "reason": "cannot resolve container image"}))
        _emit_metric("ReloadFailed", ns)
        return {"status": "failed", "reason": "cannot resolve container image"}

    registry_arn = _ensure_cloud_map(ns, cloud_map_ns_id)
    try:
        task_def_arn = _register_task_definition(ns, prefix, container_image)
    except Exception as e:
        # The Cloud Map entry was just created (line above); without a task def
        # the service can never be created, leaving it orphaned. Log with the
        # dangling registry so an operator can reconcile, then fail the reload.
        print(
            json.dumps(
                {
                    "action": "register_task_definition_failed",
                    "namespace": ns,
                    "registry_arn": registry_arn,
                    "error": str(e),
                }
            )
        )
        _emit_metric("ReloadFailed", ns)
        return {"status": "failed", "reason": f"register_task_definition failed: {e}"}

    try:
        ecs.create_service(
            cluster=cluster,
            serviceName=service_name,
            taskDefinition=task_def_arn,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [sg_id] if sg_id else [],
                    "assignPublicIp": "DISABLED",
                }
            },
            serviceRegistries=[{"registryArn": registry_arn}],
            deploymentConfiguration={
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
                "deploymentCircuitBreaker": {"enable": True, "rollback": True},
            },
        )
        print(json.dumps({"action": "service_created", "namespace": ns, "service": service_name}))
        _configure_auto_scaling(cluster, service_name)
        _emit_metric("ReloadTriggered", ns)
        return {"status": "provisioned", "namespace": ns, "service": service_name}
    except ecs.exceptions.InvalidParameterException as e:
        if "already exists" in str(e).lower() or "not idempotent" in str(e).lower():
            ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
            )
            _configure_auto_scaling(cluster, service_name)
            print(json.dumps({"action": "reload_triggered", "namespace": ns, "service": service_name}))
            _emit_metric("ReloadTriggered", ns)
            return {"status": "reload_triggered", "namespace": ns}
        raise


def _configure_auto_scaling(cluster, service_name, min_capacity=1):
    """Register auto-scaling for a per-namespace VKG service (min=min_capacity, max=3, CPU 70%).

    ``min_capacity`` is 0 only for a service scaled to zero. The CPU target-tracking
    policy cannot scale out from zero tasks (no CPUUtilization datapoints), so the
    service stays at zero until a deploy restores it.
    """
    cluster_name = cluster.split("/")[-1]
    resource_id = f"service/{cluster_name}/{service_name}"
    try:
        autoscaling.register_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            MinCapacity=min_capacity,
            MaxCapacity=3,
        )
        autoscaling.put_scaling_policy(
            PolicyName=f"{service_name}-cpu-scaling",
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration={
                "TargetValue": 70.0,
                "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
                "ScaleInCooldown": 300,
                "ScaleOutCooldown": 60,
            },
        )
        print(json.dumps({"action": "autoscaling_configured", "service": service_name, "min_capacity": min_capacity}))
    except Exception as e:
        # Non-fatal, but an IAM gap here leaves the service silently unscaled.
        error_code = e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else None
        print(
            json.dumps(
                {
                    "action": "autoscaling_failed",
                    "level": "WARNING",
                    "service": service_name,
                    "error_code": error_code,
                    "error": str(e),
                }
            )
        )


def _ensure_cloud_map(ns, cloud_map_ns_id):
    """Create or look up Cloud Map service entry. Returns registry ARN."""
    try:
        resp = sd.create_service(
            Name=f"vkg-{ns}",
            NamespaceId=cloud_map_ns_id,
            DnsConfig={"DnsRecords": [{"Type": "A", "TTL": 10}], "RoutingPolicy": "MULTIVALUE"},
            HealthCheckCustomConfig={"FailureThreshold": 1},
        )
        return resp["Service"]["Arn"]
    except sd.exceptions.ServiceAlreadyExists:
        pass
    paginator = sd.get_paginator("list_services")
    for page in paginator.paginate(Filters=[{"Name": "NAMESPACE_ID", "Values": [cloud_map_ns_id], "Condition": "EQ"}]):
        for svc in page.get("Services", []):
            if svc["Name"] == f"vkg-{ns}":
                return svc["Arn"]
    raise RuntimeError(f"Cloud Map service vkg-{ns} not found after create reported it exists")
