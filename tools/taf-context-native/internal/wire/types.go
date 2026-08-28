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
)

var operations = [...]Operation{Estimate, Build, Update, StatusOperation, Metrics, RepositoryMap, SearchSymbols, SearchDocs, SourceSnippets}

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

type Request struct {
	SchemaVersion                string    `json:"schema_version"`
	RequestIdentity              string    `json:"request_identity"`
	ConsumerIdentity             string    `json:"consumer_identity"`
	Operation                    Operation `json:"operation"`
	RepositoryIdentity           string    `json:"repository_identity"`
	WorktreeIdentity             string    `json:"worktree_identity"`
	CommittedHead                string    `json:"committed_head"`
	DirtyOverlayFingerprint      string    `json:"dirty_overlay_fingerprint"`
	ProviderIdentity             string    `json:"provider_identity"`
	IndexIdentity                *string   `json:"index_identity"`
	RequiredCapability           string    `json:"required_capability"`
	MinimumFreshness             string    `json:"minimum_freshness"`
	Query                        *string   `json:"query"`
	ResultIdentities             []string  `json:"result_identities"`
	Filters                      Filters   `json:"filters"`
	MaximumResults               int       `json:"maximum_results"`
	MaximumModelOutputCharacters int       `json:"maximum_model_output_characters"`
	AllowInferred                bool      `json:"allow_inferred"`
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
}
