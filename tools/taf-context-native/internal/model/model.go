package model

type EvidenceClass string

const (
	Verified  EvidenceClass = "verified"
	Inferred  EvidenceClass = "inferred"
	Uncertain EvidenceClass = "uncertain"
)

type RecordKind string

const (
	Module        RecordKind = "module"
	Definition    RecordKind = "definition"
	Import        RecordKind = "import"
	EntryPoint    RecordKind = "entry-point"
	Configuration RecordKind = "configuration"
	Heading       RecordKind = "heading"
	DocumentChunk RecordKind = "document-chunk"
)

type Record struct {
	Identity         string
	Path             string
	StartLine        int
	EndLine          int
	Language         string
	RecordKind       RecordKind
	SourceType       string
	QualifiedName    string
	ExtractionMethod string
	EvidenceClass    EvidenceClass
	SearchTerms      []string
	SourceDigest     string
	Preview          string
}

type Coverage struct {
	PathCoverage             float64
	LanguageCoverage         float64
	IndexedPathCount         int
	ExcludedPathCount        int
	UnsupportedLanguageCount int
	ParseFailureCount        int
	ExclusionReasonCounts    map[string]int
}

type Binding struct {
	RepositoryIdentity      string
	WorktreeIdentity        string
	CommittedHead           string
	DirtyOverlayFingerprint string
}

// SourceCatalog is the canonical, compact source-classification witness kept
// with an immutable generation. It lets update mutate only declared paths
// while retaining build-equivalent coverage and source binding.
type SourcePath struct {
	RelativePath string
	Language     string
	Size         int64
	SHA256       string
}

type SourceExclusion struct {
	RelativePath string
	Reason       string
}

type SourceWarning struct {
	RelativePath string
	Codes        []string
}

type SourceCatalog struct {
	Paths              []SourcePath
	Exclusions         []SourceExclusion
	ExtractionWarnings []SourceWarning
	Partial            bool
	Warnings           []string
}

type Manifest struct {
	FormatVersion           string
	EngineVersion           string
	Binding                 Binding
	InclusionPolicyIdentity string
	ExclusionPolicyIdentity string
	ParserIdentities        map[string]string
	Coverage                Coverage
	RecordCount             int
	PostingCount            int
	SourceBindingDigest     string
	PayloadDigest           string
	IndexIdentity           string
	GenerationIdentity      string
	SemanticDigest          string
	// SourceCatalog is serialized inside the authenticated index payload, not
	// the bounded public manifest JSON.
	SourceCatalog SourceCatalog
}

type WorkCounters struct {
	EligiblePaths         int
	ChangedPaths          int
	EligibleSourceBytes   uint64
	IndexedPaths          int
	ExcludedPaths         int
	ParseFailures         int
	ConsideredRecords     int
	ParsedRepositoryFiles int
	OpenedRepositoryFiles int
	ReadRepositoryBytes   int64
	ReadStateFiles        int
	WrittenStateFiles     int
	WrittenStateBytes     int64
}

type ChangeDocument struct {
	SchemaVersion                 string
	PriorIndexIdentity            string
	BeforeRepositoryIdentity      string
	BeforeWorktreeIdentity        string
	BeforeCommittedHead           string
	BeforeDirtyOverlayFingerprint string
	AfterRepositoryIdentity       string
	AfterWorktreeIdentity         string
	AfterCommittedHead            string
	AfterDirtyOverlayFingerprint  string
	Level0ChangeManifestIdentity  string
	ChangedPaths                  []string
}
