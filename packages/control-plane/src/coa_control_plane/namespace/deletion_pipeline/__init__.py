# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace deletion Step Functions pipeline — Lambda task handlers.

Each module here is invoked as a Step Functions ``LambdaInvoke`` task. The
state machine definition lives in CDK
(``infra/lib/stacks/services/namespace-deletion-pipeline.ts``).

Pipeline order (see pipeline construct for the full state machine):
  1. delete_sources       — stop the domain ABox builds, then delete every
                             source via the real sources-api delete path
                             (S3 / OpenSearch / DataZone / Glue cleanup
                             happens inside that Lambda, not here)
  2. delete_metrics       — bulk-delete every metric via the real
                             metric-api delete path (Neptune + OpenSearch)
  3. delete_ontology      — ontology-engine full teardown + S3 artifacts
  4. delete_platform      — VKG service, Athena workgroup, DataZone project,
                             role mappings, roles
  5. finalize             — delete namespace DDB record + name reservation,
                             status -> DELETED
  (any step failure -> mark_failed sets status -> DELETE_FAILED)
"""
