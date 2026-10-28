// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The sentences of the designer's check findings, data checks, change history
 * and change warnings. The engine sends each as a stable code and the values
 * it names next to its English text (`message`, `error`, `summary`,
 * `warnings`); this writes the code's catalogue entry in the user's language
 * instead. A code it does not know, a value its sentence misses, or an entry
 * stored before the codes shows the engine's English text.
 */
import type { AppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";
import type { StudioFinding, StudioMessage } from "../../../services/studio";

/** Each code's sentence: the engine's English (`studio/messages.py`), with the same placeholders. */
const SENTENCES: ReadonlyMap<string, AppMessage> = new Map<string, AppMessage>([
  // The lines of a history entry.
  ["OPENED_DRAFT", "Opened the draft on v{version}"],
  ["OPENED_DRAFT_NO_VERSION", "Opened the draft on no version"],
  ["UNDID_REVISION", "Undid revision {revision}"],
  ["REBASED", "Rebased onto v{version}"],
  ["REBASED_NO_VERSION", "Rebased onto no version"],
  ["ADD_CLASS", "Added class {label}"],
  ["SET_LABEL", "Renamed {term} to {label}"],
  ["SET_COMMENT", "Changed the description of {term}"],
  ["CHANGE_IRI", "Changed the IRI of {term} to {newTerm}"],
  ["DELETE_CLASS_DETACH", "Deleted class {term} (detach its properties)"],
  ["DELETE_CLASS_DELETE", "Deleted class {term} (delete its properties)"],
  ["MERGE_CLASSES", "Merged {source} into {target}"],
  ["ADD_PARENT", "Made {term} a subclass of {parent}"],
  ["REMOVE_PARENT", "Removed {parent} from the parents of {term}"],
  [
    "ADD_OBJECT_PROPERTY",
    "Added relationship {label} from {domain} to {range}",
  ],
  ["ADD_DATATYPE_PROPERTY", "Added attribute {label} to {domain}"],
  ["UPDATE_PROPERTY", "Changed the {fields} of {term}"],
  ["DELETE_PROPERTY", "Deleted property {term}"],
  ["REMOVE_MATCH", "Removed the grounding of {term}"],
  ["SET_EXACT_MATCH", "Grounded {term} to {target} (exact)"],
  ["SET_CLOSE_MATCH", "Grounded {term} to {target} (close)"],
  ["REPLACE_DATATYPE", "Replaced the datatype {old} with {new}"],
  ["RESTORE_TRIPLES", "Reverted a change"],
  // The instant findings.
  [
    "DANGLING_DOMAIN",
    "The domain of {property} is {domain}, which is not a class of the draft",
  ],
  [
    "DANGLING_RANGE",
    "The range of {property} is {range}, which is not a class of the draft",
  ],
  [
    "UNKNOWN_DATATYPE_RANGE",
    "The range of the attribute {property} is {range}, which is not a datatype the Ontology designer accepts",
  ],
  [
    "DATATYPE_RANGE_NOT_DATATYPE",
    "The attribute {property} has the class {range} as its range, not a datatype",
  ],
  ["SUBCLASS_CYCLE", "These classes are subclasses of each other: {classes}"],
  ["MISSING_LABEL", "{term} has no label"],
  ["DUPLICATE_CLASS_LABEL", "{count} classes share the label '{label}'"],
  [
    "DUPLICATE_RELATIONSHIP_LABEL",
    "{count} relationships share the label '{label}'",
  ],
  ["DUPLICATE_ATTRIBUTE_LABEL", "{count} attributes share the label '{label}'"],
  ["DUPLICATE_TERM_LABEL", "{count} terms share the label '{label}'"],
  ["ISOLATED_CLASS", "{term} has no relationship, parent or attribute"],
  // The warnings of a change.
  [
    "MERGE_MAPPED_TABLES",
    "{source} and {target} are mapped to tables {sourceTables} and {targetTables}; structured SQL will describe them as one table.",
  ],
  [
    "DOMAIN_ON_PARENT_TABLE",
    "{property} is mapped from table {tables}; with the domain {domain}, SPARQL through the VKG no longer offers it on {mappedClass}, but NL-to-SQL still does.",
  ],
  [
    "DOMAIN_ON_PARENT_MAP",
    "{property} is mapped by {map}; with the domain {domain}, SPARQL through the VKG no longer offers it on {mappedClass}, but NL-to-SQL still does.",
  ],
  [
    "RANGE_ON_PARENT",
    "{property} joins to {mappedClass} in the mapping; with the range {range}, SPARQL through the VKG no longer offers it as a join to {mappedClass}, but NL-to-SQL still does.",
  ],
  [
    "RANGE_UNCONFIRMED",
    "{property} is mapped through an IRI template, so the mapping cannot confirm its range {range}",
  ],
  [
    "DOMAIN_GIVEN_BACK_TABLE",
    "{deleted} was the domain of {property}, which is mapped from table {tables}; its domain is now {classes}.",
  ],
  [
    "DOMAIN_GIVEN_BACK_MAP",
    "{deleted} was the domain of {property}, which is mapped by {map}; its domain is now {classes}.",
  ],
  [
    "RANGE_GIVEN_BACK",
    "{deleted} was the range of {property}, which joins to {classes} in the mapping; its range is now {classes}.",
  ],
  ["ANOTHER_CLASS_LABELLED", "Another class is also labelled '{label}': {iri}"],
  [
    "ANOTHER_RELATIONSHIP_LABELLED",
    "Another relationship is also labelled '{label}': {iri}",
  ],
  [
    "ANOTHER_ATTRIBUTE_LABELLED",
    "Another attribute is also labelled '{label}': {iri}",
  ],
  // The errors of the data checks' sections.
  ["TABLES_UNREADABLE", "The source catalog could not be read."],
  ["ABOX_UNREADABLE", "The domain data could not be read."],
  ["MAPPINGS_UNREADABLE", "The document mappings could not be read."],
  [
    "METRICS_UNCHECKED",
    "Breaking metrics could not be checked; the graph database may be unreachable.",
  ],
  // The structured check's error.
  [
    "BASE_MAPPING_UNREADABLE",
    "The table mappings of the version the draft was built on could not be read.",
  ],
]);

/** The fields `UPDATE_PROPERTY` names in `fields`, joined by ", ". */
const FIELDS: ReadonlyMap<string, AppMessage> = new Map<string, AppMessage>([
  ["domain", "domain"],
  ["range", "range"],
  ["datatype", "datatype"],
  ["label", "label"],
  ["comment", "comment"],
]);

const PLACEHOLDER = /\{([A-Za-z][A-Za-z0-9_]*)\}/g;

/**
 * `message` in the user's language, or `english`, the engine's text, when
 * there is no message, its code is not one of these, or a value is missing.
 */
export function studioMessageText(
  t: AppTranslate,
  message: StudioMessage | null | undefined,
  english: string,
): string {
  const sentence = message && SENTENCES.get(message.code);
  if (!message || !sentence) return english;
  const values = new Map(Object.entries(message.params));
  for (const [, name] of sentence.matchAll(PLACEHOLDER)) {
    if (!values.has(name)) return english;
  }
  const fields = values.get("fields");
  if (message.code === "UPDATE_PROPERTY" && fields !== undefined) {
    const words = fields.split(", ").map((field) => FIELDS.get(field));
    if (words.some((word) => word === undefined)) return english;
    values.set(
      "fields",
      words
        .flatMap((word) => (word ? [t(word)] : []))
        .reduce((items, item) => t("{items}, {item}", { items, item })),
    );
  }
  return t(sentence, Object.fromEntries(values));
}

/**
 * Each of `english`, the engine's lines (a summary, or warnings), in the
 * user's language when `messages`, their codes in the same order, codes it.
 */
export function studioMessageLines(
  t: AppTranslate,
  messages: StudioMessage[] | null | undefined,
  english: string[],
): string[] {
  return english.map((line, i) => studioMessageText(t, messages?.[i], line));
}

/** The coded sentence of a finding; none from an engine that does not send it. */
export function findingMessage(finding: StudioFinding): StudioMessage | null {
  return finding.messageCode && finding.messageParams
    ? { code: finding.messageCode, params: finding.messageParams }
    : null;
}

/** The coded sentence of a data section's error; none without its code. */
export function errorSentence(
  code: string | null | undefined,
): StudioMessage | null {
  return code ? { code, params: {} } : null;
}
