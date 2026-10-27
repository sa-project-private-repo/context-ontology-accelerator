// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import {
  mapTraceStatus,
  humanizeStepId,
  STEP_LABELS,
  STEP_DISPLAY_NAMES,
  CEDAR_STEP_IDS,
  FIREWALL_STEP_IDS,
} from "./step-labels";

describe("mapTraceStatus", () => {
  it("maps success-like statuses to success", () => {
    for (const s of ["success", "completed", "ok", "allow"]) {
      expect(mapTraceStatus(s)).toBe("success");
    }
  });

  it("maps hard-failure statuses to error", () => {
    for (const s of ["error", "denied", "failed"]) {
      expect(mapTraceStatus(s)).toBe("error");
    }
  });

  it("maps degraded-but-continued statuses to warning", () => {
    for (const s of ["retry", "retrying", "timeout", "degraded"]) {
      expect(mapTraceStatus(s)).toBe("warning");
    }
  });

  it("maps routine not-applicable statuses to stopped (muted)", () => {
    for (const s of [
      "skipped",
      "miss",
      "empty",
      "multi_metric_bypass",
      "residual_qualifier_bypass",
    ]) {
      expect(mapTraceStatus(s)).toBe("stopped");
    }
  });

  it("falls back to info for unknown statuses", () => {
    expect(mapTraceStatus("something_else")).toBe("info");
  });
});

describe("humanizeStepId", () => {
  it("strips tier prefix and title-cases", () => {
    expect(humanizeStepId("t3.vector_search")).toBe("Vector Search");
    expect(humanizeStepId("t2.vkg.compile")).toBe("Vkg Compile");
  });
});

describe("label maps cover the boundary-control step IDs", () => {
  it("every Cedar/Firewall step ID has a display name", () => {
    for (const id of [...CEDAR_STEP_IDS, ...FIREWALL_STEP_IDS]) {
      expect(STEP_DISPLAY_NAMES[id]).toBeTruthy();
    }
  });

  it("labels the bare domain-ABox step the Tier 3 lexical path emits", () => {
    expect(STEP_LABELS.domain_abox).toBe("Domain facts");
    expect(STEP_DISPLAY_NAMES.domain_abox).toBe("Domain facts");
  });

  it("live STEP_LABELS and plan STEP_DISPLAY_NAMES share the core tier IDs", () => {
    for (const id of [
      "t1.metric_match",
      "t2.sql.generate",
      "t2.vkg.translate",
      "t3.synthesize",
    ]) {
      expect(STEP_LABELS[id]).toBeTruthy();
      expect(STEP_DISPLAY_NAMES[id]).toBeTruthy();
    }
  });
});

describe("agentic mode trace rendering", () => {
  // The agentic loop emits BARE step IDs (no t1./t2./t3. prefix). Without explicit
  // entries the entire agentic trace renders unlabeled, so this asserts the set
  // stays in sync with the Python trace.record() call sites.
  const AGENTIC_STEP_IDS = [
    "agentic_session_start",
    "decompose",
    "subquestion_start",
    "next_step_select",
    "tools_gated",
    "ontology_resolved",
    "ontology_edge_filter",
    "present_edges_discovered",
    "exploration_branch",
    "no_progress_step",
    "ad_hoc_query",
    "agentic_working_answer",
    "agentic_final_semantic_search",
    "agentic_retrieval_stop",
    "llm_synthesize",
  ];

  it("every agentic step ID has a live label", () => {
    for (const id of AGENTIC_STEP_IDS) {
      expect(STEP_LABELS[id]).toBeTruthy();
    }
  });

  it('maps "succeeded" to success, not the info default', () => {
    // The controller/retriever record "succeeded" (not "success") on the paths that
    // report a tool actually working; falling through to `default` rendered every
    // successful agentic tool call as a neutral info dot.
    expect(mapTraceStatus("succeeded")).toBe("success");
    expect(mapTraceStatus("success")).toBe("success");
  });

  it("maps agentic per-step degradations to warning", () => {
    expect(mapTraceStatus("timeout")).toBe("warning");
    expect(mapTraceStatus("unknown_tool")).toBe("warning");
    expect(mapTraceStatus("invalid_input")).toBe("warning");
  });
});
