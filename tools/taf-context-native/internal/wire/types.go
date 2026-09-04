// Package wire defines the strict, portable Level 1 JSON boundary.
package wire

import "errors"

var (
	ErrDuplicateKey       = errors.New("duplicate JSON object key")
	ErrRequiredCapability = errors.New("required capability does not match operation")
	ErrInvalidWire        = errors.New("invalid Level 1 wire record")
)

type Operation string

const (
	Estimate        Operation = "estimate"
	Build           Operation = "build"
	Update          Operation = "update"
	StatusOperation Operation = "status"
	Metrics         Operation = "metrics"
	RepositoryMap   Operation = "repository-map"
	SearchSymbols   Operation = "search-symbols"
	SearchDocs      Operation = "search-docs"
	SourceSnippets  Operation = "source-snippets"
	RelatedSymbols  Operation = "related-symbols"
	ChangedSymbols  Operation = "changed-symbols"
	// RepositoryOverview answers how a repository is organized: directory
	// groups with counts plus a ranked file layer, under schema 4.
	RepositoryOverview Operation = "repository-overview"
)

var operations = [...]Operation{Estimate, Build, Update, StatusOperation, Metrics, RepositoryMap, SearchSymbols, SearchDocs, SourceSnippets, RelatedSymbols, ChangedSymbols, RepositoryOverview}

// Operations returns a copy of the frozen operation vocabulary.
func Operations() []Operation { return append([]Operation(nil), operations[:]...) }

type Status string

const (
	Ready       Status = "ready"
	Partial     Status = "partial"
	Stale       Status = "stale"
	Unsupported Status = "unsupported"
	Error       Status = "error"
)

type Envelope struct {
	Phase                string  `json:"phase"`
	RepositoryRoot       string  `json:"repository_root"`
	StateRoot            string  `json:"state_root"`
	ChangedPathsDocument *string `json:"changed_paths_document"`
	Request              Request `json:"request"`
}

type Filters struct {
	PathPrefixes []string `json:"path_prefixes"`
	Languages    []string `json:"languages"`
	SymbolKinds  []string `json:"symbol_kinds"`
	SourceTypes  []string `json:"source_types"`
}

// ChangedRange names one changed path and the changed line spans inside it.
// An empty Ranges means the whole file changed; every span is an inclusive
// [start, end] line pair.
type ChangedRange struct {
	Path   string   `json:"path"`
	Ranges [][2]int `json:"ranges"`
}

// Request carries the frozen schema-1 keys, the schema-2 direction, and the
// schema-3 changed-range selector. Both added tags are omitempty so a marshaled
// schema-1 request keeps its frozen key set exactly; schema-2 producers spell
// direction out and schema-3 producers spell both out, null included.
type Request struct {
	SchemaVersion                string          `json:"schema_version"`
	RequestIdentity              string          `json:"request_identity"`
	ConsumerIdentity             string          `json:"consumer_identity"`
	Operation                    Operation       `json:"operation"`
	RepositoryIdentity           string          `json:"repository_identity"`
	WorktreeIdentity             string          `json:"worktree_identity"`
	CommittedHead                string          `json:"committed_head"`
	DirtyOverlayFingerprint      string          `json:"dirty_overlay_fingerprint"`
	ProviderIdentity             string          `json:"provider_identity"`
	IndexIdentity                *string         `json:"index_identity"`
	RequiredCapability           string          `json:"required_capability"`
	MinimumFreshness             string          `json:"minimum_freshness"`
	Query                        *string         `json:"query"`
	ResultIdentities             []string        `json:"result_identities"`
	Direction                    *string         `json:"direction,omitempty"`
	ChangedRanges                *[]ChangedRange `json:"changed_ranges,omitempty"`
	Filters                      Filters         `json:"filters"`
	MaximumResults               int             `json:"maximum_results"`
	MaximumModelOutputCharacters int             `json:"maximum_model_output_characters"`
	AllowInferred                bool            `json:"allow_inferred"`
}

type Result struct {
	SchemaVersion           string            `json:"schema_version"`
	RequestIdentity         string            `json:"request_identity"`
	Operation               Operation         `json:"operation"`
	Status                  Status            `json:"status"`
	ProviderIdentity        string            `json:"provider_identity"`
	ProviderVersion         string            `json:"provider_version"`
	IndexIdentity           *string           `json:"index_identity"`
	RepositoryIdentity      string            `json:"repository_identity"`
	WorktreeIdentity        string            `json:"worktree_identity"`
	CommittedHead           string            `json:"committed_head"`
	DirtyOverlayFingerprint string            `json:"dirty_overlay_fingerprint"`
	Freshness               string            `json:"freshness"`
	ParserVersions          map[string]string `json:"parser_versions"`
	Coverage                Coverage          `json:"coverage"`
	Findings                []Finding         `json:"findings"`
	ReturnedCount           int               `json:"returned_count"`
	OmittedCount            int               `json:"omitted_count"`
	Truncated               bool              `json:"truncated"`
	OutputCharacters        int               `json:"output_characters"`
	Warnings                []string          `json:"warnings"`
	NextSafeAction          string            `json:"next_safe_action"`
	// Groups and Overview exist only in schema 4. Both tags are omitempty so a
	// marshaled schema-1, -2, or -3 result keeps its frozen key set exactly,
	// which also means marshaling drops them when they are nil: a schema-4
	// result must set both, and the validator refuses one that does not.
	Groups   *[]OverviewGroup `json:"groups,omitempty"`
	Overview *OverviewSummary `json:"overview,omitempty"`
}

// OverviewLanguage names one language inside a group and how many of the
// group's files carry it. A group's list is ordered by FileCount descending,
// then Language ascending, so equal counts still order deterministically.
type OverviewLanguage struct {
	Language  string `json:"language"`
	FileCount int    `json:"file_count"`
}

// OverviewGroup is one directory group of the repository overview. PathPrefix
// is relative to the repository root and is either "." (the files at the root),
// "*" (the row the surplus groups are folded into), a directory prefix ending
// in "/", or "<directory>/." (the files directly inside a split directory).
// Depth counts the segments below the overview root, so "." and "*" are 0.
// RepresentativeIdentity cites the group's top-ranked file and is null for "*",
// which sums several directories and therefore represents none of them.
type OverviewGroup struct {
	PathPrefix             string             `json:"path_prefix"`
	Depth                  int                `json:"depth"`
	FileCount              int                `json:"file_count"`
	DefinitionCount        int                `json:"definition_count"`
	EntryPointCount        int                `json:"entry_point_count"`
	DocumentCount          int                `json:"document_count"`
	ConfigurationCount     int                `json:"configuration_count"`
	Languages              []OverviewLanguage `json:"languages"`
	RepresentativeIdentity *string            `json:"representative_identity"`
}

// OverviewSummary states what the group table was derived from: the overview
// root ("" for the repository root, otherwise a directory prefix ending in
// "/"), how many files were counted, and how many groups the "*" row folded.
type OverviewSummary struct {
	Root             string `json:"root"`
	CountedFileCount int    `json:"counted_file_count"`
	OtherGroupCount  int    `json:"other_group_count"`
}

type Coverage struct {
	PathCoverage             float64        `json:"path_coverage"`
	LanguageCoverage         float64        `json:"language_coverage"`
	IndexedPathCount         int            `json:"indexed_path_count"`
	ExcludedPathCount        int            `json:"excluded_path_count"`
	UnsupportedLanguageCount int            `json:"unsupported_language_count"`
	ParseFailureCount        int            `json:"parse_failure_count"`
	ExclusionReasonCounts    map[string]int `json:"exclusion_reason_counts"`
}

type Finding struct {
	Rank             int    `json:"rank"`
	ResultIdentity   string `json:"result_identity"`
	Path             string `json:"path"`
	StartLine        int    `json:"start_line"`
	EndLine          int    `json:"end_line"`
	Language         string `json:"language"`
	RecordKind       string `json:"record_kind"`
	SourceType       string `json:"source_type"`
	QualifiedName    string `json:"qualified_name"`
	ExtractionMethod string `json:"extraction_method"`
	EvidenceClass    string `json:"evidence_class"`
	Preview          string `json:"preview"`
	// The four edge fields below exist only in schema 2; a schema-1 result is
	// marshaled through findingV1, which drops them entirely.
	Relation       string `json:"relation"`
	EdgeEvidence   string `json:"edge_evidence"`
	ReferenceLine  int    `json:"reference_line"`
	ReferenceCount int    `json:"reference_count"`
}
