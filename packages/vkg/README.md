# Virtual Knowledge Graph (VKG)

Translation-only service wrapping Ontop 5.x. Translates SPARQL queries into SQL using ontology mappings, without connecting to a real database.

## Architecture

- **Ontop 5.x** — SPARQL-to-SQL translator (runs on port 8081 internally)
- **translate-server.py** — Python HTTP facade (port 8080) exposing `/translate` and `/health`
- **H2 in-memory** — schema-only database for Ontop's mapping parser (no real data)

At startup, the entrypoint downloads ontology + mappings from S3, loads the schema into H2, and starts both services.

### Patched Ontop dependencies

Ontop 5.5.0 (`ARG ONTOP_VERSION` in the Dockerfile) bundles Tomcat, Spring Framework, Thymeleaf, Jackson, commons-beanutils and Guava releases with known CVEs. At build time `ontop-lib-overrides.sh` replaces them in `/opt/ontop/lib` with the versions pinned in `ontop-lib-overrides.txt`, and removes `thymeleaf-extras-java8time`, which Thymeleaf 3.1 absorbed. The pins follow the versions Ontop's own `version5` branch uses on the same Spring Boot 2.7 line.

- Every JAR is checked against its pinned sha256 before it replaces the bundled one. A mismatch or a missing download fails the build.
- The build also fails if a bundled JAR is newer than its pin, so a pin can never downgrade a library.
- When you change `ONTOP_VERSION`, go through the manifest and drop each entry the new release already bundles at that version or later. The no-downgrade check names the entries you have to drop.
- JARs come from Maven Central. Pass `--build-arg MAVEN_BASE=<repository URL>` to use a mirror.

## Production

The container is built from this Dockerfile and deployed to ECS. Ontology artifacts are fetched from S3 at runtime:

```
s3://{ONTOLOGY_BUCKET}/ontologies/{NAMESPACE}/{VERSION}/
  ├── ontology.ttl      # OWL ontology
  ├── mappings.obda     # Ontop OBDA mappings
  └── schema.sql        # H2 schema for mapping validation
```

To upload artifacts for a namespace:

```bash
aws s3 cp ./ontology.ttl s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
aws s3 cp ./mappings.obda s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
aws s3 cp ./schema.sql s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
```

## Local Testing

Test fixtures are in `tests/fixtures/`. To run the VKG container locally with real S3 artifacts:

```bash
# Build the image
docker build -t coa-vkg:local packages/vkg/

# Run with real S3 artifacts (requires AWS credentials)
docker run -d --name coa-vkg -p 8090:8080 \
  -e ONTOLOGY_BUCKET=coa-dev-neptune-data-123456789012 \
  -e NAMESPACE=insurance \
  -e VERSION=latest \
  -e AWS_DEFAULT_REGION=us-east-1 \
  coa-vkg:local
```

### Test the translation endpoint

```bash
curl -s http://localhost:8090/sparql/translate \
  -H "Content-Type: application/json" \
  -d '{"sparql": "SELECT ?x WHERE { ?x a <http://example.org/insurance#Claim> }", "namespace": "insurance"}'
```

### Test fixtures

| File | Purpose |
|------|---------|
| `tests/fixtures/insurance.obda` | Sample OBDA mappings for the insurance namespace (Claim → claims table) |
| `tests/fixtures/schema.sql` | H2 schema mirroring Athena `insurance_lake.claims` for Ontop validation |

## Container Startup and Troubleshooting

### S3 download retry

At startup, the entrypoint downloads ontology artifacts from S3 with **3 attempts** and exponential backoff (2s, 4s, 8s delays). Expected log output during a transient failure:

```
[VKG] Downloading artifacts...
[VKG] WARN: S3 download attempt 1/3 failed — retrying in 2s
[VKG] WARN: S3 download attempt 2/3 failed — retrying in 4s
```

If all 3 attempts fail, the container enters **degraded mode** (503 on all `/sparql/translate` requests) and logs:

```
[VKG] WARN: Failed to download from S3 after 3 attempts — starting in degraded mode
```

**Common S3 issues:**
- Verify the S3 path exists: `aws s3 ls s3://$ONTOLOGY_BUCKET/ontologies/$NAMESPACE/$VERSION/`
- Check IAM permissions on the task role (needs `s3:GetObject` on `ontologies/*`)
- Confirm the bucket name matches the CDK-deployed bucket

**Recovery:** Fix the S3 artifacts, then restart the task. The health check will replace degraded containers automatically (see below).

## Task Sizing and Environment Variables

The VKG task's resource allocation and JVM heap are controlled by environment
variables set on the ECS task definition (and passed through by the reload
Lambda). Getting these right matters: an under-provisioned task cannot finish the
OWL2QL translation pass for larger ontologies, which leaves the container failing
its health check and the namespace reporting `DEGRADED` health indefinitely.

| Variable | Default | Purpose |
|----------|---------|---------|
| `VKG_TASK_CPU` | `1024` (1 vCPU) | Fargate task CPU units. Set on the reload Lambda; used when it registers a new task definition. |
| `VKG_TASK_MEMORY` | `2048` (2 GB) | Fargate task memory. |
| `ONTOP_JAVA_ARGS` | `-Xmx1536m -Xms512m` | JVM args for the Ontop launcher (read directly by the container). The reload Lambda's `VKG_ONTOP_JAVA_ARGS` is written into the task def as this variable. |

**`ONTOP_JAVA_ARGS`, not `JAVA_OPTS`.** The Ontop launcher reads its heap
settings from `ONTOP_JAVA_ARGS`. Setting `JAVA_OPTS` has **no effect** — the heap
is silently ignored and Ontop starts at its small built-in default regardless of
task memory. The entrypoint keeps a backward-compatibility fallback
(`ONTOP_JAVA_ARGS="${ONTOP_JAVA_ARGS:-${JAVA_OPTS:-}}"`) so a task def that still
only sets `JAVA_OPTS` is honoured, but new configuration should always set
`ONTOP_JAVA_ARGS`.

**Heap vs. task memory.** Size the max heap (`-Xmx`) at roughly **60–75% of
`VKG_TASK_MEMORY`**, leaving room for JVM overhead and the OS. E.g. a 4 GB task
pairs with `-Xmx3072m -Xms1024m`.

**Verifying the applied sizing.** Check the running task's definition:

```bash
aws ecs describe-task-definition --task-definition <family> \
  --query 'taskDefinition.{cpu:cpu,memory:memory,env:containerDefinitions[0].environment}'
```

Confirm `cpu`/`memory` match your intended values and that the container
environment contains `ONTOP_JAVA_ARGS` (and **not** `JAVA_OPTS`). At startup the
entrypoint also logs the resolved value: `[VKG] Ontop heap args
(ONTOP_JAVA_ARGS): '...'`.

For how to configure these from CDK (the `VkgStack` `cpu`, `memoryLimitMiB`, and
`ontopJavaArgs` props), see the **VKG Task Sizing** section of the deployment
guide.

## Health Check and Self-Healing

### /health endpoint

| Status | Meaning |
|--------|---------|
| `200` | Ontop loaded, ready to translate |
| `503` | Degraded mode (ontology not loaded) |

### Self-healing mechanism

The Docker/ECS health check calls `curl -sf http://localhost:8080/health`. Since `-f` fails on non-2xx responses, a container stuck in degraded mode (returning 503) will be marked **UNHEALTHY** by ECS after 5 consecutive failures (30s interval = ~2.5 min).

ECS then replaces the task automatically. The replacement container retries S3 download from scratch.

**Health check parameters:**
- Interval: 30s
- Timeout: 10s
- Start period: 180s (grace period for S3 download + Ontop initialization)
- Retries: 5

### Checking container health

```bash
# From inside the container
curl -sf http://localhost:8080/health

# From ECS (check task health status)
aws ecs describe-tasks --cluster $CLUSTER --tasks $TASK_ARN \
  --query 'tasks[0].containers[0].healthStatus'
```

### Expected lifecycle during reload failures

1. EventBridge triggers reload Lambda
2. Lambda force-deploys the ECS service (rolling update)
3. New task starts, retries S3 download (3 attempts)
4. If S3 fails: container enters degraded mode → health check fails after ~2.5 min → ECS replaces
5. Cycle repeats until S3 artifacts are available

## Image Reloads and the Scheduled Sweep

VKG runs **one ECS service per namespace** (`<prefix>-vkg-<namespace>`). No static
"default" service exists: a namespace's service is created the first time one of
its ontology versions is deployed. The `<prefix>-vkg-reload` Lambda owns
provisioning, scale-to-zero and image refresh; the latest image URI is published
to SSM (`/<prefix>/vkg/container-image`) on every CDK deploy.

### Three ways the reload Lambda runs

| Trigger | Event | Scope |
|---------|-------|-------|
| `ontology.deployed` (EventBridge) | `{"detail": {"namespace": "...", "version": "v3"}}` (`version` is `null` after a delete) | The one namespace whose served ontology changed |
| Deploy-time rollout (CloudFormation custom resource, async) | `{"sweep": true, "trigger": "deploy", "image": "..."}` | **Every** `<prefix>-vkg-*` service in the cluster; runs on every deploy when the image comes from a CI tag, otherwise when the image URI changes |
| Scheduled sweep (EventBridge, weekly) | `{"sweep": true}` | **Every** `<prefix>-vkg-*` service in the cluster |

The ontology engine emits `ontology.deployed` once a deployment (or rollback) has
copied a version's VKG files to `ontologies/<namespace>/latest/`, and with
`version: null` once deleting the namespace's domain ontology has removed them.
For one release the rule also matches the pre-versioning `ontology.published`
event, so an older engine task still triggers a reload.

A sweep run only lists the services and invokes the Lambda once per namespace,
asynchronously, with `{"sweep_namespace": "<namespace>", "trigger": "..."}`, so
each namespace is reconciled (and retried) in its own invocation. The optional
`trigger` field (`deploy`, `schedule` by default, or any label such as `manual`)
is only logged.

### `latest/` decides what a run does

The event's `version` is only logged: events are asynchronous and retried, so a
delete's and a deploy's can arrive in either order. The Lambda reads `latest/`
instead, with the same test as `entrypoint.sh`: a depth-1 `*.ttl`/`*.owl`
ontology **and** a `*.obda`/`*.r2rml` mappings file. `schema.sql` is not part of
this check, but Ontop still needs its DDL for every mapped table: one without it
fails the whole namespace's load. A publish therefore fails when a mapped table is
missing from the source catalog; its error lists the remedies.

- **Loadable files: reload.** Register a new task-definition revision when the SSM
  image differs. When the image URI already matches, an `ontology.deployed` event
  forces a new deployment (so the task loads the deployed files) and a sweep first
  checks whether the service is already current (see below). Every deployment
  runs with the ECS circuit breaker + rollback. A service at zero is scaled back
  to one (autoscaling MinCapacity first); a missing service is provisioned.
- **No loadable files: scale to zero.** This happens when the domain ontology
  is deleted (`latest/` is cleared), and also when the deployed version has no
  mappings (deploying it removes `mappings.r2rml` from `latest/`). Lower the
  autoscaling MinCapacity to 0, then set the desired count to 0. A redeploy
  would start a degraded task (`/health` 503) that fails its health check and
  rolls back, leaving the old task serving what was there before. A missing,
  `INACTIVE` or `DRAINING` service is left alone and never created.

After each action the Lambda reads `latest/` again and acts again, up to 4 times,
until what it read matches what it did, so the invocation that writes last also
reads last. If `latest/` keeps changing it emits `ReloadFailed` and fails, and the
asynchronous retry reads it again later.

### When a sweep leaves a running service alone

A sweep compares image digests instead of URI text, because a tag such as
`{name}-latest` can be pushed again for a new build: it reads the digest of the
service's running tasks (`ecs:DescribeTasks`) and the digest the image URI
points at now (`ecr:DescribeImages`, or the `@sha256:` digest of a pinned URI).
The service is left alone and reported as `unchanged` only when both are known
and equal **and** every running task was created after the newest file VKG
loads from `latest/` was written. VKG reads `latest/` only when a task starts,
so an older task missed a reload: its `ontology.deployed` event was lost or
late. Otherwise, including when a digest or a time cannot be read, it forces a
new deployment, so ECS resolves the tag again and the new task loads `latest/`.
A service at zero is scaled back up whatever digest its stopping tasks still
report.

### Deploy-time rollout

A CDK deploy updates the shared template task definition and the SSM image
parameter, then the `<prefix>-vkg-image-rollout` custom resource invokes the
reload Lambda asynchronously with `{"sweep": true, "trigger": "deploy", ...}`.
The image URI is part of that payload, so CloudFormation calls it whenever the
URI changes (and once when the stack is created). A locally built image is
tagged with its content hash, so its URI changes exactly when the image does.
An image passed in by CI (`vkg_image_uri`) uses a tag that may be reused, so
the payload also carries a per-deploy id and the rollout runs on every deploy.
Either way the sweep's check (above) means a service already running the image
the deploy points at, and the files now in its `latest/`, is not restarted.
The invoke is fire-and-forget: the deploy does not wait
for the ECS rollouts, and a namespace whose VKG cannot start (for example
because of bad ontology artifacts) does not fail or roll back the deploy. A
task that never becomes healthy is rolled back by the circuit breaker, and an
ECS API error during the sweep raises the `ReloadFailed` alarm.

### Why the scheduled sweep exists

`ontology.deployed` only refreshes a namespace **when one of its versions is
deployed** (or its domain ontology is deleted), and the deploy-time rollout only
runs as part of a deploy.
The weekly sweep (EventBridge `rate(7 days)` → reload Lambda with
`{"sweep": true}`) is the backstop for everything else: a namespace whose
deploy-time rollout failed or was rolled back is tried again, so stale,
Inspector-flagged VKG images do not linger, and a lost or late event is
corrected. Like the deploy-time rollout, it reconciles every VKG service against
both `latest/` and the SSM image: loadable files and running: redeployed (a new
task definition when the image changed) unless its tasks already run the digest
the SSM image points at and were created after the files in `latest/` were
written (`unchanged`); loadable files and at zero: scaled back up; no files and
running: scaled to zero; no files and at zero: skipped. A single namespace's
failure is isolated so the rest are still patched.

### Manual sweep (on demand)

```bash
aws lambda invoke --function-name <prefix>-vkg-reload \
  --payload "$(printf '{"sweep":true,"trigger":"manual"}' | base64)" /tmp/out.json
cat /tmp/out.json   # -> {"status":"sweep_dispatched","total":N,"dispatched":M,...}
```

Each namespace's outcome (`reload_triggered`, `unchanged`, `scaled_to_zero`, ...)
is logged by its own invocation of the reload Lambda.

### IAM

The reload Lambda role holds, in addition to the per-namespace deploy
permissions, **`ecs:ListServices` scoped to the VKG cluster** — the sweep needs
it to enumerate `<prefix>-vkg-*` services. It is read-only and cluster-scoped
via the `ecs:cluster` condition key (`ecs:ListServices` has no IAM resource
type, so it is granted on `Resource: "*"` and constrained to this cluster by
condition).

For the digest comparison it also holds **`ecs:ListTasks`** (constrained to the
VKG cluster by the `ecs:cluster` condition, for the same reason as
`ecs:ListServices`), **`ecs:DescribeTasks` on this cluster's tasks**, and
**`ecr:DescribeImages` on the VKG image repository only**.

The deploy-time rollout runs as its own `<prefix>-vkg-image-rollout` function,
whose role holds only **`lambda:InvokeFunction` on the reload Lambda**.

### Observability

All three paths emit `ReloadTriggered` / `ReloadFailed` CloudWatch metrics under the
`COA/VKG` namespace (dimensioned by `Namespace` plus an undimensioned roll-up the
`ReloadFailed` alarm evaluates). Sweep-level failures — the SSM image can't be
resolved, or `ListServices` errors — emit `ReloadFailed` with namespace `sweep`
and end the run cleanly rather than raising uncaught.
