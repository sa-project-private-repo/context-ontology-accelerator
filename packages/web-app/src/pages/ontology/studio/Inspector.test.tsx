// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
  act,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ApiClient } from "@components/ApiClientProvider";
import { Inspector, type InspectorProps } from "./Inspector";
import type {
  StudioAboxState,
  StudioTerm,
  StudioTermHit,
  StudioTermProperty,
  StudioTermRef,
} from "../../../services/studio";

const listDomainInstances = vi.fn();
const getDomainInstance = vi.fn();
vi.mock("../../../services/ontology-engine", () => ({
  listDomainInstances: (...a: unknown[]) => listDomainInstances(...a),
  getDomainInstance: (...a: unknown[]) => getDomainInstance(...a),
}));

const api = {} as ApiClient;
const CUSTOMER = "http://e/o#Customer";
const PARTY = "http://e/o#Party";
const VIP = "http://e/o#VipCustomer";
const ORDER = "http://e/o#Order";
const EMAIL = "http://e/o#customer_email";
const PLACED = "http://e/o#placed";
const FIBO_AGENT = "https://fibo/Agent";
const FIBO_ORDER = "https://fibo/Order";
const XSD = "http://www.w3.org/2001/XMLSchema#";

const PARTY_REF: StudioTermRef = { iri: PARTY, label: "Party", declared: true };
const FIBO_REF: StudioTermRef = {
  iri: FIBO_AGENT,
  label: "FIBO Agent",
  declared: false,
};

const EMAIL_PROPERTY: StudioTermProperty = {
  iri: EMAIL,
  label: "email",
  kind: "datatypeProperty",
  direction: "outgoing",
  other: `${XSD}string`,
  otherLabel: "string",
  otherDeclared: false,
};

const CLASS_TERM: StudioTerm = {
  iri: CUSTOMER,
  kind: "class",
  label: "Customer",
  labels: [{ value: "Customer", lang: null }],
  altLabels: [],
  comment: "A buyer",
  editable: true,
  parents: [PARTY_REF],
  children: [],
  matches: [{ target: FIBO_AGENT, kind: "exact" }],
  properties: [EMAIL_PROPERTY],
  mappings: [
    { triplesMap: "http://e/o#TriplesMap_Customer", table: "customer" },
  ],
  abox: { aboxCount: 2, afterDeploy: false },
};

const PROPERTY_TERM: StudioTerm = {
  iri: EMAIL,
  kind: "datatypeProperty",
  label: "email",
  labels: [],
  altLabels: [],
  comment: "",
  editable: true,
  domain: [{ iri: CUSTOMER, label: "Customer", declared: true }],
  range: [{ iri: `${XSD}string`, label: "string", declared: false }],
  mappings: [{ triplesMap: "http://e/o#TriplesMap_Customer", column: "email" }],
};

/** An attribute no column backs (one the designer added): only its datatype can change. */
const UNMAPPED_PROPERTY_TERM: StudioTerm = { ...PROPERTY_TERM, mappings: [] };

const OBJECT_PROPERTY_TERM: StudioTerm = {
  iri: PLACED,
  kind: "objectProperty",
  label: "placed",
  labels: [],
  altLabels: [],
  comment: "",
  editable: true,
  domain: [{ iri: CUSTOMER, label: "Customer", declared: true }],
  range: [{ iri: ORDER, label: "Order", declared: true }],
  mappings: [],
};

const STALE_ABOX: StudioAboxState = {
  status: "READY",
  current: false,
  conformsToVersion: 2,
  activeVersion: 3,
  reason: "The domain data was built for v2; v3 is active",
};

/** Pick `option` in the Cloudscape Select labelled `label` (it opens on mousedown, picks on mouseup). */
function pickOption(label: RegExp, option: string) {
  fireEvent.mouseDown(screen.getByRole("button", { name: label }));
  const match = screen
    .getAllByRole("option")
    .find((o) => within(o).queryAllByText(option).length > 0);
  if (!match) throw new Error(`No option ${option}`);
  fireEvent.mouseUp(match);
}

type Rendered = InspectorProps & {
  rerender: (overrides: Partial<InspectorProps>) => void;
};

function renderInspector(overrides: Partial<InspectorProps> = {}): Rendered {
  let props: InspectorProps = {
    term: CLASS_TERM,
    loading: false,
    editable: true,
    busy: false,
    apiClient: api,
    namespace: "ns",
    onApply: vi.fn().mockResolvedValue(true),
    search: vi.fn().mockResolvedValue([]),
    onOpenTerm: vi.fn(),
    ...overrides,
  };
  const view = render(<Inspector {...props} />);
  return Object.assign(props, {
    rerender(next: Partial<InspectorProps>) {
      props = { ...props, ...next };
      view.rerender(<Inspector {...props} />);
    },
  });
}

/** The ops of the last `onApply` call; `toStrictEqual` on it also catches an extra key or an explicit undefined. */
function sent(p: InspectorProps): unknown {
  const calls = vi.mocked(p.onApply).mock.calls;
  return calls[calls.length - 1]?.[0];
}

/** Types in a designer term picker and picks the hit it offers. */
async function searchAndPick(
  p: InspectorProps,
  field: string,
  hit: StudioTermHit,
) {
  vi.mocked(p.search).mockResolvedValue([hit]);
  const input = screen.getByRole("combobox", { name: field });
  fireEvent.focus(input);
  // A query the label does not contain: highlighting a match splits the option's text.
  fireEvent.change(input, { target: { value: "zzz" } });
  fireEvent.mouseUp(
    await screen.findByRole("option", { name: new RegExp(hit.label) }),
  );
}

/** The action button of the Change IRI section; the section's own toggle has the same name. */
function changeIriButton(): HTMLButtonElement {
  const found = screen
    .getAllByRole("button", { name: "Change IRI" })
    .find((b) => !b.hasAttribute("aria-expanded"));
  if (!found) throw new Error("No Change IRI action button");
  return found as HTMLButtonElement;
}

function isDisabled(name: string | RegExp): boolean {
  return (screen.getByRole("button", { name }) as HTMLButtonElement).disabled;
}

describe("Inspector", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // A class with instances reads its examples as soon as it shows. Unless a
    // test is about them they never arrive, so no state changes after the test.
    listDomainInstances.mockReturnValue(new Promise(() => {}));
  });

  describe("states", () => {
    it("asks for a selection when nothing is selected", () => {
      renderInspector({ term: null });
      expect(screen.getByText(/Select a class in the graph/)).toBeTruthy();
    });

    it("shows no forms while a term is loading", () => {
      renderInspector({ loading: true });
      expect(screen.queryByLabelText("Label")).toBeNull();
      expect(screen.queryByText(/Select a class in the graph/)).toBeNull();
    });

    it("keeps nothing typed for one class when another class is selected", () => {
      const p = renderInspector();
      fireEvent.change(screen.getByLabelText("New attribute label"), {
        target: { value: "phone" },
      });
      p.rerender({ term: { ...CLASS_TERM, iri: VIP, label: "VIP customer" } });
      expect(
        (screen.getByLabelText("New attribute label") as HTMLInputElement)
          .value,
      ).toBe("");
      expect(listDomainInstances).toHaveBeenLastCalledWith(api, "ns", {
        classIri: VIP,
        maxResults: 5,
      });
    });

    it("starts the forms again for another term, and again when the saved values change", () => {
      const p = renderInspector();
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Typed for Customer" },
      });
      // Another term: nothing typed for Customer may reach it.
      p.rerender({ term: PROPERTY_TERM });
      expect((screen.getByLabelText("Label") as HTMLInputElement).value).toBe(
        "email",
      );
      // The same term, saved: the form shows what the draft now says.
      p.rerender({ term: { ...PROPERTY_TERM, label: "e-mail" } });
      expect((screen.getByLabelText("Label") as HTMLInputElement).value).toBe(
        "e-mail",
      );
    });

    it("keeps what was typed when the saved value changes under it, and follows the saved value otherwise", () => {
      const p = renderInspector();
      const label = () =>
        (screen.getByLabelText("Label") as HTMLInputElement).value;
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Clientele" },
      });
      // A later read of the same term, as after an undo: another label and description.
      p.rerender({
        term: {
          ...CLASS_TERM,
          label: "Client",
          labels: [{ value: "Client", lang: null }],
          comment: "A client",
        },
      });
      expect(label()).toBe("Clientele");
      expect(isDisabled("Save label")).toBe(false);
      // The description was not edited: it shows the draft's.
      expect(
        (screen.getByLabelText("Description") as HTMLTextAreaElement).value,
      ).toBe("A client");

      // Saved, with spaces the save trims: the typed label is the draft's now.
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Clientele " },
      });
      p.rerender({
        term: {
          ...CLASS_TERM,
          label: "Clientele",
          labels: [{ value: "Clientele", lang: null }],
        },
      });
      expect(isDisabled("Save label")).toBe(true);
      // So the label follows the draft again, here to the one an undo restores.
      p.rerender({ term: CLASS_TERM });
      expect(label()).toBe("Customer");
    });
  });

  describe("label, description and IRI", () => {
    it("saves a new label as set_label, without a lang when the label has none", () => {
      const p = renderInspector();
      expect(screen.queryByText(/^Language:/)).toBeNull();
      expect(isDisabled("Save label")).toBe(true);
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "  Client " },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save label" }));
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "set_label", iri: CUSTOMER, label: "Client" },
      ]);
    });

    it("saves the label in the language it is shown in", () => {
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          labels: [
            { value: "Customer", lang: "en" },
            { value: "고객", lang: "ko" },
          ],
        },
      });
      expect(screen.getByText("Language: en")).toBeTruthy();
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Client" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save label" }));
      expect(sent(p)).toStrictEqual([
        { op: "set_label", iri: CUSTOMER, label: "Client", lang: "en" },
      ]);
    });

    it("saves nothing for a blank label, or for text that only adds spaces", () => {
      renderInspector();
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "   " },
      });
      expect(isDisabled("Save label")).toBe(true);
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Customer  " },
      });
      expect(isDisabled("Save label")).toBe(true);
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: " A buyer " },
      });
      expect(isDisabled("Save description")).toBe(true);
    });

    it("saves the description as set_comment, and an empty one clears it", () => {
      const p = renderInspector();
      expect(isDisabled("Save description")).toBe(true);
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save description" }));
      expect(sent(p)).toStrictEqual([
        { op: "set_comment", iri: CUSTOMER, comment: "" },
      ]);
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Someone who buys" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save description" }));
      expect(sent(p)).toStrictEqual([
        { op: "set_comment", iri: CUSTOMER, comment: "Someone who buys" },
      ]);
    });

    it("changes the IRI with change_iri and newIri", () => {
      const p = renderInspector();
      expect(changeIriButton().disabled).toBe(true);
      fireEvent.change(screen.getByLabelText("New IRI"), {
        target: { value: " http://e/o#Client " },
      });
      fireEvent.click(changeIriButton());
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "change_iri", iri: CUSTOMER, newIri: "http://e/o#Client" },
      ]);
    });
  });

  describe("parents and subclasses", () => {
    it("removes a parent with remove_parent", () => {
      const p = renderInspector();
      fireEvent.click(
        screen.getByRole("button", { name: "Remove parent Party" }),
      );
      expect(sent(p)).toStrictEqual([
        { op: "remove_parent", iri: CUSTOMER, parent: PARTY },
      ]);
    });

    it("adds a parent picked from the draft's classes, then empties the picker", async () => {
      const p = renderInspector();
      await searchAndPick(p, "Parents", {
        iri: "http://e/o#Agent",
        label: "Agent",
        kind: "class",
      });
      expect(p.search).toHaveBeenCalledWith("zzz", "class");
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "add_parent", iri: CUSTOMER, parent: "http://e/o#Agent" },
      ]);
      await waitFor(() =>
        expect(
          (
            screen.getByRole("combobox", {
              name: "Parents",
            }) as HTMLInputElement
          ).value,
        ).toBe(""),
      );
    });

    it("empties the parent picker as soon as a parent is picked, applied or not", async () => {
      // A refused add (the class is already a parent) must not leave the
      // picked class in the box for a second pick that fails the same way.
      const p = renderInspector({ onApply: vi.fn().mockResolvedValue(false) });
      await searchAndPick(p, "Parents", {
        iri: "http://e/o#Agent",
        label: "Agent",
        kind: "class",
      });
      await waitFor(() =>
        expect(
          (
            screen.getByRole("combobox", {
              name: "Parents",
            }) as HTMLInputElement
          ).value,
        ).toBe(""),
      );
      expect(p.onApply).toHaveBeenCalledTimes(1);
    });

    it("links a declared parent and shows an undeclared one as plain text", () => {
      const p = renderInspector({
        term: { ...CLASS_TERM, parents: [PARTY_REF, FIBO_REF] },
      });
      fireEvent.click(screen.getByRole("button", { name: "Party" }));
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);
      expect(p.onOpenTerm).toHaveBeenCalledWith(PARTY);

      expect(screen.queryByRole("button", { name: "FIBO Agent" })).toBeNull();
      const external = screen.getByText("FIBO Agent");
      expect(external.getAttribute("title")).toBe(FIBO_AGENT);
      expect(external.closest("a, button")).toBeNull();
      fireEvent.click(external);
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);

      // An external parent can still be taken away.
      fireEvent.click(
        screen.getByRole("button", { name: "Remove parent FIBO Agent" }),
      );
      expect(sent(p)).toStrictEqual([
        { op: "remove_parent", iri: CUSTOMER, parent: FIBO_AGENT },
      ]);
    });

    it("links a declared subclass and shows an undeclared one as plain text", () => {
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          children: [
            { iri: VIP, label: "VIP customer", declared: true },
            { iri: "http://e/o#Loose", label: "Loose", declared: false },
          ],
        },
      });
      fireEvent.click(screen.getByRole("button", { name: /Subclasses \(2\)/ }));
      fireEvent.click(screen.getByRole("button", { name: "VIP customer" }));
      expect(p.onOpenTerm).toHaveBeenCalledWith(VIP);
      expect(screen.queryByRole("button", { name: "Loose" })).toBeNull();
      expect(screen.getByText("Loose").getAttribute("title")).toBe(
        "http://e/o#Loose",
      );
    });

    it("has no subclasses section for a class without subclasses", () => {
      renderInspector();
      expect(screen.queryByText(/Subclasses/)).toBeNull();
    });
  });

  describe("relationships and attributes", () => {
    it("deletes a property with delete_property", () => {
      const p = renderInspector();
      fireEvent.click(
        screen.getByRole("button", { name: "Delete property email" }),
      );
      expect(sent(p)).toStrictEqual([{ op: "delete_property", iri: EMAIL }]);
    });

    it("adds an attribute with add_datatype_property and clears its label once applied", async () => {
      const p = renderInspector();
      expect(isDisabled("Add attribute")).toBe(true);
      fireEvent.change(screen.getByLabelText("New attribute label"), {
        target: { value: " phone " },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add attribute" }));
      expect(sent(p)).toStrictEqual([
        {
          op: "add_datatype_property",
          label: "phone",
          domain: CUSTOMER,
          range: `${XSD}string`,
        },
      ]);
      await waitFor(() =>
        expect(
          (screen.getByLabelText("New attribute label") as HTMLInputElement)
            .value,
        ).toBe(""),
      );
    });

    it("sends the datatype picked for a new attribute", () => {
      const p = renderInspector();
      fireEvent.change(screen.getByLabelText("New attribute label"), {
        target: { value: "age" },
      });
      pickOption(/New attribute datatype/, "xsd:integer");
      fireEvent.click(screen.getByRole("button", { name: "Add attribute" }));
      expect(sent(p)).toStrictEqual([
        {
          op: "add_datatype_property",
          label: "age",
          domain: CUSTOMER,
          range: `${XSD}integer`,
        },
      ]);
    });

    it("keeps the typed attribute when the change was not applied", async () => {
      const p = renderInspector({
        onApply: vi.fn().mockResolvedValue(false),
      });
      fireEvent.change(screen.getByLabelText("New attribute label"), {
        target: { value: "phone" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add attribute" }));
      expect(p.onApply).toHaveBeenCalledTimes(1);
      await act(async () => {});
      expect(
        (screen.getByLabelText("New attribute label") as HTMLInputElement)
          .value,
      ).toBe("phone");
    });

    it("links the other end of a property only when the draft declares it", () => {
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          properties: [
            EMAIL_PROPERTY,
            {
              iri: PLACED,
              label: "placed",
              kind: "objectProperty",
              direction: "outgoing",
              other: ORDER,
              otherLabel: "Order",
              otherDeclared: true,
            },
            {
              iri: "http://e/o#bought",
              label: "bought",
              kind: "objectProperty",
              direction: "outgoing",
              other: FIBO_ORDER,
              otherLabel: "FIBO Order",
              otherDeclared: false,
            },
          ],
        },
      });
      // Declared: a link that opens the term.
      fireEvent.click(screen.getByRole("button", { name: "Order" }));
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);
      expect(p.onOpenTerm).toHaveBeenCalledWith(ORDER);
      // Undeclared: plain text, the IRI in its tooltip.
      for (const [label, iri] of [
        ["string", `${XSD}string`],
        ["FIBO Order", FIBO_ORDER],
      ]) {
        expect(screen.queryByRole("button", { name: label })).toBeNull();
        const text = screen.getByText(label);
        expect(text.getAttribute("title")).toBe(iri);
        fireEvent.click(text);
      }
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);
    });

    it("does not show the id of an anonymous class, nor link it", () => {
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          properties: [
            {
              iri: PLACED,
              label: "placed",
              kind: "objectProperty",
              direction: "outgoing",
              other: "N0c5f3a91d2",
              otherLabel: "N0c5f3a91d2",
              otherDeclared: false,
            },
            {
              iri: EMAIL,
              label: "email",
              kind: "datatypeProperty",
              direction: "outgoing",
              other: "N77aa",
              otherLabel: "N77aa",
              otherDeclared: false,
            },
          ],
        },
      });
      expect(screen.getByText(/\(anonymous class\)/)).toBeTruthy();
      expect(screen.getByText(/\(anonymous datatype\)/)).toBeTruthy();
      expect(screen.queryByText(/N0c5f3a91d2/)).toBeNull();
      expect(screen.queryByText(/N77aa/)).toBeNull();
      expect(screen.queryByRole("button", { name: /anonymous/ })).toBeNull();
      expect(p.onOpenTerm).not.toHaveBeenCalled();
    });
  });

  describe("grounding", () => {
    it("clears the grounding with kind none and no target", () => {
      const p = renderInspector();
      pickOption(/Grounding kind/, "No grounding");
      fireEvent.click(screen.getByRole("button", { name: "Save grounding" }));
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "set_match", iri: CUSTOMER, kind: "none" },
      ]);
    });

    it("grounds a class exactly to a target", () => {
      const p = renderInspector({
        term: { ...CLASS_TERM, matches: [] },
      });
      pickOption(/Grounding kind/, "Exact match");
      fireEvent.change(screen.getByLabelText("Grounding target"), {
        target: { value: " https://fibo/Person " },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save grounding" }));
      expect(sent(p)).toStrictEqual([
        {
          op: "set_match",
          iri: CUSTOMER,
          kind: "exact",
          target: "https://fibo/Person",
        },
      ]);
    });

    it("grounds a class closely to a target", () => {
      const p = renderInspector();
      pickOption(/Grounding kind/, "Close match");
      fireEvent.change(screen.getByLabelText("Grounding target"), {
        target: { value: "https://fibo/Party" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save grounding" }));
      expect(sent(p)).toStrictEqual([
        {
          op: "set_match",
          iri: CUSTOMER,
          kind: "close",
          target: "https://fibo/Party",
        },
      ]);
    });

    it("starts from the current grounding, and saves only a change or a complete one", () => {
      renderInspector();
      expect(
        (screen.getByLabelText("Grounding target") as HTMLInputElement).value,
      ).toBe(FIBO_AGENT);
      expect(isDisabled("Save grounding")).toBe(true);
      fireEvent.change(screen.getByLabelText("Grounding target"), {
        target: { value: "  " },
      });
      expect(isDisabled("Save grounding")).toBe(true);
      fireEvent.change(screen.getByLabelText("Grounding target"), {
        target: { value: "https://fibo/Other" },
      });
      expect(isDisabled("Save grounding")).toBe(false);
    });

    it("has nothing to save for a class with no grounding that stays without one", () => {
      renderInspector({ term: { ...CLASS_TERM, matches: [] } });
      expect(isDisabled("Save grounding")).toBe(true);
    });
  });

  describe("properties", () => {
    it("changes the domain with update_property and domain, and empties the picker", async () => {
      const p = renderInspector({ term: PROPERTY_TERM });
      await searchAndPick(p, "Domain", {
        iri: "http://e/o#Client",
        label: "Client",
        kind: "class",
      });
      expect(p.search).toHaveBeenCalledWith("zzz", "class");
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "update_property", iri: EMAIL, domain: "http://e/o#Client" },
      ]);
      await waitFor(() =>
        expect(
          (screen.getByRole("combobox", { name: "Domain" }) as HTMLInputElement)
            .value,
        ).toBe(""),
      );
    });

    it("keeps the picked class in the picker when the change was not applied", async () => {
      const p = renderInspector({
        term: PROPERTY_TERM,
        onApply: vi.fn().mockResolvedValue(false),
      });
      await searchAndPick(p, "Domain", {
        iri: "http://e/o#Client",
        label: "Client",
        kind: "class",
      });
      await act(async () => {});
      expect(
        (screen.getByRole("combobox", { name: "Domain" }) as HTMLInputElement)
          .value,
      ).toBe("Client");
    });

    it("changes an object property's range with update_property and range", async () => {
      const p = renderInspector({ term: OBJECT_PROPERTY_TERM });
      await searchAndPick(p, "Range", {
        iri: "http://e/o#Invoice",
        label: "Invoice",
        kind: "class",
      });
      expect(p.search).toHaveBeenCalledWith("zzz", "class");
      expect(p.onApply).toHaveBeenCalledTimes(1);
      expect(sent(p)).toStrictEqual([
        { op: "update_property", iri: PLACED, range: "http://e/o#Invoice" },
      ]);
      await act(async () => {});
    });

    it("has a class picker, not a datatype select, for the range of an object property", () => {
      renderInspector({ term: OBJECT_PROPERTY_TERM });
      expect(screen.getByRole("combobox", { name: "Range" })).toBeTruthy();
      expect(screen.queryByRole("button", { name: /^Datatype/ })).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Change datatype" }),
      ).toBeNull();
    });

    it("changes a datatype property's range and deletes it", () => {
      const p = renderInspector({ term: UNMAPPED_PROPERTY_TERM });
      expect(isDisabled("Change datatype")).toBe(true);
      pickOption(/^Datatype/, "xsd:integer");
      fireEvent.click(screen.getByRole("button", { name: "Change datatype" }));
      expect(sent(p)).toStrictEqual([
        { op: "update_property", iri: EMAIL, range: `${XSD}integer` },
      ]);
      fireEvent.click(screen.getByRole("button", { name: "Delete property" }));
      expect(p.onApply).toHaveBeenCalledTimes(2);
      expect(sent(p)).toStrictEqual([{ op: "delete_property", iri: EMAIL }]);
    });

    it("shows a range outside the offered datatypes as it is, not as xsd:string", () => {
      const p = renderInspector({
        term: {
          ...UNMAPPED_PROPERTY_TERM,
          range: [{ iri: `${XSD}float`, label: "float", declared: false }],
        },
      });
      expect(
        screen.getByRole("button", { name: /^Datatype/ }).textContent,
      ).toContain("xsd:float");
      expect(isDisabled("Change datatype")).toBe(true);
      pickOption(/^Datatype/, "xsd:string");
      fireEvent.click(screen.getByRole("button", { name: "Change datatype" }));
      expect(sent(p)).toStrictEqual([
        { op: "update_property", iri: EMAIL, range: `${XSD}string` },
      ]);
    });

    it("offers no datatype as selected for a property without a range", () => {
      const p = renderInspector({
        term: { ...UNMAPPED_PROPERTY_TERM, range: [] },
      });
      const select = screen.getByRole("button", { name: /^Datatype/ });
      expect(select.textContent).toContain("Choose a datatype");
      expect(select.textContent).not.toContain("xsd:string");
      expect(isDisabled("Change datatype")).toBe(true);
      pickOption(/^Datatype/, "xsd:string");
      fireEvent.click(screen.getByRole("button", { name: "Change datatype" }));
      expect(sent(p)).toStrictEqual([
        { op: "update_property", iri: EMAIL, range: `${XSD}string` },
      ]);
    });

    it("keeps the datatype of a mapped attribute, which follows its column", () => {
      renderInspector({ term: PROPERTY_TERM });
      expect(
        screen.getByText("The datatype follows column email."),
      ).toBeTruthy();
      expect(isDisabled("Change datatype")).toBe(true);
      const select = screen.getByRole("button", { name: /^Datatype/ });
      expect(
        select.getAttribute("aria-disabled") === "true" ||
          (select as HTMLButtonElement).disabled,
      ).toBe(true);
    });

    it("names the column as the engine does, without the mapping's SQL quotes", () => {
      // The inducers write rr:column as a quoted SQL identifier, and the term view passes it on.
      renderInspector({
        term: {
          ...PROPERTY_TERM,
          mappings: [
            { triplesMap: "http://e/o#TriplesMap_Customer", column: '"email"' },
          ],
        },
      });
      expect(
        screen.getByText("The datatype follows column email."),
      ).toBeTruthy();
    });

    it("says nothing about a column for an attribute no column backs", () => {
      renderInspector({ term: UNMAPPED_PROPERTY_TERM });
      expect(screen.queryByText(/The datatype follows column/)).toBeNull();
    });

    it("links the domain and range of a property only when the draft declares them", () => {
      const p = renderInspector({
        term: {
          ...OBJECT_PROPERTY_TERM,
          domain: [{ iri: CUSTOMER, label: "Customer", declared: true }],
          range: [{ iri: FIBO_ORDER, label: "FIBO Order", declared: false }],
        },
      });
      fireEvent.click(screen.getByRole("button", { name: "Customer" }));
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);
      expect(p.onOpenTerm).toHaveBeenCalledWith(CUSTOMER);
      expect(screen.queryByRole("button", { name: "FIBO Order" })).toBeNull();
      const external = screen.getByText("FIBO Order");
      expect(external.getAttribute("title")).toBe(FIBO_ORDER);
      fireEvent.click(external);
      expect(p.onOpenTerm).toHaveBeenCalledTimes(1);
    });

    it("links a declared range of an object property", () => {
      const p = renderInspector({ term: OBJECT_PROPERTY_TERM });
      fireEvent.click(screen.getByRole("button", { name: "Order" }));
      expect(p.onOpenTerm).toHaveBeenCalledWith(ORDER);
    });

    it("sends a label and a description for a property too", () => {
      const p = renderInspector({ term: PROPERTY_TERM });
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "e-mail" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save label" }));
      expect(sent(p)).toStrictEqual([
        { op: "set_label", iri: EMAIL, label: "e-mail" },
      ]);
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Where to write" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save description" }));
      expect(sent(p)).toStrictEqual([
        { op: "set_comment", iri: EMAIL, comment: "Where to write" },
      ]);
    });
  });

  describe("mappings", () => {
    it("shows a class's mapping, read only", () => {
      renderInspector();
      expect(
        screen.getByText(/TriplesMap_Customer \(table customer\)/),
      ).toBeTruthy();
    });

    it("names the table and the column as the Datatype hint does, without the mapping's SQL quotes", () => {
      // The inducers write rr:tableName and rr:column as quoted SQL identifiers.
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          mappings: [
            {
              triplesMap: "http://e/o#TriplesMap_Customer",
              table: '"sales"."customers"',
            },
          ],
        },
      });
      expect(
        screen.getByText("TriplesMap_Customer (table customers)"),
      ).toBeTruthy();
      p.rerender({
        term: {
          ...PROPERTY_TERM,
          mappings: [
            { triplesMap: "http://e/o#TriplesMap_Customer", column: '"email"' },
          ],
        },
      });
      expect(
        screen.getByText("TriplesMap_Customer (column email)"),
      ).toBeTruthy();
      expect(
        screen.getByText("The datatype follows column email."),
      ).toBeTruthy();
    });

    it("shows a property's mapping and says when there is none", () => {
      const p = renderInspector({ term: PROPERTY_TERM });
      expect(
        screen.getByText(/TriplesMap_Customer \(column email\)/),
      ).toBeTruthy();
      expect(
        screen.queryByText("Structured queries cannot use this term."),
      ).toBeNull();
      p.rerender({ term: { ...PROPERTY_TERM, mappings: [] } });
      expect(screen.getByText("Not mapped to a table")).toBeTruthy();
      expect(
        screen.getByText("Structured queries cannot use this term."),
      ).toBeTruthy();
    });

    it("says a class with subclasses is used only through one mapped to a table", () => {
      // As the Checks tab counts it: a parent of a mapped class is answered through it.
      const p = renderInspector({
        term: {
          ...CLASS_TERM,
          mappings: [],
          children: [{ iri: VIP, label: "VIP customer", declared: true }],
        },
      });
      expect(screen.getByText("Not mapped to a table")).toBeTruthy();
      expect(
        screen.getByText(
          "Structured queries can use this term only through a subclass mapped to a table.",
        ),
      ).toBeTruthy();
      expect(
        screen.queryByText("Structured queries cannot use this term."),
      ).toBeNull();
      p.rerender({ term: { ...CLASS_TERM, mappings: [], children: [] } });
      expect(
        screen.getByText("Structured queries cannot use this term."),
      ).toBeTruthy();
    });
  });

  describe("domain data", () => {
    const ACME = {
      iri: "http://e/data/1",
      classIri: CUSTOMER,
      classLabel: "Customer",
      label: "Acme",
      altLabels: [],
      evidenceCount: 3,
    };
    const INSTANCE = {
      iri: "http://e/data/1",
      classes: [],
      label: "Acme",
      altLabels: [],
      attributes: [],
      outgoing: [],
      incoming: [],
      evidence: [
        {
          chunkId: "c-1",
          docSourceId: "ds-1",
          sourceKey: "docs/contracts/acme.pdf",
          text: "Acme Corporation signed the supply contract.",
        },
      ],
    };
    const chunk = (n: number, text = `Chunk ${n}`) => ({
      chunkId: `c-${n}`,
      docSourceId: "ds-1",
      sourceKey: `docs/chunk-${n}.pdf`,
      text,
    });

    beforeEach(() => {
      listDomainInstances.mockResolvedValue({ instances: [ACME] });
    });

    it("shows the count, up to five examples and their evidence", async () => {
      getDomainInstance.mockResolvedValue(INSTANCE);
      renderInspector();
      expect(
        screen.getByText(/2 instances in the active version/),
      ).toBeTruthy();
      expect(await screen.findByText("Acme")).toBeTruthy();
      expect(listDomainInstances).toHaveBeenCalledWith(api, "ns", {
        classIri: CUSTOMER,
        maxResults: 5,
      });
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Acme" }),
      );
      expect(
        await screen.findByText("Acme Corporation signed the supply contract."),
      ).toBeTruthy();
      expect(screen.getByText("acme.pdf:")).toBeTruthy();
      expect(getDomainInstance).toHaveBeenCalledWith(
        api,
        "ns",
        "http://e/data/1",
      );
    });

    it("shows at most three evidence chunks, each cut at 300 characters", async () => {
      getDomainInstance.mockResolvedValue({
        ...INSTANCE,
        evidence: [chunk(1, "x".repeat(400)), chunk(2), chunk(3), chunk(4)],
      });
      renderInspector();
      await screen.findByText("Acme");
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Acme" }),
      );
      expect(await screen.findByText("chunk-1.pdf:")).toBeTruthy();
      expect(screen.getByText("chunk-2.pdf:")).toBeTruthy();
      expect(screen.getByText("chunk-3.pdf:")).toBeTruthy();
      expect(screen.queryByText("chunk-4.pdf:")).toBeNull();
      expect(screen.getByText(`${"x".repeat(300)}…`)).toBeTruthy();
    });

    it("hides the evidence again on a second click", async () => {
      getDomainInstance.mockResolvedValue(INSTANCE);
      renderInspector();
      await screen.findByText("Acme");
      const link = screen.getByRole("button", { name: "Evidence for Acme" });
      expect(link.getAttribute("aria-expanded")).toBe("false");
      fireEvent.click(link);
      await screen.findByText("acme.pdf:");
      expect(link.getAttribute("aria-expanded")).toBe("true");
      fireEvent.click(link);
      expect(screen.queryByText("acme.pdf:")).toBeNull();
      expect(link.getAttribute("aria-expanded")).toBe("false");
    });

    it("says so when an example has no evidence", async () => {
      getDomainInstance.mockResolvedValue({ ...INSTANCE, evidence: [] });
      renderInspector();
      await screen.findByText("Acme");
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Acme" }),
      );
      expect(await screen.findByText("No evidence")).toBeTruthy();
    });

    it("shows the evidence of the example clicked last, whichever answer comes last", async () => {
      let answerFirst!: (v: unknown) => void;
      listDomainInstances.mockResolvedValue({
        instances: [ACME, { ...ACME, iri: "http://e/data/2", label: "Birch" }],
      });
      getDomainInstance.mockImplementation(
        (_api: unknown, _ns: string, iri: string) =>
          iri === ACME.iri
            ? new Promise((resolve) => (answerFirst = resolve))
            : Promise.resolve({
                ...INSTANCE,
                iri,
                label: "Birch",
                evidence: [chunk(2, "Birch text")],
              }),
      );
      renderInspector();
      await screen.findByText("Birch");
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Acme" }),
      );
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Birch" }),
      );
      expect(await screen.findByText("Birch text")).toBeTruthy();
      await act(async () => answerFirst(INSTANCE));
      expect(screen.getByText("Birch text")).toBeTruthy();
      expect(screen.queryByText("acme.pdf:")).toBeNull();
    });

    it("keeps the examples when their evidence cannot be read", async () => {
      getDomainInstance.mockRejectedValue(new Error("Evidence is unavailable"));
      renderInspector();
      await screen.findByText("Acme");
      fireEvent.click(
        screen.getByRole("button", { name: "Evidence for Acme" }),
      );
      expect(await screen.findByText("Evidence is unavailable")).toBeTruthy();
      expect(screen.getByText("Acme")).toBeTruthy();
    });

    it("shows why the examples could not be read", async () => {
      listDomainInstances.mockRejectedValue(new Error("Domain data is down"));
      renderInspector();
      expect(await screen.findByText("Domain data is down")).toBeTruthy();
    });

    it("says None when the class has no example in the domain data", async () => {
      listDomainInstances.mockResolvedValue({ instances: [] });
      renderInspector();
      expect(await screen.findByText("None")).toBeTruthy();
    });

    it("reads no examples for a class without instances", () => {
      renderInspector({
        term: { ...CLASS_TERM, abox: { aboxCount: 0, afterDeploy: false } },
      });
      expect(
        screen.getByText(/0 instances in the active version/),
      ).toBeTruthy();
      expect(listDomainInstances).not.toHaveBeenCalled();
    });

    it("says the data is filled after deploy for a class only the draft has", () => {
      renderInspector({
        term: { ...CLASS_TERM, abox: { aboxCount: null, afterDeploy: true } },
      });
      expect(screen.getByText(/Filled after deploy/)).toBeTruthy();
      expect(listDomainInstances).not.toHaveBeenCalled();
    });

    it("has no domain data section when the namespace has no ABox, or for a property", () => {
      const p = renderInspector({ term: { ...CLASS_TERM, abox: null } });
      expect(screen.queryByText("Domain data")).toBeNull();
      p.rerender({ term: PROPERTY_TERM });
      expect(screen.queryByText("Domain data")).toBeNull();
      expect(listDomainInstances).not.toHaveBeenCalled();
    });

    it("warns that the data is out of date when the ABox is not current", async () => {
      renderInspector({ abox: STALE_ABOX });
      expect(
        screen.getByText(/The domain data was built for v2; v3 is active/),
      ).toBeTruthy();
      await screen.findByText("Acme");
    });

    it("gives no warning when the ABox is current, or unknown", async () => {
      const p = renderInspector({ abox: { ...STALE_ABOX, current: true } });
      await screen.findByText("Acme");
      expect(screen.queryByText(/Out of date/)).toBeNull();
      p.rerender({ abox: null });
      expect(screen.queryByText(/Out of date/)).toBeNull();
    });

    it("reads the examples again once the domain data is built for another version", async () => {
      const p = renderInspector({ abox: STALE_ABOX });
      await screen.findByText("Acme");
      listDomainInstances.mockResolvedValue({
        instances: [ACME, { ...ACME, iri: "http://e/data/2", label: "Birch" }],
      });
      // The same build (an edit of the class, say): the examples stay.
      p.rerender({ term: { ...CLASS_TERM }, abox: { ...STALE_ABOX } });
      expect(listDomainInstances).toHaveBeenCalledTimes(1);
      p.rerender({
        term: { ...CLASS_TERM, abox: { aboxCount: 3, afterDeploy: false } },
        abox: { ...STALE_ABOX, current: true, conformsToVersion: 3 },
      });
      expect(await screen.findByText("Birch")).toBeTruthy();
      expect(listDomainInstances).toHaveBeenCalledTimes(2);
    });
  });

  describe("when nothing may change", () => {
    it("disables every form while a change is being applied, and enables them otherwise", () => {
      const p = renderInspector({
        term: { ...CLASS_TERM, parents: [PARTY_REF], matches: [] },
      });
      // Give each form something to save, so only `busy` can hold it back.
      fireEvent.change(screen.getByLabelText("Label"), {
        target: { value: "Client" },
      });
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "Changed" },
      });
      fireEvent.change(screen.getByLabelText("New IRI"), {
        target: { value: "http://e/o#Client" },
      });
      fireEvent.change(screen.getByLabelText("New attribute label"), {
        target: { value: "phone" },
      });
      pickOption(/Grounding kind/, "Exact match");
      fireEvent.change(screen.getByLabelText("Grounding target"), {
        target: { value: "https://fibo/Person" },
      });
      const gated = [
        "Save label",
        "Save description",
        "Remove parent Party",
        "Delete property email",
        "Add attribute",
        "Save grounding",
      ];
      for (const name of gated) expect(isDisabled(name)).toBe(false);
      expect(changeIriButton().disabled).toBe(false);
      expect(
        (screen.getByRole("combobox", { name: "Parents" }) as HTMLInputElement)
          .disabled,
      ).toBe(false);

      p.rerender({ busy: true });
      for (const name of gated) expect(isDisabled(name)).toBe(true);
      expect(changeIriButton().disabled).toBe(true);
      expect(
        (screen.getByRole("combobox", { name: "Parents" }) as HTMLInputElement)
          .disabled,
      ).toBe(true);
      expect(
        (screen.getByLabelText("Label") as HTMLInputElement).disabled,
      ).toBe(true);
    });

    it("disables every form of a term that cannot change, and says why", () => {
      renderInspector({ editable: false });
      expect(screen.getByText(/read-only here/)).toBeTruthy();
      for (const name of [
        "Remove parent Party",
        "Delete property email",
        "Save grounding",
      ]) {
        expect(isDisabled(name)).toBe(true);
      }
      expect(changeIriButton().disabled).toBe(true);
      expect(
        (screen.getByLabelText("Label") as HTMLInputElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByLabelText("Description") as HTMLTextAreaElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByRole("combobox", { name: "Parents" }) as HTMLInputElement)
          .disabled,
      ).toBe(true);
    });

    it("disables the property forms too", () => {
      renderInspector({ term: PROPERTY_TERM, editable: false });
      expect(isDisabled("Delete property")).toBe(true);
      expect(isDisabled("Change datatype")).toBe(true);
      expect(
        (screen.getByRole("combobox", { name: "Domain" }) as HTMLInputElement)
          .disabled,
      ).toBe(true);
    });

    it("keeps links to related terms usable", () => {
      const p = renderInspector({ editable: false });
      fireEvent.click(screen.getByRole("button", { name: "Party" }));
      expect(p.onOpenTerm).toHaveBeenCalledWith(PARTY);
    });
  });
});
