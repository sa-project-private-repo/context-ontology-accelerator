// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Ontology designer: the editable draft of a proposal and its typed change sets, the chat with the
// assistant, whose change sets the user previews and applies, and the checks, the query console and
// the competency questions.
//
// The engine answers every error as `{"detail": {"code", "message", ...}}`. The shared error shapes
// carry only `message`, so each operation documents the `code`s and extra fields it can return. A 422
// `detail` can also be FastAPI's own list of `{type, loc, msg}` when a body field is missing or mistyped.
$version: "2"

namespace com.amazon.semanticcontext.graphstudio

use com.amazon.semanticcontext.common#ClientToken
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#PaginationToken
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError
use com.amazon.semanticcontext.ontologygraph#BreakingMetricList
use com.amazon.semanticcontext.ontologygraph#VertexIri

/// Kind of a draft term.
enum StudioTermKind {
    /// An `owl:Class`.
    CLASS = "class"

    /// An `owl:ObjectProperty` (a relationship between classes).
    OBJECT_PROPERTY = "objectProperty"

    /// An `owl:DatatypeProperty` (an attribute with an XSD range).
    DATATYPE_PROPERTY = "datatypeProperty"
}

/// Kind of an edge of the draft graph.
enum StudioEdgeKind {
    /// Child `rdfs:subClassOf` parent.
    SUBCLASS_OF = "subClassOf"

    /// An object property from its domain class to its range class.
    OBJECT_PROPERTY = "objectProperty"
}

/// Whether the graph shows every class or the neighbourhood of one.
enum StudioGraphMode {
    /// Every class of the draft.
    FULL = "full"

    /// One class and its neighbours up to two hops (always above 150 classes).
    FOCUS = "focus"
}

/// What produced a revision of the draft.
enum StudioChangeKind {
    /// The draft was opened (revision 0, or the first free number).
    OPEN = "open"

    /// A change set was applied.
    APPLY = "apply"

    /// The newest change set in effect was undone.
    UNDO = "undo"

    /// An undone change set was applied again.
    REDO = "redo"

    /// The draft was rebuilt on a newer version and its user ops replayed.
    REBASE = "rebase"
}

/// Kind of a grounding match.
enum StudioMatchKind {
    /// `skos:exactMatch`.
    EXACT = "exact"

    /// `skos:closeMatch`.
    CLOSE = "close"
}

/// How a class left a draft that an accept would publish.
enum StudioRenameKind {
    /// Its IRI was changed (`change_iri`).
    RENAMED = "renamed"

    /// It was merged into another class (`merge_classes`).
    MERGED = "merged"
}

/// Who produced a revision.
enum StudioChangeSource {
    /// A direct edit, an undo, a redo or a rebase.
    USER = "user"

    /// A change set the assistant proposed and the user applied.
    AGENT = "agent"
}

/// Status of an assistant turn (one message and the work on it).
enum StudioTurnStatus {
    /// The assistant is still working; poll the events.
    RUNNING = "running"

    /// The assistant answered.
    COMPLETED = "completed"

    /// The turn reached its tool or time budget and answered with what it had.
    LIMITED = "limited"

    /// The turn stopped on an error, or its worker died.
    FAILED = "failed"
}

/// Kind of a chat event.
enum StudioChatEventType {
    /// The user's message (`text`, `author`, `selection`).
    USER_MESSAGE = "user_message"

    /// What the assistant said while it called tools (`text`).
    THINKING = "thinking"

    /// A tool call (`tool`, `input`).
    TOOL_CALL = "tool_call"

    /// A tool's result (`tool`, `status`, `summary`).
    TOOL_RESULT = "tool_result"

    /// A change set the assistant proposed (`changeSetId`, `opSummaries`).
    CHANGE_SET = "change_set"

    /// The assistant's answer (`text`).
    ANSWER = "answer"

    /// A query the assistant ran (`language`, `target`, `query`, `rowCount`); the chat offers to open it in the query console.
    QUERY = "query"

    /// The end of the turn (`status`, `message`).
    TURN_ENDED = "turn_ended"
}

/// Status of a change set the assistant proposed.
enum StudioChangeSetStatus {
    /// Waiting for the user.
    PENDING = "pending"

    /// Applied as a revision (all of its ops or some).
    APPLIED = "applied"

    /// Discarded by the user.
    DISCARDED = "discarded"
}

/// What a change set preview marks on a node or edge.
enum StudioPreviewMark {
    /// The change set adds it.
    ADDED = "added"

    /// The change set changes it (for a class: its own facts or one of its attributes).
    CHANGED = "changed"

    /// The change set removes it.
    REMOVED = "removed"
}

/// Id of a change set the assistant proposed; ids sort by time.
@length(min: 1, max: 64)
@pattern("^[0-9A-Za-z-]{1,64}$")
string StudioChangeSetId

/// Strings such as warnings, IRIs or op ids.
list StudioStringList {
    member: String
}

/// IRIs of draft terms.
list StudioIriList {
    member: VertexIri
}

/// A draft revision number. Revisions count up from 0, the one the draft was opened at.
@range(min: 0, max: 1000000000)
integer StudioRevision

/// An op id: `"{revision}:{index}"`, the op at `index` of the change set that made `revision`.
@pattern("^[0-9]+:[0-9]+$")
@length(min: 3, max: 32)
string StudioOpId

/// Op ids.
@length(max: 1000)
list StudioOpIdList {
    member: StudioOpId
}

/// The position of an op in a change set the assistant proposed.
@range(min: 0, max: 49)
integer StudioOpIndex

/// Op indexes of a change set the assistant proposed: 1 to 50, as a change set has at most 50 ops.
@length(min: 1, max: 50)
list StudioOpIndexList {
    member: StudioOpIndex
}

/// Typed change ops, applied in order: 1 to 100.
@length(min: 1, max: 100)
list StudioOpList {
    /// A typed change op, a JSON object whose `op` is one of the 14 user ops: `add_class`, `set_label`, `set_comment`, `change_iri`, `delete_class`, `merge_classes`, `add_parent`, `remove_parent`, `add_object_property`, `add_datatype_property`, `update_property`, `delete_property`, `set_match` or `replace_datatype`. `replace_datatype` takes `from`, any datatype IRI or malformed token such as `xsd:datetime`, and `to`, a datatype; it rewrites every use of `from` in ranges, `rr:datatype` and typed literals. Each op is parsed strictly (an unknown `op` or field is a 422), a label is at most 200 characters and a comment at most 4000, and the ops of one request are at most 2097152 bytes once serialized as JSON (a 422 beyond).
    @tags(["bounded-at-runtime"])
    member: Document
}

/// Term counts of the draft.
structure StudioDraftCounts {
    /// Classes.
    @required
    classes: Integer

    /// Object properties.
    @required
    objectProperties: Integer

    /// Datatype properties.
    @required
    datatypeProperties: Integer

    /// R2RML TriplesMaps.
    @required
    triplesMaps: Integer
}

/// The state of a proposal's draft.
@mixin
structure StudioDraftView {
    /// Proposal id.
    @required
    proposalId: String

    /// Proposal status (`pending`, `updated`, `accept_failed`, `accepting`, `accepted`, ...).
    @required
    status: String

    /// True while the proposal's status (`pending`, `updated` or `accept_failed`) allows edits.
    @required
    editable: Boolean

    /// Current revision; every write names it as `expectedRevision`.
    @required
    revision: Integer

    /// Version the draft was built on; absent when there was none.
    baseVersion: Integer

    /// Newest published version now; absent when there is none.
    headVersion: Integer

    /// True when a newer version was published after the draft was built; accept needs a rebase first.
    @required
    rebaseRequired: Boolean

    /// Ontology id the draft's versions belong to.
    @required
    ontologyId: String

    /// Term counts.
    @required
    counts: StudioDraftCounts

    /// True when a change set is in effect that undo would revert.
    @required
    canUndo: Boolean

    /// True when an undone change set can be applied again.
    @required
    canRedo: Boolean

    /// When the draft was opened.
    @required
    openedAt: String

    /// Who opened the draft.
    @required
    openedBy: String

    /// When the draft last changed.
    @required
    updatedAt: String

    /// Who last changed the draft.
    @required
    updatedBy: String
}

/// A datatype property shown on its class node.
structure StudioAttribute {
    /// Property IRI.
    @required
    iri: String

    /// Property label.
    @required
    label: String

    /// XSD range IRI; absent when the property has none.
    range: String
}

/// Attributes of a class node.
list StudioAttributeList {
    member: StudioAttribute
}

/// The members of a class node of the draft graph.
@mixin
structure StudioGraphNodeFields {
    /// Class IRI.
    @required
    iri: String

    /// Class label.
    @required
    label: String

    /// True when the class is grounded to a reference ontology (`skos:exactMatch` / `closeMatch` outside the namespace).
    @required
    grounded: Boolean

    /// True when the class is in the namespace's IRI space, so ops may change it.
    @required
    editable: Boolean

    /// Its datatype properties, at most 100.
    @required
    attributes: StudioAttributeList

    /// How many datatype properties it has.
    @required
    attributeCount: Integer

    /// Instances of the class in the active version's domain ABox; absent when that version lacks the class or there is no ABox.
    aboxCount: Integer

    /// True when only the draft has the class: its instances appear after it is deployed.
    @required
    afterDeploy: Boolean
}

/// A class node of the draft graph.
structure StudioGraphNode with [StudioGraphNodeFields] {}

/// Class nodes.
list StudioGraphNodeList {
    member: StudioGraphNode
}

/// The members of an edge between two class nodes.
@mixin
structure StudioGraphEdgeFields {
    /// Stable edge id.
    @required
    id: String

    /// Edge kind.
    @required
    kind: StudioEdgeKind

    /// Source class IRI (the child, or the property's domain).
    @required
    source: String

    /// Target class IRI (the parent, or the property's range).
    @required
    target: String

    /// Object property IRI (object-property edges only).
    iri: String

    /// Object property label (object-property edges only).
    label: String
}

/// An edge between two class nodes.
structure StudioGraphEdge with [StudioGraphEdgeFields] {}

/// Graph edges.
list StudioGraphEdgeList {
    member: StudioGraphEdge
}

/// The active version's domain ABox as the badges see it.
structure StudioAboxState {
    /// ABox build status (`READY`, `BUILDING`, `FAILED`).
    @required
    status: String

    /// True when the ABox was built for the active version.
    @required
    current: Boolean

    /// Version the ABox was built for; absent when none.
    conformsToVersion: Integer

    /// Active (deployed) version.
    @required
    activeVersion: Integer

    /// Why the counts are out of date; absent when they are current.
    reason: String
}

/// A term named by another term (a parent, child, domain or range), with its label. The draft can name a term it does not declare, such as a parent from a reference ontology (FIBO) or an XSD datatype.
structure StudioTermRef {
    /// Term IRI.
    @required
    iri: String

    /// Term label (the IRI's tail when the draft does not declare the term).
    @required
    label: String

    /// True when the draft declares the term as a class or property, so `GetStudioTerm` can open it; false for an external term, which answers 404 `TermNotFound`.
    @required
    declared: Boolean
}

/// Terms with labels.
list StudioTermRefList {
    member: StudioTermRef
}

/// A search hit.
structure StudioTermHit {
    /// Term IRI.
    @required
    iri: String

    /// Term label.
    @required
    label: String

    /// Term kind.
    @required
    kind: StudioTermKind
}

/// Search hits, best first.
list StudioTermHitList {
    member: StudioTermHit
}

/// One label of a term.
structure StudioLabel {
    /// Label text.
    @required
    value: String

    /// Language tag; absent for a plain label.
    lang: String
}

/// Labels of a term.
list StudioLabelList {
    member: StudioLabel
}

/// A grounding of a class.
structure StudioMatch {
    /// Reference-ontology IRI.
    @required
    target: String

    /// Match kind.
    @required
    kind: StudioMatchKind
}

/// Groundings of a class.
list StudioMatchList {
    member: StudioMatch
}

/// A property that uses a class as its domain (outgoing) or range (incoming).
structure StudioTermProperty {
    /// Property IRI.
    @required
    iri: String

    /// Property label.
    @required
    label: String

    /// Property kind.
    @required
    kind: StudioTermKind

    /// `outgoing` (the class is the domain) or `incoming` (the class is the range).
    @required
    direction: String

    /// The other end: the range of an outgoing property, the domain of an incoming one.
    other: String

    /// Label of the other end.
    otherLabel: String

    /// True when the draft declares the other end as a class or property (always false without one), so `GetStudioTerm` can open it; false for an external term such as an XSD datatype.
    @required
    otherDeclared: Boolean
}

/// Properties of a class.
list StudioTermPropertyList {
    member: StudioTermProperty
}

/// An R2RML map of a term.
structure StudioMapping {
    /// TriplesMap IRI.
    triplesMap: String

    /// Logical table name (classes).
    table: String

    /// Mapped column (datatype properties).
    column: String

    /// Parent TriplesMap of a join (object properties).
    parentTriplesMap: String
}

/// R2RML maps of a term.
list StudioMappingList {
    member: StudioMapping
}

/// The ABox badge of a class.
structure StudioAboxBadge {
    /// Instances in the active version's domain ABox; absent when that version lacks the class.
    aboxCount: Integer

    /// True when only the draft has the class.
    @required
    afterDeploy: Boolean
}

/// The result of a write to the draft.
@mixin
structure StudioChangeView {
    /// The new revision (the current one for a dry run).
    @required
    revision: Integer

    /// True when nothing was stored.
    @required
    dryRun: Boolean

    /// Warnings such as duplicate labels, a merge of classes mapped from different tables, or a mapped property whose domain or range moved to a parent class, in English.
    @required
    warnings: StudioStringList

    /// The lines of `warnings` as codes with their values, in the same order: at most the first 100. The codes: `MERGE_MAPPED_TABLES` (`source`, `target`, `sourceTables`, `targetTables`: the tables of each class joined by ", "), `DOMAIN_ON_PARENT_TABLE` (`property`, `tables`, `domain`, `mappedClass`) or `DOMAIN_ON_PARENT_MAP` (`property`, `map`, `domain`, `mappedClass`) for a mapped property whose domain moved to a parent class, `RANGE_ON_PARENT` (`property`, `mappedClass`, `range`) for a relationship whose range moved to a parent of the class it joins, `RANGE_UNCONFIRMED` (`property`, `range`) for a range an IRI template cannot confirm, `DOMAIN_GIVEN_BACK_TABLE` (`deleted`, `property`, `tables`, `classes`) or `DOMAIN_GIVEN_BACK_MAP` (`deleted`, `property`, `map`, `classes`) and `RANGE_GIVEN_BACK` (`deleted`, `property`, `classes`) for a mapped property whose domain or range was a deleted parent and is now the classes its mapping reads, and `ANOTHER_CLASS_LABELLED`, `ANOTHER_RELATIONSHIP_LABELLED` or `ANOTHER_ATTRIBUTE_LABELLED` (`label`, `iri`) for a duplicate label. Show the line of `warnings` for a code you do not know.
    warningMessages: StudioMessageList

    /// IRIs the change set minted, in op order.
    @required
    created: StudioStringList

    /// Added, removed and changed IRIs per kind (`classes`, `objectProperties`, `datatypeProperties`).
    @required
    diff: Document

    /// True when a change set is in effect that undo would revert.
    @required
    canUndo: Boolean

    /// True when an undone change set can be applied again.
    @required
    canRedo: Boolean
}

/// The stable code of a sentence the Ontology designer writes, for a client that writes it in the user's language. The member that carries a code lists its codes; a client that does not know a code shows the English text next to it.
@length(min: 1, max: 64)
string StudioMessageCode

/// The name of a placeholder of a coded sentence.
@length(min: 1, max: 32)
string StudioMessageParamName

/// The values a coded sentence names, by placeholder: labels and IRI tails of terms, version and revision numbers, counts, all as text. At most 10.
@length(max: 10)
map StudioMessageParams {
    key: StudioMessageParamName
    value: String
}

/// A sentence as its stable code and the values it names.
structure StudioMessage {
    /// The code.
    @required
    code: StudioMessageCode

    /// The values, by placeholder.
    @required
    params: StudioMessageParams
}

/// Coded sentences, such as the lines of a summary or of the warnings of a change: at most 100.
@length(max: 100)
list StudioMessageList {
    member: StudioMessage
}

/// One revision in the change history.
structure StudioHistoryEntry {
    /// Revision number.
    @required
    revision: Integer

    /// What produced it.
    @required
    kind: StudioChangeKind

    /// Whether it is a direct edit or an applied assistant change set.
    @required
    source: StudioChangeSource

    /// The assistant's reason, for an applied change set.
    rationale: String

    /// The applied change set, for an assistant revision.
    changeSetId: String

    /// Who made it (for an assistant revision, who applied it).
    author: String

    /// When.
    at: String

    /// One line per op, in English.
    @required
    summary: StudioStringList

    /// The lines of `summary` as codes with their values, in the same order: at most the first 100. Absent for a revision written before the codes, which has only `summary`. The first line of an open, undo or rebase is `OPENED_DRAFT` (`version`), `OPENED_DRAFT_NO_VERSION`, `UNDID_REVISION` (`revision`), `REBASED` (`version`) or `REBASED_NO_VERSION`. Each op has one: `ADD_CLASS` (`label`), `SET_LABEL` (`term`, `label`), `SET_COMMENT` (`term`), `CHANGE_IRI` (`term`, `newTerm`), `DELETE_CLASS_DETACH` or `DELETE_CLASS_DELETE` (`term`), `MERGE_CLASSES` (`source`, `target`), `ADD_PARENT` or `REMOVE_PARENT` (`term`, `parent`), `ADD_OBJECT_PROPERTY` (`label`, `domain`, `range`), `ADD_DATATYPE_PROPERTY` (`label`, `domain`), `UPDATE_PROPERTY` (`fields`, the changed ones among `domain`, `range`, `label` and `comment` joined by ", ", with an attribute's range named `datatype` except in lines written before; `term`), `DELETE_PROPERTY` (`term`), `REMOVE_MATCH` (`term`), `SET_EXACT_MATCH` or `SET_CLOSE_MATCH` (`term`, `target`), `REPLACE_DATATYPE` (`old`, `new`) or `RESTORE_TRIPLES`. Show the line of `summary` for a code you do not know.
    summaryMessages: StudioMessageList

    /// Ops in the change set.
    @required
    opCount: Integer

    /// The revision an undo or redo acted on.
    target: Integer

    /// Warnings recorded with it, in English.
    @required
    warnings: StudioStringList

    /// The lines of `warnings` as codes with their values, in the same order: at most the first 100, with the codes of `StudioChangeView` `warningMessages`. Absent for a revision written before the codes, which has only `warnings`.
    warningMessages: StudioMessageList

    /// True when its change set is in effect (undo would revert it).
    @required
    active: Boolean

    /// True when it was undone and can be redone.
    @required
    undone: Boolean
}

/// History entries, newest first.
list StudioHistoryEntryList {
    member: StudioHistoryEntry
}

/// A class or property an accept would rename or merge away.
structure StudioRename {
    /// IRI in the base version.
    @required
    fromIri: String

    /// IRI in the draft.
    @required
    toIri: String

    /// Renamed or merged.
    @required
    kind: StudioRenameKind
}

/// Renames and merges.
list StudioRenameList {
    member: StudioRename
}

/// Open the proposal in the Ontology designer: its draft starts as what accepting it would publish.
///
/// Naturally idempotent, so there is no `clientToken`: a draft that already exists is returned as it is, whatever the proposal's status.
///
/// Errors: 404 `ProposalNotFound`. 409 `ProposalNotEditable` (with `status`) when there is no draft yet and the proposal is not `pending`, `updated` or `accept_failed`. 409 `StudioConflict` when the namespace publishes another ontology, the proposal has no ontology, or it cannot be merged onto the newest version.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio", code: 200)
operation OpenStudioDraft {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid
    }

    output := with [StudioDraftView] {}

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Read the draft's revision, base version, whether a rebase is needed, and its counts.
///
/// Errors: 404 `ProposalNotFound`, or `StudioDraftNotOpen` when the proposal was never opened in the Ontology designer.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio", code: 200)
operation GetStudioDraft {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid
    }

    output := with [StudioDraftView] {}

    errors: [
        NotFoundError
    ]
}

/// Read the class graph of the draft with ABox badges: every class up to 150, else (or with `focus`) one class and its neighbours up to two hops, at most 150.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen`, or `TermNotFound` when `focus` is not a class of the draft. 422 `InvalidIri` when `focus` is not a valid IRI.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/graph", code: 200)
operation GetStudioGraph {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Class IRI to centre a focus view on; without it the most connected class is used above 150 classes.
        @httpQuery("focus")
        focus: VertexIri
    }

    output := {
        /// Revision shown.
        @required
        revision: Integer

        /// Full or focus view.
        @required
        mode: StudioGraphMode

        /// The class the focus view is centred on.
        focus: String

        /// Classes in the whole draft.
        @required
        totalClasses: Integer

        /// True when the focus view hit its node limit.
        @required
        truncated: Boolean

        /// Class nodes shown.
        @required
        nodes: StudioGraphNodeList

        /// Edges between the nodes shown.
        @required
        edges: StudioGraphEdgeList

        /// The active version's domain ABox; absent when the namespace has none.
        abox: StudioAboxState
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Read one class or property of the draft for the Inspector.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen`, or `TermNotFound` when `iri` is not a class or property of the draft. 422 `InvalidIri` when `iri` is not a valid IRI.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/term", code: 200)
operation GetStudioTerm {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Term IRI (a query parameter: an IRI with `#` cannot travel in a path label).
        @required
        @httpQuery("iri")
        iri: VertexIri
    }

    output := {
        /// Term IRI.
        @required
        iri: String

        /// Term kind.
        @required
        kind: StudioTermKind

        /// Main label.
        @required
        label: String

        /// Every `rdfs:label`.
        @required
        labels: StudioLabelList

        /// `skos:altLabel` values.
        @required
        altLabels: StudioStringList

        /// `rdfs:comment`.
        @required
        comment: String

        /// True when the term is in the namespace's IRI space.
        @required
        editable: Boolean

        /// Named parents (classes).
        parents: StudioTermRefList

        /// Named children (classes).
        children: StudioTermRefList

        /// Groundings (classes).
        matches: StudioMatchList

        /// Properties using the class (classes).
        properties: StudioTermPropertyList

        /// Domain classes (properties).
        domain: StudioTermRefList

        /// Range classes or datatype (properties).
        range: StudioTermRefList

        /// R2RML maps.
        @required
        mappings: StudioMappingList

        /// ABox badge (classes, when the namespace has an ABox).
        abox: StudioAboxBadge

        /// True when structured queries answer the class (classes): it is mapped, a mapped class reaches it by a forward `rdfs:subClassOf`/`owl:equivalentClass` path, or it states `owl:equivalentClass` to a mapped class.
        structuredAnswered: Boolean
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Search the draft's terms by label or IRI.
///
/// Not paginated on purpose: a search answers its best `limit` hits, ranked, and a caller who does
/// not find a term narrows `q` or `kind` rather than paging through weaker matches.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 422 when `q` is empty or longer than 200 characters, `kind` is unknown, or `limit` is out of range.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/terms", code: 200)
operation SearchStudioTerms {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Text to find in labels and IRI tails (case-insensitive), 1-200 characters.
        @required
        @length(min: 1, max: 200)
        @httpQuery("q")
        q: String

        /// Only terms of this kind.
        @httpQuery("kind")
        kind: StudioTermKind

        /// Maximum hits (1-50, default 20).
        @range(min: 1, max: 50)
        @httpQuery("limit")
        limit: Integer
    }

    output := {
        /// Revision searched.
        @required
        revision: Integer

        /// Hits, best first.
        @required
        terms: StudioTermHitList
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Apply a change set to `expectedRevision`: all ops or none. With `dryRun` the result is computed and nothing is stored.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `DraftConflict` (with the current `revision`) when the draft moved on. 409 `ProposalNotEditable` (with `status`) once the proposal left review, a dry run included. 409 `InvalidChange` (with `opIndex`) when an op does not apply, including an `update_property` that gives a mapped property a domain outside the classes its TriplesMap maps and their parents, a datatype other than its column's `rr:datatype`, or a range outside the class its join reaches and its parents, a `remove_parent` or `set_match` that takes away the parent such a domain or range needs, a `replace_datatype` from a datatype the mapping uses to one that is not XSD, and a `replace_datatype` that would retype a mapped property whose column keeps another `rr:datatype`. 422 `InvalidChange` (with `opIndex`) when an op does not parse: an unknown `op`, or a missing or extra field. 422 when there are no ops, more than 100, or more than 2097152 bytes of them.
///
/// No `clientToken`: every write names `expectedRevision`, so a repeat of a change set that was stored is a 409 `DraftConflict` whose `revision` shows it landed. A dry run stores nothing and can be repeated.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/changes", code: 200)
operation ApplyStudioChanges {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Ops, applied in order (at most 100).
        @required
        ops: StudioOpList

        /// The revision the change set was made against.
        @required
        expectedRevision: StudioRevision

        /// Compute the result and store nothing.
        dryRun: Boolean
    }

    output := with [StudioChangeView] {}

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// Undo the newest change set in effect, as a new revision.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `DraftConflict` (with the current `revision`), `ProposalNotEditable` (with `status`), or `NothingToUndo`.
///
/// No `clientToken`: a repeat of a write that landed names a revision the draft moved on from, so it is a 409 `DraftConflict` whose `revision` shows it landed.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/undo", code: 200)
operation UndoStudioChange {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// The revision on screen.
        @required
        expectedRevision: StudioRevision
    }

    output := with [StudioChangeView] {}

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// Apply the newest undone change set again, as a new revision.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `DraftConflict` (with the current `revision`), `ProposalNotEditable` (with `status`), or `NothingToRedo`.
///
/// No `clientToken`: a repeat of a write that landed names a revision the draft moved on from, so it is a 409 `DraftConflict` whose `revision` shows it landed.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/redo", code: 200)
operation RedoStudioChange {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// The revision on screen.
        @required
        expectedRevision: StudioRevision
    }

    output := with [StudioChangeView] {}

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// List the draft's revisions, newest first, one page at a time.
///
/// Keep paging until `nextToken` is absent. `revision`, `canUndo` and `canRedo` describe the draft,
/// not the page: every page carries them, read at the time of that request. A token only resumes the
/// draft it was minted for; a malformed token, or one from another draft, is a 400.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "entries")
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/history", code: 200)
operation ListStudioHistory {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of entries to return in one page (default 50).
        @httpQuery("maxResults")
        @range(min: 1, max: 100)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Current revision. On every page.
        @required
        revision: Integer

        /// This page of entries, newest first.
        @required
        entries: StudioHistoryEntryList

        /// True when undo would revert a change set. On every page.
        @required
        canUndo: Boolean

        /// True when redo would apply an undone change set. On every page.
        @required
        canRedo: Boolean
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Rebuild the draft on the newest version and replay its user ops.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `DraftConflict` (with the current `revision`), `ProposalNotEditable` (with `status`), or `NothingToRebase` when the draft is on the newest version. 409 `RebaseFailed` when ops do not apply to the newest version: `failures` lists them (`opId`, `op`, `error`) and `revision` is the unchanged revision; retry with their ids in `skipOps`. 409 `InvalidChange` when `skipOps` names an unknown op id. 409 `StudioConflict` when the proposal cannot be merged onto the newest version.
///
/// No `clientToken`: a repeat of a rebase that landed names a revision the draft moved on from, so it is a 409 `DraftConflict` whose `revision` shows it landed.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/rebase", code: 200)
operation RebaseStudioDraft {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// The revision on screen.
        @required
        expectedRevision: StudioRevision

        /// Op ids (`"{revision}:{index}"`) not to replay, at most 1000.
        skipOps: StudioOpIdList
    }

    output := {
        /// The new revision.
        @required
        revision: Integer

        /// The version the draft is now built on.
        baseVersion: Integer

        /// Ops replayed.
        @required
        replayed: Integer

        /// Op ids skipped.
        @required
        skipped: StudioStringList

        /// True when undo would revert the replayed ops.
        @required
        canUndo: Boolean

        /// Always false: a rebase clears redo.
        @required
        canRedo: Boolean
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// What accepting the current revision would publish: the diff with the base version, renames and merges, the governed metrics it breaks, and what structured queries cannot answer among the terms it adds or changes and the classes the base version answers and the draft does not. Accept itself is `AcceptProposal` with this `revision` as `expectedRevision`.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/accept-preview", code: 200)
operation GetStudioAcceptPreview {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid
    }

    output := {
        /// Revision previewed.
        @required
        revision: Integer

        /// Version the draft was built on.
        baseVersion: Integer

        /// Newest published version now.
        headVersion: Integer

        /// True when accept would answer 409 `RebaseRequired`.
        @required
        rebaseRequired: Boolean

        /// Added, removed and changed IRIs per kind compared with the base version.
        @required
        diff: Document

        /// Classes and properties of the base version the draft renamed or merged away.
        @required
        renamed: StudioRenameList

        /// Governed metrics linked to classes the draft removes.
        @required
        breakingMetrics: BreakingMetricList

        /// The code of `breakingMetricsError`, `METRICS_UNCHECKED`; absent when the metrics were checked.
        breakingMetricsErrorCode: StudioMessageCode

        /// Fixed message ("Breaking metrics could not be checked; the graph database may be unreachable.") when the metrics could not be checked, in which case `breakingMetrics` is empty; absent when they were.
        breakingMetricsError: String

        /// The structured-query check of `GetStudioChecks`, limited to the terms the diff adds or changes and the classes the base version answers and the draft does not (an edit to another term can leave a parent unanswered); `mapsNoTable`, `removedMappedClasses` and `removedMappedProperties` are about the whole draft.
        @required
        structured: StudioStructuredCheck
    }

    errors: [
        NotFoundError
    ]
}

// ── Chat with the assistant and its change sets ─────────────────────────
/// A term selected in the graph that a message is about.
structure StudioSelectedTerm {
    /// Term IRI.
    @required
    iri: String

    /// Term label.
    @required
    label: String

    /// Term kind.
    @required
    kind: StudioTermKind
}

/// Selected terms.
list StudioSelectedTermList {
    member: StudioSelectedTerm
}

/// One step of the chat. `type` decides which of the optional fields are set.
structure StudioChatEvent {
    /// Position in the chat; increases across all turns of the proposal.
    @required
    seq: Integer

    /// The turn it belongs to.
    @required
    turnId: String

    /// Event kind.
    @required
    type: StudioChatEventType

    /// When it was written.
    at: String

    /// The message, the assistant's text while it worked, or its answer (Markdown).
    text: String

    /// Who sent the message (`user_message`).
    author: String

    /// The terms selected when the message was sent (`user_message`).
    selection: StudioSelectedTermList

    /// Tool name (`tool_call`, `tool_result`).
    tool: String

    /// One-line summary of the tool's arguments (`tool_call`).
    input: String

    /// `success` or `error` for a `tool_result`; the turn's status for `turn_ended`.
    status: String

    /// One-line account of a tool's result (`tool_result`).
    summary: String

    /// The proposed change set (`change_set`).
    changeSetId: String

    /// One line per op of the proposed change set (`change_set`), in English.
    opSummaries: StudioStringList

    /// The lines of `opSummaries` as codes with their values, in the same order, with the codes of `StudioHistoryEntry` `summaryMessages` (`change_set`). Absent for an event written before the codes.
    opSummaryMessages: StudioMessageList

    /// Why the turn ended limited or failed (`turn_ended`).
    message: String

    /// Query language (`query`).
    language: StudioQueryLanguage

    /// What the query read (`query`).
    target: StudioQueryTarget

    /// The query text (`query`).
    query: String

    /// Rows the query returned, at most 1,000 (`query`).
    rowCount: Integer
}

/// Chat events, oldest first.
list StudioChatEventList {
    member: StudioChatEvent
}

/// An assistant turn: one message and the work on it.
structure StudioTurn {
    /// Turn id.
    @required
    turnId: String

    /// Turn status.
    @required
    status: StudioTurnStatus

    /// Who sent the message.
    author: String

    /// When it started.
    startedAt: String

    /// When it ended; absent while it runs.
    endedAt: String
}

/// Why a change set no longer applies to a newer revision of the draft.
structure StudioChangeSetStale {
    /// The revision it was tried on.
    @required
    revision: Integer

    /// What failed.
    @required
    message: String

    /// Index of the op that failed.
    @required
    opIndex: Integer
}

/// The members of a change set the assistant proposed.
@mixin
structure StudioChangeSetFields {
    /// Change set id.
    @required
    changeSetId: StudioChangeSetId

    /// The turn that proposed it.
    turnId: String

    /// Status.
    @required
    status: StudioChangeSetStatus

    /// When it was proposed.
    createdAt: String

    /// The revision it was checked on when it was proposed.
    @required
    createdRevision: Integer

    /// Who sent the message it answers.
    createdBy: String

    /// The assistant's reason.
    @required
    rationale: String

    /// Its ops, in order, as typed ops (see `StudioOpList`).
    @required
    ops: StudioOpList

    /// One line per op, in English.
    @required
    summary: StudioStringList

    /// The lines of `summary` as codes with their values, in the same order, with the codes of `StudioHistoryEntry` `summaryMessages`. Absent for a change set proposed before the codes, which has only `summary`.
    summaryMessages: StudioMessageList

    /// Warnings of its dry run, in English.
    @required
    warnings: StudioStringList

    /// The lines of `warnings` as codes with their values, in the same order: at most the first 100, with the codes of `StudioChangeView` `warningMessages`. Absent for a change set proposed before the codes.
    warningMessages: StudioMessageList

    /// How many IRIs it adds, removes and changes per kind.
    diffCounts: Document

    /// The revision it was applied as.
    appliedRevision: Integer

    /// The ops that were applied.
    appliedOpIndexes: StudioOpIndexList

    /// Who applied it.
    appliedBy: String

    /// When it was applied.
    appliedAt: String

    /// Who discarded it.
    discardedBy: String

    /// When it was discarded.
    discardedAt: String

    /// Set when applying it to a newer revision failed; cleared when a later apply works.
    stale: StudioChangeSetStale
}

/// A change set the assistant proposed.
structure StudioChangeSet with [StudioChangeSetFields] {}

/// Change sets, oldest first.
list StudioChangeSetList {
    member: StudioChangeSet
}

/// A class node of a change set preview.
structure StudioPreviewNode with [StudioGraphNodeFields] {
    /// What the change set does to the class; absent when nothing.
    preview: StudioPreviewMark
}

/// Preview nodes.
list StudioPreviewNodeList {
    member: StudioPreviewNode
}

/// An edge of a change set preview.
structure StudioPreviewEdge with [StudioGraphEdgeFields] {
    /// What the change set does to the edge; absent when nothing.
    preview: StudioPreviewMark
}

/// Preview edges.
list StudioPreviewEdgeList {
    member: StudioPreviewEdge
}

/// The graph after a change set, with what it adds, changes and removes marked.
structure StudioPreviewGraph {
    /// Revision the change set was run on.
    @required
    revision: Integer

    /// Full or focus view, as `GetStudioGraph` decides it for the draft after the change set.
    @required
    mode: StudioGraphMode

    /// The class the focus view is centred on.
    focus: String

    /// Classes in the whole draft after the change set.
    @required
    totalClasses: Integer

    /// True when the focus view hit its node limit.
    @required
    truncated: Boolean

    /// Class nodes, the ones the change set removes included.
    @required
    nodes: StudioPreviewNodeList

    /// Edges between them, the ones the change set removes included.
    @required
    edges: StudioPreviewEdgeList

    /// The active version's domain ABox; absent when the namespace has none.
    abox: StudioAboxState
}

/// Why the selected ops of a change set do not apply.
structure StudioPreviewError {
    /// What failed.
    @required
    message: String

    /// Index of the op that failed, in the change set.
    @required
    opIndex: Integer
}

/// Send a message to the assistant. Its turn runs in the background: poll `ListStudioEvents` until the turn is no longer `running`. The assistant reads the draft and may propose change sets; it never changes the draft.
///
/// Each call starts a new turn; send a `clientToken` to make a retry safe.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `ProposalNotEditable` (with `status`), `TurnInProgress` (with the running `turnId`), `ConversationFull` (the conversation holds 300 messages), or a `clientToken` reused with a different message. 422 `InvalidMessage` (empty or too long, or more than 20 selected terms) or `InvalidIri`.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/messages", code: 202)
operation SendStudioMessage {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// The message (1-4000 characters once leading and trailing white space is removed).
        @required
        @length(min: 1, max: 4000)
        text: String

        /// IRIs selected in the graph that the message is about (at most 20); the ones the draft does not declare are dropped.
        @length(max: 20)
        selection: StudioIriList

        /// Optional idempotency token. A retry with the same token and the same
        /// message within 24 hours returns the original turn instead of
        /// starting a second one; reusing the token with a different message
        /// is rejected with a 409.
        @idempotencyToken
        clientToken: ClientToken
    }

    output := {
        /// The turn that answers the message.
        @required
        turnId: String

        /// Seq of the message's event.
        @required
        seq: Integer
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// List the chat's events after a seq, oldest first, and the newest turn. A turn whose worker died is reported `failed` here.
///
/// Not paginated with `nextToken` on purpose: `after` is a cursor the caller keeps across polls. While a turn runs, new events arrive after the last one read, so the caller asks again after the last `seq` it has; a `nextToken` that is absent at the end of the list could not say where to resume once more events land. `more` says that events already written follow the ones returned.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 422 when `after` or `limit` is out of range.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/events", code: 200)
operation ListStudioEvents {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Only events with a larger seq (default 0: all).
        @range(min: 0, max: 1000000000)
        @httpQuery("after")
        after: Integer

        /// Maximum events (1-500, default 200).
        @range(min: 1, max: 500)
        @httpQuery("limit")
        limit: Integer
    }

    output := {
        /// Events, oldest first.
        @required
        events: StudioChatEventList

        /// Seq of the newest event of the chat.
        @required
        lastSeq: Integer

        /// True when more events follow the ones returned; ask again after the last seq.
        @required
        more: Boolean

        /// The newest turn; absent before the first message.
        turn: StudioTurn
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// List the change sets the assistant proposed, oldest first, one page at a time.
///
/// Keep paging until `nextToken` is absent; with `status`, a page can hold fewer change sets than `maxResults`, even none, while more follow. A page also holds at most 4000000 bytes of change sets (as UTF-8 JSON) and ends early before the one that would pass it; a single larger change set comes on a page of its own. `revision` describes the draft, not the page: every page carries it, read at the time of that request. A token only resumes the listing it was minted for (the same draft and `status`); a malformed token, or one from another listing, is a 400.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "changeSets")
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/changesets", code: 200)
operation ListStudioChangeSets {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Only change sets with this status.
        @httpQuery("status")
        status: StudioChangeSetStatus

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of change sets to return in one page (default 20).
        @httpQuery("maxResults")
        @range(min: 1, max: 50)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Current revision of the draft. On every page.
        @required
        revision: Integer

        /// This page of change sets, oldest first.
        @required
        changeSets: StudioChangeSetList
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Preview the selected ops of a change set on the current revision without storing anything: the diff and the graph after them, with added, changed and removed classes and edges marked. When they do not apply, `applies` is false and `error` names the op.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen` or `ChangeSetNotFound`. 422 `InvalidRequest` for a malformed id or `opIndexes`, `InvalidIri` for a malformed `focus`.
@readonly
@http(
    method: "GET"
    uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/changesets/{changeSetId}/preview"
    code: 200
)
operation PreviewStudioChangeSet {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Change set id.
        @required
        @httpLabel
        changeSetId: StudioChangeSetId

        /// The ops to preview, comma-separated (`0,2,3`); all of them when absent.
        @httpQuery("opIndexes")
        @length(min: 1, max: 200)
        @pattern("^[0-9]{1,2}(,[0-9]{1,2})*$")
        opIndexes: String

        /// Class IRI the graph on screen is focused on.
        @httpQuery("focus")
        focus: VertexIri
    }

    output := {
        /// Revision the ops were run on.
        @required
        revision: Integer

        /// Change set id.
        @required
        changeSetId: StudioChangeSetId

        /// The ops run, ascending.
        @required
        opIndexes: StudioOpIndexList

        /// True when the ops apply to the current revision.
        @required
        applies: Boolean

        /// Why they do not apply; absent when they do.
        error: StudioPreviewError

        /// Warnings of the dry run.
        @required
        warnings: StudioStringList

        /// IRIs the ops would mint.
        @required
        created: StudioStringList

        /// Added, removed and changed IRIs per kind; absent when the ops do not apply.
        diff: Document

        /// The graph after the ops; absent when they do not apply.
        graph: StudioPreviewGraph
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Apply the selected ops of a pending change set (all of them without `opIndexes`) as one revision whose source is the assistant. On a newer revision than the one it was proposed on, the ops are checked again; when they no longer apply the change set is marked `stale` and nothing changes.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen` or `ChangeSetNotFound`. 409 `DraftConflict` (with the current `revision`), `ProposalNotEditable` (with `status`), `ChangeSetNotPending` (with `status`), `InvalidChange` (with `opIndex`) when the selected ops do not apply to the revision the change set was proposed on, or `ChangeSetStale` (with `opIndex` and `revision`) when they do not apply to a newer one. 422 `InvalidRequest` for a malformed id or `opIndexes`.
///
/// No `clientToken`: a change set is applied at most once, so a repeat of an apply that landed is a 409 `ChangeSetNotPending` whose `status` is `applied`.
@http(
    method: "POST"
    uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/changesets/{changeSetId}/apply"
    code: 200
)
operation ApplyStudioChangeSet {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Change set id.
        @required
        @httpLabel
        changeSetId: StudioChangeSetId

        /// The revision on screen.
        @required
        expectedRevision: StudioRevision

        /// The ops to apply; all of them when absent.
        opIndexes: StudioOpIndexList
    }

    output := with [StudioChangeView] {
        /// The change set, now applied.
        @required
        changeSet: StudioChangeSet
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

/// Discard a pending change set.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen` or `ChangeSetNotFound`. 409 `ChangeSetNotPending` (with `status`). 422 `InvalidRequest` for a malformed id.
///
/// No `clientToken`: a repeat of a discard that landed is a 409 `ChangeSetNotPending` whose `status` is `discarded`.
@http(
    method: "POST"
    uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/changesets/{changeSetId}/discard"
    code: 200
)
operation DiscardStudioChangeSet {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Change set id.
        @required
        @httpLabel
        changeSetId: StudioChangeSetId
    }

    output := with [StudioChangeSetFields] {}

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

// ── Checks, the query console and competency questions ─────────────────────
/// Severity of a check finding.
enum StudioFindingSeverity {
    /// The draft is wrong: a dangling domain or range, a subclass cycle, a class as a datatype range.
    ERROR = "error"

    /// Likely a mistake: a missing or duplicate label, or a domain or range outside the draft's IRI space.
    WARNING = "warning"

    /// Worth a look: a class with no relationship, parent or attribute.
    INFO = "info"
}

/// One finding of the instant checks.
structure StudioFinding {
    /// `DANGLING_DOMAIN`, `DANGLING_RANGE`, `SUBCLASS_CYCLE`, `MISSING_LABEL`, `DUPLICATE_LABEL`, `ISOLATED_CLASS` or `DATATYPE_RANGE_NOT_DATATYPE`.
    @required
    code: String

    /// How serious it is.
    @required
    severity: StudioFindingSeverity

    /// What is wrong, naming the terms, in English.
    @required
    message: String

    /// The sentence of `message` as a code: `DANGLING_DOMAIN` (`property`, `domain`), `DANGLING_RANGE` (`property`, `range`), `UNKNOWN_DATATYPE_RANGE` (`property`, `range`: a range that is no datatype the designer accepts), `DATATYPE_RANGE_NOT_DATATYPE` (`property`, `range`), `SUBCLASS_CYCLE` (`classes`, their names joined by " -> "), `MISSING_LABEL` (`term`), `DUPLICATE_CLASS_LABEL`, `DUPLICATE_RELATIONSHIP_LABEL`, `DUPLICATE_ATTRIBUTE_LABEL` or `DUPLICATE_TERM_LABEL` (`count`, `label`), or `ISOLATED_CLASS` (`term`). Show `message` for a code you do not know.
    @required
    messageCode: StudioMessageCode

    /// The values `messageCode` names, by placeholder.
    @required
    messageParams: StudioMessageParams

    /// The IRIs involved, sorted.
    @required
    iris: StudioStringList
}

/// Findings, errors first.
list StudioFindingList {
    member: StudioFinding
}

/// Findings per severity, all of them counted.
structure StudioFindingCounts {
    /// Errors.
    @required
    error: Integer

    /// Warnings.
    @required
    warning: Integer

    /// Infos.
    @required
    info: Integer
}

/// The structural checks of the draft's TBox.
structure StudioInstantChecks {
    /// At most 200 findings, errors first.
    @required
    findings: StudioFindingList

    /// Every finding counted.
    @required
    counts: StudioFindingCounts

    /// True when there were more than 200 findings.
    @required
    truncated: Boolean
}

/// A term of the structured-query check.
structure StudioStructuredTerm {
    /// Term IRI.
    @required
    iri: String

    /// Term label (its IRI tail when it has none).
    @required
    label: String

    /// `unmappedProperties`: the mapped class the property is listed for: its first mapped domain class by IRI, or, for a relationship none of whose domains is mapped, its first mapped range class. `removedMappedProperties`: the class whose TriplesMap mapped it in the version the draft was built on (the first by IRI).
    domainIri: String

    /// `labelDiffers`: the table or column name that structured queries keep using. `removedMappedProperties`: the column an attribute was read from, when every map of it read the same one.
    physicalName: String

    /// `multiTableClasses`: the tables the class is mapped from, sorted (at most 20). `removedMappedClasses`: the tables it was mapped from in the version the draft was built on. A table is its datasource and its full `rr:tableName`; each is its dotted name, followed by its datasource id in brackets when another table has that name in another datasource.
    @length(max: 20)
    tables: StudioStringList
}

/// Terms sorted by label, then IRI: at most 100.
@length(max: 100)
list StudioStructuredTermList {
    member: StudioStructuredTerm
}

/// The terms of one finding of the structured-query check.
structure StudioStructuredGroup {
    /// Every term of the finding, counted.
    @required
    count: Integer

    /// The first 100 of them.
    @required
    terms: StudioStructuredTermList
}

/// What structured queries (Ontop and NL-to-SQL) cannot answer in the draft, and the names they keep using. A class is mapped when it is the `rr:class` of the subject map of a TriplesMap; a property is mapped when a PredicateObjectMap names it with `rr:predicate`, or with an `rr:predicateMap` whose `rr:constant` is the property. Read from the draft alone, except `mapsNoTable`, `removedMappedClasses` and `removedMappedProperties`, which compare it with the version it was built on.
structure StudioStructuredCheck {
    /// Classes that no TriplesMap maps, leaving out a parent (at any depth) or an equivalent (`owl:equivalentClass`) of a mapped class, which structured queries answer through that class. Structured queries cannot answer them; facts extracted from documents can fill them after deploy.
    @required
    unmappedClasses: StudioStructuredGroup

    /// Attributes and relationships that no PredicateObjectMap maps although one of their domain classes is mapped, or, for a relationship, one of its range classes (the NL-to-SQL foreign-key walk follows it from either end). Structured queries do not use them. Each term has `domainIri`.
    @required
    unmappedProperties: StudioStructuredGroup

    /// Mapped classes read from one table, and their attributes, whose label is not the table or column name. Structured queries keep using that name. Each term has `physicalName`.
    @required
    labelDiffers: StudioStructuredGroup

    /// Mapped classes read from more than one table, which NL-to-SQL describes as one table. Each term has `tables`.
    @required
    multiTableClasses: StudioStructuredGroup

    /// Classes mapped in the version the draft was built on that the draft deletes: structured queries no longer answer from their tables. A renamed or merged class is not listed, since its maps move with it, nor one whose tables the draft still reads. Each term has `tables`.
    @required
    removedMappedClasses: StudioStructuredGroup

    /// Attributes and relationships mapped in the version the draft was built on that the draft deletes: structured queries no longer use their columns or joins. A renamed property is not listed, since its maps move with it, nor one whose column or join a map of the draft still reads from the same table, nor a mapped predicate that the ontology does not declare as an attribute or relationship (such as `rdfs:label`). Each term has `domainIri`, and an attribute read from one column has `physicalName`.
    @required
    removedMappedProperties: StudioStructuredGroup

    /// True when the draft maps no table although the version it was built on mapped at least one: structured queries are off once it is deployed.
    @required
    mapsNoTable: Boolean

    /// Fixed message ("The table mappings of the version the draft was built on could not be read.") when that version could not be read, in which case `mapsNoTable` is false and `removedMappedClasses` and `removedMappedProperties` are empty; absent otherwise.
    error: String

    /// The code of `error`, `BASE_MAPPING_UNREADABLE`; absent when that version was read.
    errorCode: StudioMessageCode
}

/// The tables the draft's mapping names that the source catalog does not have.
structure StudioTablesCheck {
    /// False when the draft has no mapping to check.
    checked: Boolean

    /// Missing tables.
    missing: StudioStringList

    /// Why the section could not be checked; absent when it was.
    error: String

    /// The code of `error`, `TABLES_UNREADABLE`; absent when it was checked.
    errorCode: StudioMessageCode
}

/// How a class with instances leaves the draft.
enum StudioAboxImpactChange {
    /// The draft deletes it, or renames or merges it into a class that the draft
    /// then deletes.
    REMOVED = "removed"

    /// The draft changes its IRI.
    RENAMED = "renamed"

    /// The draft merges it into another class (`merge_classes`).
    MERGED = "merged"
}

/// A class of the active version with instances that the draft removes, renames
/// or merges away.
structure StudioAboxImpact {
    /// The class IRI in the active version.
    @required
    classIri: String

    /// Its instances in the domain ABox.
    @required
    instances: Integer

    /// Removed, renamed or merged.
    @required
    change: StudioAboxImpactChange

    /// The class of the draft it went to, when renamed or merged.
    toIri: String
}

/// Impacts, the most instances first.
list StudioAboxImpactList {
    member: StudioAboxImpact
}

/// The draft compared with the active version's domain ABox.
structure StudioAboxCheck {
    /// False when the namespace has no domain ABox.
    available: Boolean

    /// True when the ABox conforms to the active version.
    current: Boolean

    /// Classes with instances that the draft removes, renames or merges away.
    impacts: StudioAboxImpactList

    /// Why the section could not be checked; absent when it was.
    error: String

    /// The code of `error`, `ABOX_UNREADABLE`; absent when it was checked.
    errorCode: StudioMessageCode
}

/// The document mapping rows the draft would leave stale once deployed.
structure StudioMappingsCheck {
    /// False when the namespace has no active version.
    available: Boolean

    /// Rows that would go stale.
    stale: Integer

    /// Their targets, one for each row, sorted (at most 50).
    staleTargets: StudioStringList

    /// Why the section could not be checked; absent when it was.
    error: String

    /// The code of `error`, `MAPPINGS_UNREADABLE`; absent when it was checked.
    errorCode: StudioMessageCode
}

/// The governed metrics the draft would break.
structure StudioMetricsCheck {
    /// Breaking metrics.
    breaking: BreakingMetricList

    /// Why the section could not be checked; absent when it was.
    error: String

    /// The code of `error`, `METRICS_UNCHECKED`; absent when it was checked.
    errorCode: StudioMessageCode
}

/// The draft compared with the data outside it; each section is best effort.
structure StudioDataChecks {
    /// Source tables.
    @required
    tables: StudioTablesCheck

    /// Domain ABox.
    @required
    abox: StudioAboxCheck

    /// Document mappings.
    @required
    mappings: StudioMappingsCheck

    /// Governed metrics.
    @required
    metrics: StudioMetricsCheck
}

/// Check the current revision: its structural findings, what structured queries cannot answer in it, and its comparison with the data. The full SHACL validation is the proposal's validate job.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/checks", code: 200)
operation GetStudioChecks {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid
    }

    output := {
        /// The revision checked.
        @required
        revision: Integer

        /// Structural findings.
        @required
        instant: StudioInstantChecks

        /// What structured queries cannot answer in the draft.
        @required
        structured: StudioStructuredCheck

        /// Comparison with the data.
        @required
        data: StudioDataChecks
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Language of a console query.
enum StudioQueryLanguage {
    /// SPARQL 1.1: `SELECT`, `ASK`, `CONSTRUCT` or `DESCRIBE`.
    SPARQL = "sparql"

    /// openCypher, read clauses only.
    CYPHER = "cypher"
}

/// What a console query reads.
enum StudioQueryTarget {
    /// The draft's current revision and the instances extracted from documents (SPARQL).
    DRAFT = "draft"

    /// The documents' lexical graph (Cypher).
    LEXICAL = "lexical"
}

/// Kind of a result cell.
enum StudioQueryCellType {
    /// An IRI.
    URI = "uri"

    /// A literal, with its `datatype` or `lang`.
    LITERAL = "literal"

    /// A blank node.
    BNODE = "bnode"

    /// A Cypher value that is not a string, as JSON.
    VALUE = "value"
}

/// One cell of a result row.
structure StudioQueryCell {
    /// Kind.
    @required
    type: StudioQueryCellType

    /// The IRI, the lexical form, the blank node label or the JSON value.
    @required
    value: String

    /// Datatype IRI of a typed literal.
    datatype: String

    /// Language tag of a literal.
    lang: String
}

/// One result row; an unbound variable is null.
@sparse
list StudioQueryRow {
    member: StudioQueryCell
}

/// Result rows.
list StudioQueryRowList {
    member: StudioQueryRow
}

/// Returned with HTTP 502 when the graph database cannot run the query (`QueryFailed`).
@error("server")
@httpError(502)
structure StudioQueryFailedError {
    /// What failed, in one line.
    @required
    message: String
}

/// Returned with HTTP 504 when the query does not finish within 20 seconds (`QueryTimeout`).
@error("server")
@httpError(504)
structure StudioQueryTimeoutError {
    /// Advice to narrow the query.
    @required
    message: String
}

/// Run one read-only query of the console: SPARQL on the draft's current revision and the instances from documents, or openCypher on the documents' lexical graph. At most 1,000 rows come back within 20 seconds. SPARQL may not name graphs (`FROM`, `FROM NAMED`, `GRAPH`, `SERVICE`): the server chooses them. Every Cypher node pattern must carry one of the namespace's labels. `CONSTRUCT` and `DESCRIBE` results are `subject`, `predicate`, `object` rows.
///
/// A POST only because the query is a body. It only reads (the graph it may load for the revision is a cache), so a repeat is safe and there is no `clientToken`.
///
/// Errors: 404 `ProposalNotFound` or `StudioDraftNotOpen`. 409 `ProposalNotEditable` (with `status`) for SPARQL on a draft no longer under review. 422 `QueryRefused` for a query the guards refuse or Neptune finds malformed. 502 `QueryFailed`. 504 `QueryTimeout`.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/queries", code: 200)
operation RunStudioQuery {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// `sparql` goes with `draft`, `cypher` with `lexical`.
        @required
        language: StudioQueryLanguage

        /// What the query reads.
        @required
        target: StudioQueryTarget

        /// The query (at most 20,000 characters).
        @required
        @length(min: 1, max: 20000)
        query: String
    }

    output := {
        /// Query language.
        @required
        language: StudioQueryLanguage

        /// What the query read.
        @required
        target: StudioQueryTarget

        /// The draft revision the query read.
        @required
        revision: Integer

        /// `SELECT`, `ASK`, `CONSTRUCT` or `DESCRIBE` for SPARQL; `MATCH` for Cypher.
        @required
        form: String

        /// Column names; `result` for `ASK`.
        @required
        columns: StudioStringList

        /// At most 1,000 rows.
        @required
        rows: StudioQueryRowList

        /// Rows returned.
        @required
        rowCount: Integer

        /// True when the query had more rows than were returned.
        @required
        truncated: Boolean

        /// Time the server spent, in milliseconds.
        @required
        elapsedMs: Integer
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
        StudioQueryFailedError
        StudioQueryTimeoutError
    ]
}

/// The assistant's verdict on a competency question.
enum StudioQuestionStatus {
    /// The draft answers it: the query returns rows.
    ANSWERABLE = "answerable"

    /// The schema can express it, but no data answers it yet.
    EXPRESSIBLE = "expressible"

    /// Classes or relationships it needs are missing.
    NOT_EXPRESSIBLE = "not_expressible"
}

/// Id of a competency question, minted by the client.
@pattern("^[0-9A-Za-z-]{1,64}$")
@length(min: 1, max: 64)
string StudioQuestionId

/// The latest check of a competency question.
structure StudioQuestionCheck {
    /// The proposal whose draft was checked.
    @required
    proposalId: String

    /// The revision checked.
    @required
    revision: Integer

    /// The verdict; `answerable` only when the server's rerun of the query returned rows.
    @required
    status: StudioQuestionStatus

    /// The SPARQL query that answers the question; absent for `not_expressible`.
    query: String

    /// Rows the query returned.
    @required
    rowCount: Integer

    /// The first rows (at most 5).
    @required
    sample: StudioQueryRowList

    /// The assistant's explanation.
    explanation: String

    /// When it was checked.
    @required
    checkedAt: String
}

/// The fields of a competency question.
@mixin
structure StudioQuestionFields {
    /// Question id.
    @required
    questionId: StudioQuestionId

    /// The question.
    @required
    text: String

    /// What a good answer looks like.
    expectation: String

    /// The latest check; cleared when the text changes.
    lastCheck: StudioQuestionCheck

    /// When it was created.
    createdAt: String

    /// When it was last saved.
    updatedAt: String

    /// Who saved it last.
    updatedBy: String
}

/// A competency question of the namespace.
structure StudioQuestion with [StudioQuestionFields] {}

/// Competency questions, oldest first.
list StudioQuestionList {
    member: StudioQuestion
}

/// List the namespace's competency questions with their latest check, oldest first, one page at a time. Every draft of the namespace shares them.
///
/// Keep paging until `nextToken` is absent. A token resumes after the last question of its page, so a question deleted meanwhile does not shift the next page; a malformed token, or one from another namespace, is a 400.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "questions")
@http(method: "GET", uri: "/namespaces/{namespaceId}/studio/questions", code: 200)
operation ListStudioQuestions {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of questions to return in one page (default 50).
        @httpQuery("maxResults")
        @range(min: 1, max: 100)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// This page of questions, oldest first.
        @required
        questions: StudioQuestionList
    }

    errors: [
        ValidationError
    ]
}

/// Create or replace a competency question. Changing its text clears its latest check.
///
/// Errors: 409 `TooManyQuestions` (a namespace holds at most 200). 422 `InvalidQuestion` for a malformed id, an empty or too long text, or too long an expectation.
@idempotent
@http(method: "PUT", uri: "/namespaces/{namespaceId}/studio/questions/{questionId}", code: 200)
operation PutStudioQuestion {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Question id, minted by the client.
        @required
        @httpLabel
        questionId: StudioQuestionId

        /// The question (1-1000 characters).
        @required
        @length(min: 1, max: 1000)
        text: String

        /// What a good answer looks like (at most 500 characters); an empty one removes it.
        @length(max: 500)
        expectation: String
    }

    output := with [StudioQuestionFields] {}

    errors: [
        ConflictError
        ValidationError
    ]
}

/// Delete a competency question; deleting one that is gone succeeds.
///
/// Errors: 422 `InvalidQuestion` for a malformed id.
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/studio/questions/{questionId}", code: 204)
operation DeleteStudioQuestion {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Question id.
        @required
        @httpLabel
        questionId: StudioQuestionId
    }

    output := {}

    errors: [
        ValidationError
    ]
}

/// Ask the assistant to check a competency question against the draft. It begins a turn like `SendStudioMessage`: the assistant writes a SPARQL query, runs it, and records its verdict on the question; for a question that is not expressible it may propose a change set.
///
/// No `clientToken`: a repeat while the check runs is a 409 `TurnInProgress` naming its `turnId`, and a repeat after it ended checks the question again, which only replaces its latest check.
///
/// Errors: 404 `ProposalNotFound`, `StudioDraftNotOpen` or `QuestionNotFound`. 409 `ProposalNotEditable` (with `status`), `TurnInProgress` (with the running `turnId`) or `ConversationFull`. 422 `InvalidQuestion` for a malformed id.
@http(
    method: "POST"
    uri: "/namespaces/{namespaceId}/proposals/{proposalId}/studio/questions/{questionId}/check"
    code: 202
)
operation CheckStudioQuestion {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Proposal id.
        @required
        @httpLabel
        proposalId: Uuid

        /// Question id.
        @required
        @httpLabel
        questionId: StudioQuestionId
    }

    output := {
        /// The turn that checks the question.
        @required
        turnId: String

        /// Seq of the turn's first event.
        @required
        seq: Integer
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}
