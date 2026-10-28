// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Domain ABox: schema-guided instances extracted from documents.
$version: "2"

namespace com.amazon.semanticcontext.domainabox

use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#PaginationToken
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.ontologygraph#VertexIri

/// Build state of a namespace's domain ABox.
enum DomainAboxStatus {
    /// No ABox: never built, or the domain ontology was deleted.
    EMPTY

    /// A build is running.
    BUILDING

    /// The last build succeeded.
    READY

    /// The last build failed; the previous ABox, if any, is kept.
    FAILED
}

/// Kind of a mapping row.
enum AboxMappingKind {
    /// A lexical entity class, mapped to an ontology class.
    CLASS = "class"

    /// A lexical relation between two mapped classes, mapped to an object property.
    RELATION = "relation"

    /// A lexical attribute of a mapped class, mapped to a datatype property or kept as a free attribute.
    ATTRIBUTE = "attribute"
}

/// Who decided a mapping row.
enum AboxMappingOrigin {
    /// Decided automatically; may be decided again when its candidates change.
    AUTO = "auto"

    /// Set by a person; never overwritten automatically.
    MANUAL = "manual"
}

/// Whether a mapping row applies to the active ontology version.
enum AboxMappingStatus {
    /// Applied.
    ACTIVE = "active"

    /// Its target is missing from the active version, or a relation row's property does not admit
    /// the row's classes in its direction; not applied.
    STALE = "stale"

    /// Deliberately not mapped.
    IGNORED = "ignored"
}

/// Direction in which a relation mapping applies its object property.
enum AboxMappingDirection {
    /// The lexical subject is the property's subject.
    FORWARD = "forward"

    /// The lexical subject is the property's object.
    INVERSE = "inverse"
}

/// Id of a mapping row: its kind, a dot and 32 hex digits (`class.0123...`).
@length(min: 1, max: 64)
@pattern("^(class|relation|attribute)\\.[0-9a-f]{32}$")
string AboxMappingId

/// Counts of the active ABox.
structure DomainAboxCounts {
    /// Typed instances.
    @required
    instances: Integer

    /// Relations between instances.
    @required
    relations: Integer

    /// Attribute values.
    @required
    attributes: Integer

    /// Evidence records, one per supporting chunk.
    @required
    evidence: Integer
}

/// Instance count of one class.
structure DomainClassCount {
    /// Class IRI.
    @required
    classIri: String

    /// Class label.
    @required
    label: String

    /// Instances of the class.
    @required
    count: Integer
}

/// Instance counts by class.
list DomainClassCountList {
    member: DomainClassCount
}

/// Strings such as labels, keys or chunk ids.
list DomainStringList {
    member: String
}

/// One instance in a listing.
structure DomainInstanceSummary {
    /// Instance IRI.
    @required
    iri: String

    /// Class IRI.
    @required
    classIri: String

    /// Class label.
    @required
    classLabel: String

    /// Main label: the most frequent surface form.
    @required
    label: String

    /// Other surface forms seen in documents.
    @required
    altLabels: DomainStringList

    /// Evidence records about the instance.
    @required
    evidenceCount: Integer
}

/// Instances in a listing.
list DomainInstanceSummaryList {
    member: DomainInstanceSummary
}

/// A class or other term with its label.
structure DomainTerm {
    /// Term IRI.
    @required
    iri: String

    /// Term label.
    @required
    label: String
}

/// Terms with labels.
list DomainTermList {
    member: DomainTerm
}

/// An attribute value of an instance.
structure DomainAttribute {
    /// Datatype property or free-attribute IRI.
    @required
    property: String

    /// Property label.
    @required
    label: String

    /// Value as text.
    @required
    value: String

    /// True when the attribute is not in the ontology (a free attribute).
    @required
    free: Boolean

    /// Chunk ids supporting the value.
    @required
    evidence: DomainStringList
}

/// Attribute values.
list DomainAttributeList {
    member: DomainAttribute
}

/// The instance at the other end of a relation.
structure DomainLinkedInstance {
    /// Instance IRI.
    @required
    iri: String

    /// Instance label.
    @required
    label: String

    /// Instance class IRI.
    @required
    classIri: String
}

/// A relation from this instance.
structure DomainOutgoingLink {
    /// Object property IRI.
    @required
    property: String

    /// Property label.
    @required
    label: String

    /// Related instance.
    @required
    target: DomainLinkedInstance

    /// Chunk ids supporting the relation.
    @required
    evidence: DomainStringList
}

/// Relations from this instance.
list DomainOutgoingLinkList {
    member: DomainOutgoingLink
}

/// A relation to this instance.
structure DomainIncomingLink {
    /// Object property IRI.
    @required
    property: String

    /// Property label.
    @required
    label: String

    /// Related instance.
    @required
    source: DomainLinkedInstance
}

/// Relations to this instance. At most 200 are returned, ordered by source IRI.
list DomainIncomingLinkList {
    member: DomainIncomingLink
}

/// A chunk supporting facts about the instance.
structure DomainEvidence {
    /// Lexical-graph chunk id.
    @required
    chunkId: String

    /// Document source id.
    @required
    docSourceId: String

    /// Source object key of the document.
    @required
    sourceKey: String

    /// Chunk text, truncated to 1000 characters.
    @required
    text: String
}

/// Supporting chunks. At most 50 are returned, ordered by chunk id.
list DomainEvidenceList {
    member: DomainEvidence
}

/// Which parts of an instance were cut to bound the response. A part that is cut is the first
/// page of a larger set: its other members exist but are not returned.
structure DomainInstanceTruncation {
    /// True when the instance has more than 200 incoming relations and only the first 200, ordered by source IRI, are returned.
    @required
    incoming: Boolean

    /// True when more than 50 chunks support facts about the instance and only the first 50, ordered by chunk id, are returned.
    @required
    evidence: Boolean
}

/// One row of the mapping table.
structure AboxMapping {
    /// Mapping id (`{kind}.{hash}`).
    @required
    id: String

    /// Row kind.
    @required
    kind: AboxMappingKind

    /// Lexical key: [class] for class rows, [relation, subject class, object class] for relation rows, [subject class, predicate] for attribute rows.
    @required
    key: DomainStringList

    /// Target IRI, `ignore`, or `free` (attribute rows).
    @required
    target: String

    /// Target label.
    @required
    targetLabel: String

    /// Direction of a relation row.
    @required
    direction: AboxMappingDirection

    /// Who decided the row.
    @required
    origin: AboxMappingOrigin

    /// Whether the row applies to the active version.
    @required
    status: AboxMappingStatus

    /// One-line reason of an automatic decision.
    @required
    rationale: String

    /// Ontology version the automatic decision was made for.
    decidedForVersion: Integer

    /// Who last changed the row.
    @required
    updatedBy: String

    /// When the row last changed.
    @required
    updatedAt: String
}

/// Mapping rows.
list AboxMappingList {
    member: AboxMapping
}

/// State of the namespace's domain ABox: build status, the version it conforms to, counts.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/abox", code: 200)
operation GetDomainAbox {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output := {
        /// Build status.
        @required
        status: DomainAboxStatus

        /// True when the ABox conforms to the version the namespace serves.
        @required
        current: Boolean

        /// Ontology version the ABox conforms to; absent when there is no ABox.
        conformsToVersion: Integer

        /// Ontology version the namespace serves; absent when none is deployed.
        activeVersion: Integer

        /// Id of the build that produced the ABox.
        buildId: String

        /// When that build finished.
        builtAt: String

        /// Counts of the active ABox.
        @required
        counts: DomainAboxCounts

        /// Instance counts by class.
        @required
        classCounts: DomainClassCountList

        /// Digest of the mapping rows applied.
        mappingRevision: String

        /// True when a rebuild is queued.
        @required
        pending: Boolean

        /// Error of the last failed build.
        lastError: String
    }
}

/// List the active ABox's instances, filtered by class and label, one page at a time.
///
/// Paging is by position in the label-ordered listing, so it stops 10000 instances in (see
/// `nextToken`): a deeper position is an expensive graph scan. Narrow the listing with `classIri`
/// or `q` to reach further.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "instances")
@http(method: "GET", uri: "/namespaces/{namespaceId}/abox/instances", code: 200)
operation ListDomainInstances {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Only instances of this class.
        @httpQuery("classIri")
        classIri: VertexIri

        /// Case-insensitive substring of the main label, at most 200 characters.
        @length(max: 200)
        @httpQuery("q")
        q: String

        /// Maximum number of instances to return in one page (default 50).
        @range(min: 1, max: 200)
        @httpQuery("maxResults")
        maxResults: Integer

        /// Pagination token from a previous response. A page starts at most 10000 instances into
        /// the listing: past that, `nextToken` is absent even when more instances match. A malformed
        /// token, or one from another namespace, is a 400.
        @httpQuery("nextToken")
        nextToken: PaginationToken
    }

    output := with [PaginatedOutput] {
        /// Instances on this page.
        @required
        instances: DomainInstanceSummaryList
    }

    errors: [
        ConflictError
    ]
}

/// Get one instance with its attributes, relations and supporting chunks. The IRI is a query
/// parameter, not a path label: API Gateway mangles an encoded `#` in a path segment.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/abox/instance", code: 200)
operation GetDomainInstance {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Instance IRI.
        @required
        @httpQuery("iri")
        iri: VertexIri
    }

    output := {
        /// Instance IRI.
        @required
        iri: String

        /// Classes of the instance.
        @required
        classes: DomainTermList

        /// Main label.
        @required
        label: String

        /// Other surface forms.
        @required
        altLabels: DomainStringList

        /// Attribute values.
        @required
        attributes: DomainAttributeList

        /// Relations from the instance.
        @required
        outgoing: DomainOutgoingLinkList

        /// Relations to the instance (at most 200).
        @required
        incoming: DomainIncomingLinkList

        /// Chunks supporting facts about the instance (at most 50).
        @required
        evidence: DomainEvidenceList

        /// Which of `incoming` and `evidence` were cut at their limits.
        @required
        truncated: DomainInstanceTruncation
    }

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// List the mapping table, one page at a time: lexical classes, relations and attributes and the
/// ontology terms they map to. Rows come ordered by kind, then by mapping id. Keep paging until
/// `nextToken` is absent; a page may hold fewer rows than `maxResults` (also none) when `status`
/// filters rows out.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "mappings")
@http(method: "GET", uri: "/namespaces/{namespaceId}/abox/mappings", code: 200)
operation ListAboxMappings {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        /// Only rows of this kind.
        @httpQuery("kind")
        kind: AboxMappingKind

        /// Only rows with this status.
        @httpQuery("status")
        status: AboxMappingStatus

        /// Maximum number of rows to return in one page (default 100).
        @range(min: 1, max: 200)
        @httpQuery("maxResults")
        maxResults: Integer

        /// Pagination token from a previous response. A malformed token, or one from another
        /// namespace or filter, is a 400.
        @httpQuery("nextToken")
        nextToken: PaginationToken
    }

    output := with [PaginatedOutput] {
        /// Mapping rows on this page.
        @required
        mappings: AboxMappingList
    }

    errors: [
        ConflictError
    ]
}

/// Set a mapping row's target by hand; automatic decisions never overwrite it. Requests a rebuild.
@idempotent
@http(method: "PUT", uri: "/namespaces/{namespaceId}/abox/mappings/{mappingId}", code: 200)
operation UpdateAboxMapping {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        mappingId: AboxMappingId

        /// Target IRI in the active ontology version, `ignore` (the row is then reported with status `ignored`; class and relation rows), or `free` (attribute rows).
        @required
        @length(min: 1, max: 2048)
        target: String

        /// Direction (relation rows only; default forward). The property's domain and range must admit the
        /// row's classes in this direction; otherwise the request is refused with 422.
        direction: AboxMappingDirection
    }

    output: AboxMapping

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Return a mapping row to automatic decision; it is decided again by the next build, which this requests.
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/abox/mappings/{mappingId}", code: 200)
operation ResetAboxMapping {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        mappingId: AboxMappingId
    }

    output: AboxMapping

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Queue a rebuild of the domain ABox. Returns 409 only when the namespace has no ontology at all: neither an active-version record nor any stored version. After the domain ontology is deleted no version is active, yet the rebuild is still queued (202), and that build clears the ABox.
@http(method: "POST", uri: "/namespaces/{namespaceId}/abox/rebuild", code: 202)
operation RebuildDomainAbox {
    input := {
        /// Namespace id.
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output := {
        /// True when the rebuild was queued.
        @required
        requested: Boolean
    }

    errors: [
        ConflictError
    ]
}
