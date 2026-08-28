// Package policy exposes the immutable production ceilings for the native Level 1 engine.
package policy

type Limits struct {
	SchemaVersion               string  `json:"schema_version"`
	MaximumWireBytes            int     `json:"maximum_wire_bytes"`
	MaximumStdoutBytes          int     `json:"maximum_stdout_bytes"`
	MaximumStderrBytes          int     `json:"maximum_stderr_bytes"`
	MaximumCollectionItems      int     `json:"maximum_collection_items"`
	MaximumEligiblePaths        int     `json:"maximum_eligible_paths"`
	MaximumChangedPaths         int     `json:"maximum_changed_paths"`
	MaximumEligibleSourceBytes  uint64  `json:"maximum_eligible_source_bytes"`
	MaximumSourceFileBytes      int     `json:"maximum_source_file_bytes"`
	MaximumMarkdownFileBytes    int     `json:"maximum_markdown_file_bytes"`
	MaximumLexicalCandidates    int     `json:"maximum_lexical_candidates"`
	MaximumFuzzyTerms           int     `json:"maximum_fuzzy_terms"`
	MaximumFuzzyDistance        int     `json:"maximum_fuzzy_distance"`
	BuildLatencyNSMaximum       int64   `json:"build_latency_ns_maximum"`
	QueryLatencyP95NSMaximum    int64   `json:"query_latency_p95_ns_maximum"`
	UpdateLatencyNSMaximum      int64   `json:"update_latency_ns_maximum"`
	PeakMemoryBytesMaximum      int     `json:"peak_memory_bytes_maximum"`
	StorageToSourceRatioMaximum float64 `json:"storage_to_source_ratio_maximum"`
}

var ProductionV1 = Limits{
	SchemaVersion: "1", MaximumWireBytes: 262144, MaximumStdoutBytes: 262144,
	MaximumStderrBytes: 65536, MaximumCollectionItems: 64, MaximumEligiblePaths: 250000,
	MaximumChangedPaths: 10000, MaximumEligibleSourceBytes: 4294967296,
	MaximumSourceFileBytes: 2097152, MaximumMarkdownFileBytes: 8388608,
	MaximumLexicalCandidates: 4096, MaximumFuzzyTerms: 64, MaximumFuzzyDistance: 2,
	BuildLatencyNSMaximum: 10000000000, QueryLatencyP95NSMaximum: 150000000,
	UpdateLatencyNSMaximum: 2000000000, PeakMemoryBytesMaximum: 536870912,
	StorageToSourceRatioMaximum: 1.5,
}
