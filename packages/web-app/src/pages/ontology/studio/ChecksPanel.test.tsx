// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, it, expect, vi, beforeEach } from "vitest";
import type { ApiClient } from "@components/ApiClientProvider";
import type { ProposalValidationJob } from "../../../services/ontology-engine";
import type {
  StudioChecks,
  StudioStructuredCheck,
  StudioStructuredGroup,
} from "../../../services/studio";
import { formatAppMessage } from "../../../i18n/appMessages";
import {
  LocaleProvider,
  type AppTranslate,
} from "../../../i18n/LocaleProvider";
import {
  ChecksPanel,
  MAX_FAILED_POLLS,
  VALIDATION_POLL_MS,
  askAboutFindings,
} from "./ChecksPanel";

const english: AppTranslate = (message, values) =>
  formatAppMessage("en", message, values);

const studio = vi.hoisted(() => ({ getStudioChecks: vi.fn() }));
const engine = vi.hoisted(() => ({
  validateProposal: vi.fn(),
  getProposalValidationJob: vi.fn(),
}));
vi.mock("../../../services/studio", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../services/studio")>()),
  getStudioChecks: (...a: unknown[]) => studio.getStudioChecks(...a),
}));
vi.mock("../../../services/ontology-engine", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("../../../services/ontology-engine")
  >()),
  validateProposal: (...a: unknown[]) => engine.validateProposal(...a),
  getProposalValidationJob: (...a: unknown[]) =>
    engine.getProposalValidationJob(...a),
}));

const api = {} as ApiClient;
const ORDER = "http://e/o#Order";
const GHOST = "http://e/o#Ghost";

const CHECKS: StudioChecks = {
  revision: 4,
  instant: {
    findings: [
      {
        code: "DANGLING_RANGE",
        severity: "error",
        message:
          "The range of placed by is Ghost, which is not a class of the draft",
        iris: [GHOST, ORDER],
      },
      {
        code: "ISOLATED_CLASS",
        severity: "info",
        message: "Item has no relationship, parent or attribute",
        iris: ["http://e/o#Item"],
      },
    ],
    counts: { error: 1, warning: 0, info: 1 },
    truncated: false,
  },
  data: {
    tables: { checked: true, missing: ["orders"], error: null },
    abox: {
      available: true,
      current: true,
      impacts: [
        {
          classIri: ORDER,
          instances: 12,
          change: "renamed",
          toIri: "http://e/o#PurchaseOrder",
        },
      ],
      error: null,
    },
    mappings: { error: "The document mappings could not be read." },
    metrics: { breaking: [], error: null },
  },
};

const NONE: StudioStructuredGroup = { count: 0, terms: [] };
const CLEAR: StudioStructuredCheck = {
  unmappedClasses: NONE,
  unmappedProperties: NONE,
  labelDiffers: NONE,
  multiTableClasses: NONE,
  mapsNoTable: false,
  error: null,
};
const PREMIUM = "http://e/o#PremiumCustomer";
const CUSTOMER = "http://e/o#Customer";

function withStructured(structured: StudioStructuredCheck): StudioChecks {
  return { ...CHECKS, structured };
}

function renderPanel(
  overrides: Partial<React.ComponentProps<typeof ChecksPanel>> = {},
) {
  const props = {
    apiClient: api,
    namespace: "ns",
    proposalId: "p-1",
    revision: 4,
    editable: true,
    onOpenTerm: vi.fn(),
    onAsk: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<ChecksPanel {...props} />) };
}

const RUNNING: ProposalValidationJob = {
  job_id: "j-1",
  proposal_id: "p-1",
  status: "running",
  created_at: "",
};
const PASSED: ProposalValidationJob = {
  ...RUNNING,
  status: "completed",
  report: {
    job_id: "j-1",
    proposal_id: "p-1",
    passed: true,
    findings: [],
    tiers_run: ["1"],
    created_at: "",
  },
};

/** Advance fake timers inside act, so the state updates of the polls they fire are flushed. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function runButton(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: "Run full validation",
  }) as HTMLButtonElement;
}

/** Disabled, or busy: Cloudscape marks a loading button aria-disabled. */
function isDisabled(button: HTMLButtonElement): boolean {
  return button.disabled || button.getAttribute("aria-disabled") === "true";
}

/**
 * Click Run full validation inside an async act. The started job is set when
 * validateProposal settles, after the click's own act: left to React's
 * scheduler, it and its poll timer could come after advance() has fixed its
 * window of fake time, and the poll would fall outside it.
 */
async function clickRun() {
  await act(async () => {
    fireEvent.click(runButton());
  });
}

/** Opens Full validation and starts it. */
async function runFullValidation() {
  await screen.findByText("Checks of revision 4");
  fireEvent.click(screen.getByText("Full validation"));
  await clickRun();
  expect(engine.validateProposal).toHaveBeenCalled();
}

describe("ChecksPanel", () => {
  beforeEach(() => {
    studio.getStudioChecks.mockReset().mockResolvedValue(CHECKS);
    engine.validateProposal.mockReset();
    engine.getProposalValidationJob.mockReset();
  });

  it("lists the findings and the data sections, each with its own error", async () => {
    renderPanel();
    expect(
      await screen.findByText(/which is not a class of the draft/),
    ).toBeTruthy();
    expect(screen.getByText("Checks of revision 4")).toBeTruthy();
    expect(screen.getByText("1 error")).toBeTruthy();
    expect(screen.getByText(/Not in the source catalog: orders/)).toBeTruthy();
    expect(screen.getByText(/renamed to PurchaseOrder/)).toBeTruthy();
    expect(
      screen.getByText("The document mappings could not be read."),
    ).toBeTruthy();
    expect(screen.getByText("No metric would break.")).toBeTruthy();
  });

  it("lists what structured queries cannot answer, with the terms of each finding", async () => {
    studio.getStudioChecks.mockResolvedValue(
      withStructured({
        ...CLEAR,
        unmappedClasses: {
          count: 2,
          terms: [
            { iri: "http://e/o#LoyaltyProgramme", label: "Loyalty programme" },
            { iri: PREMIUM, label: "Premium customer" },
          ],
        },
        unmappedProperties: {
          count: 1,
          terms: [
            {
              iri: "http://e/o#customers_loyaltyTier",
              label: "loyalty tier",
              domainIri: CUSTOMER,
            },
          ],
        },
        labelDiffers: {
          count: 1,
          terms: [
            { iri: CUSTOMER, label: "Client", physicalName: "customers" },
          ],
        },
        multiTableClasses: {
          count: 1,
          terms: [
            { iri: ORDER, label: "Order", tables: ["orders", "returns"] },
          ],
        },
      }),
    );
    const { props } = renderPanel();
    expect(await screen.findByText("Structured queries")).toBeTruthy();
    expect(
      screen.getByText(
        "2 classes have no table mapping. Structured queries cannot answer them; facts extracted from documents can fill them after deploy.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "1 attribute or relationship of a mapped class, or relationship into one, has no column or join behind it. Structured queries do not use it.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "1 mapped term has a label that differs from its table or column name. Structured queries keep using the table or column name.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "1 class is mapped from more than one table; structured queries treat it as one table.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Client (customers)")).toBeTruthy();
    expect(screen.getByText("Order (orders, returns)")).toBeTruthy();
    expect(screen.getByText("loyalty tier")).toBeTruthy();
    fireEvent.click(screen.getByText("Premium customer"));
    expect(props.onOpenTerm).toHaveBeenCalledWith(PREMIUM);
    expect(screen.queryByText(/structured queries are off/)).toBeNull();
    expect(screen.queryByText(/^Every class has a table mapping/)).toBeNull();
  });

  it("lists the mapped classes and properties the draft deletes, with their tables and columns, as text", async () => {
    studio.getStudioChecks.mockResolvedValue(
      withStructured({
        ...CLEAR,
        removedMappedClasses: {
          count: 1,
          terms: [{ iri: ORDER, label: "orders", tables: ["orders"] }],
        },
        removedMappedProperties: {
          count: 2,
          terms: [
            {
              iri: "http://e/o#customers_signupDate",
              label: "signup_date",
              domainIri: CUSTOMER,
              physicalName: "signup_date",
            },
            {
              iri: "http://e/o#orders_customerId",
              label: "customer_id",
              domainIri: ORDER,
            },
          ],
        },
      }),
    );
    const { props } = renderPanel();
    expect(
      await screen.findByText(
        "1 mapped class is deleted; structured queries no longer answer from its tables.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "2 mapped attributes or relationships are deleted; structured queries no longer use their columns or joins.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("customer_id")).toBeTruthy();
    // The draft no longer has them, so they are not links to the Inspector.
    fireEvent.click(screen.getByText("orders (orders)"));
    fireEvent.click(screen.getByText("signup_date (signup_date)"));
    expect(props.onOpenTerm).not.toHaveBeenCalled();
    expect(screen.queryByText(/^Every class has a table mapping/)).toBeNull();
  });

  it("wraps each line between words, not inside a word", async () => {
    // A StatusIndicator breaks its text between any two letters: "no longer u / se its column".
    studio.getStudioChecks.mockResolvedValue(
      withStructured({
        ...CLEAR,
        removedMappedProperties: {
          count: 1,
          terms: [
            {
              iri: "http://e/o#customers_signupDate",
              label: "signup_date",
              domainIri: CUSTOMER,
            },
          ],
        },
      }),
    );
    renderPanel();
    const lines = [
      await screen.findByText(
        "1 mapped attribute or relationship is deleted; structured queries no longer use its column or join.",
      ),
      screen.getByText(
        "The range of placed by is Ghost, which is not a class of the draft",
      ),
      screen.getByText("Not in the source catalog: orders"),
      screen.getByText(/has 12 instances and is renamed to PurchaseOrder/),
      screen.getByText("The document mappings could not be read."),
      screen.getByText("No metric would break."),
    ];
    for (const line of lines) {
      expect(line.tagName).toBe("SPAN");
      expect(line).toHaveStyle({
        wordBreak: "normal",
        overflowWrap: "anywhere",
      });
    }
  });

  it("links the first 20 terms of a structured finding and counts the others", async () => {
    const terms = Array.from({ length: 25 }, (_, i) => ({
      iri: `http://e/o#C${i}`,
      label: `C${i}`,
    }));
    studio.getStudioChecks.mockResolvedValue(
      withStructured({
        ...CLEAR,
        unmappedClasses: { count: 130, terms },
      }),
    );
    renderPanel();
    expect(
      await screen.findByText(/^130 classes have no table mapping/),
    ).toBeTruthy();
    expect(screen.getByText("C19")).toBeTruthy();
    expect(screen.queryByText("C20")).toBeNull();
    expect(screen.getByText("and 110 more")).toBeTruthy();
  });

  it("warns when the draft maps no table, and says when every term is mapped", async () => {
    studio.getStudioChecks.mockResolvedValue(
      withStructured({ ...CLEAR, mapsNoTable: true }),
    );
    const { props, rerender } = renderPanel();
    expect(
      await screen.findByText(
        "This draft maps no table; structured queries are off after deploy.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/^Every class has a table mapping/)).toBeNull();
    studio.getStudioChecks.mockResolvedValue(withStructured(CLEAR));
    rerender(<ChecksPanel {...props} revision={5} />);
    expect(
      await screen.findByText(
        "Every class has a table mapping, and so do the attributes and relationships of mapped classes and the relationships into them.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/structured queries are off/)).toBeNull();
  });

  it("shows the structured check's error as the engine wrote it", async () => {
    const error =
      "The table mappings of the version the draft was built on could not be read.";
    studio.getStudioChecks.mockResolvedValue(
      withStructured({ ...CLEAR, error }),
    );
    renderPanel();
    expect(await screen.findByText(error)).toBeTruthy();
  });

  it("leaves the structured section out for an engine that does not send it", async () => {
    renderPanel();
    await screen.findByText("Checks of revision 4");
    expect(screen.queryByText("Structured queries")).toBeNull();
  });

  it("says that no document mapping would go stale", async () => {
    studio.getStudioChecks.mockResolvedValue({
      ...CHECKS,
      data: {
        ...CHECKS.data,
        mappings: { available: true, stale: 0, staleTargets: [], error: null },
      },
    });
    renderPanel();
    expect(
      await screen.findByText("No document mapping would go stale."),
    ).toBeTruthy();
  });

  it("names as many stale mappings as it counts", async () => {
    const targets = [
      ORDER,
      ORDER,
      ...Array.from({ length: 48 }, (_, i) => `http://e/o#T${i}`),
    ];
    studio.getStudioChecks.mockResolvedValue({
      ...CHECKS,
      data: {
        ...CHECKS.data,
        // Two rows map to Order; the list is cut at 50 of the 53 rows.
        mappings: { available: true, stale: 53, staleTargets: targets },
      },
    });
    renderPanel();
    const line = await screen.findByText(/53 mappings would go stale/);
    expect(line.textContent).toMatch(/: Order \(2\), T0, T1, /);
    expect(line.textContent).toMatch(/, T47, \+3 more$/);
  });

  it("says a class with instances is merged into the class kept", async () => {
    studio.getStudioChecks.mockResolvedValue({
      ...CHECKS,
      data: {
        ...CHECKS.data,
        abox: {
          ...CHECKS.data.abox,
          impacts: [
            {
              classIri: "http://e/o#Patient",
              instances: 4,
              change: "merged",
              toIri: "http://e/o#Person",
            },
            {
              classIri: "http://e/o#Disease",
              instances: 1,
              change: "removed",
              toIri: null,
            },
          ],
        },
      },
    });
    renderPanel();
    expect(
      await screen.findByText(/has 4 instances and is merged into Person\./),
    ).toBeTruthy();
    expect(screen.getByText(/has 1 instance and is removed\./)).toBeTruthy();
    expect(screen.queryByText(/renamed/)).toBeNull();
  });

  it("opens a finding's term and asks the assistant about the problems", async () => {
    const { props } = renderPanel();
    fireEvent.click(await screen.findByText("Ghost"));
    expect(props.onOpenTerm).toHaveBeenCalledWith(GHOST);
    fireEvent.click(screen.getByRole("button", { name: "Ask the assistant" }));
    const text = (props.onAsk as ReturnType<typeof vi.fn>).mock
      .calls[0][0] as string;
    expect(text).toContain("which is not a class of the draft");
    expect(text).not.toContain("Item has no relationship"); // info is not a problem
  });

  it("does not offer to ask the assistant on a read-only draft", async () => {
    // Its chat takes no message.
    renderPanel({ editable: false });
    await screen.findByText(/which is not a class of the draft/);
    expect(
      isDisabled(
        screen.getByRole("button", {
          name: "Ask the assistant",
        }) as HTMLButtonElement,
      ),
    ).toBe(true);
  });

  it("checks again for a new revision", async () => {
    const { rerender, props } = renderPanel();
    await screen.findByText("Checks of revision 4");
    rerender(<ChecksPanel {...props} revision={5} />);
    await waitFor(() =>
      expect(studio.getStudioChecks).toHaveBeenCalledTimes(2),
    );
  });

  it("runs the full validation and lists its findings", async () => {
    engine.validateProposal.mockResolvedValue({
      job_id: "j-1",
      proposal_id: "p-1",
      status: "running",
      created_at: "",
    });
    engine.getProposalValidationJob.mockResolvedValue({
      job_id: "j-1",
      proposal_id: "p-1",
      status: "completed",
      created_at: "",
      report: {
        job_id: "j-1",
        proposal_id: "p-1",
        passed: false,
        findings: [
          {
            validator: "shacl",
            tier: "1",
            severity: "error",
            code: "SHACL_VIOLATION",
            message: "Order needs a total",
          },
        ],
        tiers_run: ["1"],
        created_at: "",
      },
    });
    renderPanel();
    await screen.findByText("Checks of revision 4");
    fireEvent.click(screen.getByText("Full validation"));
    fireEvent.click(
      screen.getByRole("button", { name: "Run full validation" }),
    );
    expect(
      await screen.findByText("Order needs a total", {}, { timeout: 4000 }),
    ).toBeTruthy();
    expect(engine.validateProposal).toHaveBeenCalledWith(api, "ns", "p-1");
    expect(engine.getProposalValidationJob).toHaveBeenCalledWith(
      api,
      "ns",
      "p-1",
      "j-1",
    );
  });

  it("keeps polling the full validation through a failed read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      engine.validateProposal.mockResolvedValue(RUNNING);
      engine.getProposalValidationJob
        .mockRejectedValueOnce(new Error("Failed to fetch"))
        .mockResolvedValue(PASSED);
      renderPanel();
      await runFullValidation();
      await advance(VALIDATION_POLL_MS);
      expect(screen.getByText("Failed to fetch")).toBeTruthy();
      expect(isDisabled(runButton())).toBe(true);
      // The job finished on the server meanwhile: the next read brings it.
      await advance(VALIDATION_POLL_MS);
      expect(screen.getByText("The validation passed.")).toBeTruthy();
      expect(screen.queryByText("Failed to fetch")).toBeNull();
      expect(engine.getProposalValidationJob).toHaveBeenCalledTimes(2);
      expect(isDisabled(runButton())).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives a job up after failed reads in a row, so another one can start", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      engine.validateProposal.mockResolvedValue(RUNNING);
      engine.getProposalValidationJob.mockRejectedValue(
        new Error("Service Unavailable"),
      );
      renderPanel();
      await runFullValidation();
      for (let n = 0; n < MAX_FAILED_POLLS + 3; n++)
        await advance(VALIDATION_POLL_MS);
      expect(engine.getProposalValidationJob).toHaveBeenCalledTimes(
        MAX_FAILED_POLLS,
      );
      expect(
        screen.getByText("The validation's status could not be read"),
      ).toBeTruthy();
      expect(isDisabled(runButton())).toBe(false);
      await clickRun();
      expect(engine.validateProposal).toHaveBeenCalledTimes(2);
      expect(
        screen.queryByText("The validation's status could not be read"),
      ).toBeNull();
      // The new job is polled from a fresh count.
      await advance(VALIDATION_POLL_MS);
      expect(engine.getProposalValidationJob).toHaveBeenCalledTimes(
        MAX_FAILED_POLLS + 1,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a full validation is out of date once the draft moved on", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      engine.validateProposal.mockResolvedValue(RUNNING);
      engine.getProposalValidationJob.mockResolvedValue(PASSED);
      const { props, rerender } = renderPanel();
      await runFullValidation();
      await advance(VALIDATION_POLL_MS);
      expect(screen.getByText("The validation passed.")).toBeTruthy();
      expect(screen.queryByText(/out of date/)).toBeNull();
      // An Undo, or an edit from the graph, while the Checks tab is open.
      rerender(<ChecksPanel {...props} revision={5} />);
      expect(
        screen.getByText(
          "Validation of revision 4, out of date: run it again.",
        ),
      ).toBeTruthy();
      // A run of the revision on screen is current again.
      await clickRun();
      expect(engine.validateProposal).toHaveBeenCalledTimes(2);
      await advance(VALIDATION_POLL_MS);
      expect(screen.getByText("The validation passed.")).toBeTruthy();
      expect(screen.queryByText(/out of date/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a validation that was running when the draft moved on is out of date", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      engine.validateProposal.mockResolvedValue(RUNNING);
      // Each read is a new object, as a parsed response is.
      engine.getProposalValidationJob
        .mockResolvedValueOnce({ ...RUNNING })
        .mockResolvedValue(PASSED);
      const { props, rerender } = renderPanel();
      await runFullValidation();
      await advance(VALIDATION_POLL_MS);
      rerender(<ChecksPanel {...props} revision={5} />);
      await advance(VALIDATION_POLL_MS);
      expect(screen.getByText("The validation passed.")).toBeTruthy();
      expect(
        screen.getByText(
          "Validation of revision 4, out of date: run it again.",
        ),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  describe("in Japanese", () => {
    afterEach(() => {
      window.localStorage.clear();
      document.documentElement.lang = "";
    });

    it("writes coded findings and section errors in Japanese, and the others as the engine wrote them", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      const [dangling, isolated] = CHECKS.instant.findings;
      studio.getStudioChecks.mockResolvedValue({
        ...CHECKS,
        instant: {
          ...CHECKS.instant,
          findings: [
            {
              ...dangling,
              messageCode: "DANGLING_RANGE",
              messageParams: { property: "placed by", range: "Ghost" },
            },
            // A code this app does not know yet keeps the engine's English.
            { ...isolated, messageCode: "A_LATER_CODE", messageParams: {} },
          ],
        },
        data: {
          ...CHECKS.data,
          mappings: {
            error: "The document mappings could not be read.",
            errorCode: "MAPPINGS_UNREADABLE",
          },
          metrics: {
            error:
              "Breaking metrics could not be checked; the graph database may be unreachable.",
          },
        },
      });
      const onAsk = vi.fn();
      render(
        <LocaleProvider>
          <ChecksPanel
            apiClient={api}
            namespace="ns"
            proposalId="p-1"
            revision={4}
            editable
            onOpenTerm={vi.fn()}
            onAsk={onAsk}
          />
        </LocaleProvider>,
      );
      expect(
        await screen.findByText(
          "placed by の範囲 Ghost はドラフトのクラスではありません",
        ),
      ).toBeTruthy();
      expect(
        screen.getByText("Item has no relationship, parent or attribute"),
      ).toBeTruthy();
      expect(
        screen.getByText("ドキュメントのマッピングを読み取れませんでした。"),
      ).toBeTruthy();
      // From an engine that sends no code, the error stays as it wrote it.
      expect(
        screen.getByText(
          "Breaking metrics could not be checked; the graph database may be unreachable.",
        ),
      ).toBeTruthy();
      fireEvent.click(
        screen.getByRole("button", { name: "アシスタントに質問" }),
      );
      expect(onAsk.mock.calls[0][0]).toContain(
        "- placed by の範囲 Ghost はドラフトのクラスではありません",
      );
    });

    it("wraps a Japanese line as it wraps an English one", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      studio.getStudioChecks.mockResolvedValue(
        withStructured({
          ...CLEAR,
          removedMappedProperties: {
            count: 1,
            terms: [
              {
                iri: "http://e/o#customers_signupDate",
                label: "signup_date",
                domainIri: CUSTOMER,
              },
            ],
          },
        }),
      );
      render(
        <LocaleProvider>
          <ChecksPanel
            apiClient={api}
            namespace="ns"
            proposalId="p-1"
            revision={4}
            editable
            onOpenTerm={vi.fn()}
            onAsk={vi.fn()}
          />
        </LocaleProvider>,
      );
      const line = await screen.findByText(
        "マッピング済みの 1 件の属性またはリレーションシップが削除されます。構造化クエリはその列や結合を使用しなくなります。",
      );
      expect(line.tagName).toBe("SPAN");
      expect(line).toHaveStyle({
        wordBreak: "normal",
        overflowWrap: "anywhere",
      });
    });

    it("writes the structured check's error in Japanese by its code", async () => {
      window.localStorage.setItem("scl.locale", "ja");
      studio.getStudioChecks.mockResolvedValue(
        withStructured({
          ...CLEAR,
          error:
            "The table mappings of the version the draft was built on could not be read.",
          errorCode: "BASE_MAPPING_UNREADABLE",
        }),
      );
      render(
        <LocaleProvider>
          <ChecksPanel
            apiClient={api}
            namespace="ns"
            proposalId="p-1"
            revision={4}
            editable
            onOpenTerm={vi.fn()}
            onAsk={vi.fn()}
          />
        </LocaleProvider>,
      );
      expect(
        await screen.findByText(
          "ドラフトがベースにしているバージョンのテーブルマッピングを読み取れませんでした。",
        ),
      ).toBeTruthy();
    });
  });

  it("names at most ten findings in a message", () => {
    const many = Array.from({ length: 15 }, (_, n) => ({
      code: "MISSING_LABEL",
      severity: "warning" as const,
      message: `T${n} has no label`,
      iris: [],
    }));
    const text = askAboutFindings(many, english);
    expect(text).toContain("T9 has no label");
    expect(text).not.toContain("T10 has no label");
  });
});
