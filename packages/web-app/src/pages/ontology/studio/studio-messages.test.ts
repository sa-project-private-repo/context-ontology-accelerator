// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { formatAppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";
import type { StudioMessage } from "../../../services/studio";
import {
  errorSentence,
  findingMessage,
  studioMessageLines,
  studioMessageText,
} from "./studio-messages";

const english: AppTranslate = (message, values) =>
  formatAppMessage("en", message, values);
const japanese: AppTranslate = (message, values) =>
  formatAppMessage("ja", message, values);

/** Every code the engine sends (``studio/messages.py`` and the data checks), with values for each placeholder. */
const ENGINE_CODES: StudioMessage[] = [
  { code: "OPENED_DRAFT", params: { version: "3" } },
  { code: "OPENED_DRAFT_NO_VERSION", params: {} },
  { code: "UNDID_REVISION", params: { revision: "4" } },
  { code: "REBASED", params: { version: "5" } },
  { code: "REBASED_NO_VERSION", params: {} },
  { code: "ADD_CLASS", params: { label: "Supplier" } },
  { code: "SET_LABEL", params: { term: "Customer", label: "Client" } },
  { code: "SET_COMMENT", params: { term: "Customer" } },
  { code: "CHANGE_IRI", params: { term: "Customer", newTerm: "Client" } },
  { code: "DELETE_CLASS_DETACH", params: { term: "Customer" } },
  { code: "DELETE_CLASS_DELETE", params: { term: "Customer" } },
  { code: "MERGE_CLASSES", params: { source: "Vip", target: "Customer" } },
  { code: "ADD_PARENT", params: { term: "Item", parent: "Party" } },
  { code: "REMOVE_PARENT", params: { term: "Item", parent: "Party" } },
  {
    code: "ADD_OBJECT_PROPERTY",
    params: { label: "contains", domain: "Order", range: "Item" },
  },
  { code: "ADD_DATATYPE_PROPERTY", params: { label: "sku", domain: "Item" } },
  { code: "UPDATE_PROPERTY", params: { fields: "range", term: "placedBy" } },
  { code: "DELETE_PROPERTY", params: { term: "placedBy" } },
  { code: "REMOVE_MATCH", params: { term: "Item" } },
  { code: "SET_EXACT_MATCH", params: { term: "Item", target: "Product" } },
  { code: "SET_CLOSE_MATCH", params: { term: "Item", target: "Product" } },
  { code: "REPLACE_DATATYPE", params: { old: "decimal", new: "double" } },
  { code: "RESTORE_TRIPLES", params: {} },
  { code: "DANGLING_DOMAIN", params: { property: "total", domain: "Bill" } },
  { code: "DANGLING_RANGE", params: { property: "placed by", range: "Ghost" } },
  {
    code: "UNKNOWN_DATATYPE_RANGE",
    params: { property: "total", range: "xsd:money" },
  },
  {
    code: "DATATYPE_RANGE_NOT_DATATYPE",
    params: { property: "email", range: "Party" },
  },
  { code: "SUBCLASS_CYCLE", params: { classes: "A -> B -> A" } },
  { code: "MISSING_LABEL", params: { term: "Item" } },
  { code: "DUPLICATE_CLASS_LABEL", params: { count: "2", label: "Customer" } },
  {
    code: "DUPLICATE_RELATIONSHIP_LABEL",
    params: { count: "2", label: "owns" },
  },
  { code: "DUPLICATE_ATTRIBUTE_LABEL", params: { count: "3", label: "email" } },
  { code: "DUPLICATE_TERM_LABEL", params: { count: "2", label: "x" } },
  { code: "ISOLATED_CLASS", params: { term: "Ward" } },
  {
    code: "MERGE_MAPPED_TABLES",
    params: {
      source: "Vip",
      target: "Customer",
      sourceTables: "vip",
      targetTables: "customer",
    },
  },
  {
    code: "DOMAIN_ON_PARENT_TABLE",
    params: {
      property: "email",
      tables: "customer",
      domain: "Party",
      mappedClass: "Customer",
    },
  },
  {
    code: "DOMAIN_ON_PARENT_MAP",
    params: {
      property: "email",
      map: "TriplesMap_Customer",
      domain: "Party",
      mappedClass: "Customer",
    },
  },
  {
    code: "RANGE_ON_PARENT",
    params: { property: "placedBy", mappedClass: "Customer", range: "Party" },
  },
  {
    code: "RANGE_UNCONFIRMED",
    params: { property: "placedBy", range: "Item" },
  },
  {
    code: "ANOTHER_CLASS_LABELLED",
    params: { label: "Customer", iri: "http://e/o#Client" },
  },
  {
    code: "ANOTHER_RELATIONSHIP_LABELLED",
    params: { label: "owns", iri: "http://e/o#has" },
  },
  {
    code: "ANOTHER_ATTRIBUTE_LABELLED",
    params: { label: "email", iri: "http://e/o#mail" },
  },
  { code: "TABLES_UNREADABLE", params: {} },
  { code: "ABOX_UNREADABLE", params: {} },
  { code: "MAPPINGS_UNREADABLE", params: {} },
  { code: "METRICS_UNCHECKED", params: {} },
  { code: "BASE_MAPPING_UNREADABLE", params: {} },
];

describe("studioMessageText", () => {
  it("writes a history line and a finding in the user's language", () => {
    const added = { code: "ADD_CLASS", params: { label: "Supplier" } };
    expect(studioMessageText(english, added, "x")).toBe("Added class Supplier");
    expect(studioMessageText(japanese, added, "x")).toBe(
      "クラス Supplier を追加しました",
    );
    const dangling = {
      code: "DANGLING_RANGE",
      params: { property: "placed by", range: "Ghost" },
    };
    expect(studioMessageText(english, dangling, "x")).toBe(
      "The range of placed by is Ghost, which is not a class of the draft",
    );
    expect(studioMessageText(japanese, dangling, "x")).toBe(
      "placed by の範囲 Ghost はドラフトのクラスではありません",
    );
  });

  it("uses the catalogue's words for a grounding removed and for properties kept", () => {
    // As the catalogue words them elsewhere: グラウンディングを解除, and 保持する in the delete dialog.
    expect(
      studioMessageText(
        japanese,
        { code: "REMOVE_MATCH", params: { term: "Item" } },
        "x",
      ),
    ).toBe("Item のグラウンディングを解除しました");
    expect(
      studioMessageText(
        japanese,
        { code: "DELETE_CLASS_DETACH", params: { term: "Legacy" } },
        "x",
      ),
    ).toBe("クラス Legacy を削除しました（プロパティは保持）");
  });

  it("knows every code the engine sends", () => {
    for (const message of ENGINE_CODES) {
      expect(
        studioMessageText(japanese, message, "FALLBACK"),
        message.code,
      ).not.toBe("FALLBACK");
    }
  });

  it("names the changed fields of a property in the user's language", () => {
    const changed = {
      code: "UPDATE_PROPERTY",
      params: { fields: "domain, label, comment", term: "customer_email" },
    };
    expect(studioMessageText(english, changed, "x")).toBe(
      "Changed the domain, label, comment of customer_email",
    );
    expect(studioMessageText(japanese, changed, "x")).toBe(
      "customer_email のドメイン、ラベル、説明を変更しました",
    );
    // An attribute's datatype, as the range of an attribute is named.
    const datatype = {
      code: "UPDATE_PROPERTY",
      params: { fields: "datatype, label", term: "customer_email" },
    };
    expect(studioMessageText(english, datatype, "x")).toBe(
      "Changed the datatype, label of customer_email",
    );
    expect(studioMessageText(japanese, datatype, "x")).toBe(
      "customer_email のデータ型、ラベルを変更しました",
    );
    const unknown = { ...changed, params: { ...changed.params, fields: "x" } };
    expect(studioMessageText(japanese, unknown, "the engine's")).toBe(
      "the engine's",
    );
  });

  it("falls back to the engine's English for an unknown code, a missing value or no code", () => {
    const fallback = "Added class Supplier";
    expect(
      studioMessageText(japanese, { code: "NEW_CODE", params: {} }, fallback),
    ).toBe(fallback);
    expect(
      studioMessageText(
        japanese,
        { code: "constructor", params: {} },
        fallback,
      ),
    ).toBe(fallback);
    expect(
      studioMessageText(japanese, { code: "ADD_CLASS", params: {} }, fallback),
    ).toBe(fallback);
    expect(studioMessageText(japanese, undefined, fallback)).toBe(fallback);
    expect(studioMessageText(japanese, null, fallback)).toBe(fallback);
  });

  it("reads a finding's code and a data section's error code", () => {
    const finding = {
      code: "MISSING_LABEL",
      severity: "warning" as const,
      message: "Item has no label",
      iris: [],
    };
    expect(findingMessage(finding)).toBeNull();
    expect(
      findingMessage({
        ...finding,
        messageCode: "MISSING_LABEL",
        messageParams: { term: "Item" },
      }),
    ).toEqual({ code: "MISSING_LABEL", params: { term: "Item" } });
    expect(errorSentence(undefined)).toBeNull();
    expect(
      studioMessageText(
        japanese,
        errorSentence("MAPPINGS_UNREADABLE"),
        "The document mappings could not be read.",
      ),
    ).toBe("ドキュメントのマッピングを読み取れませんでした。");
  });
});

describe("studioMessageLines", () => {
  it("writes each coded line of a summary or of warnings, and keeps the others", () => {
    const warning = {
      code: "DOMAIN_ON_PARENT_TABLE",
      params: {
        property: "customer_email",
        tables: "customer",
        domain: "Party",
        mappedClass: "Customer",
      },
    };
    const lines = [
      "customer_email is mapped from table customer; with the domain Party, SPARQL through the VKG no longer offers it on Customer, but NL-to-SQL still does.",
      "A later warning",
    ];
    expect(studioMessageLines(japanese, [warning], lines)).toEqual([
      "customer_email はテーブル customer からマッピングされています。ドメインを Party にすると、VKG 経由の SPARQL では Customer で使えなくなりますが、NL-to-SQL では引き続き使えます。",
      "A later warning",
    ]);
    expect(studioMessageLines(english, [warning], lines)).toEqual(lines);
    // A revision or change set stored before the codes.
    expect(studioMessageLines(japanese, null, lines)).toEqual(lines);
    expect(studioMessageLines(japanese, undefined, [])).toEqual([]);
  });
});
