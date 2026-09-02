package store

import "testing"

func TestQueryShortNameIsTheLastDottedSegment(t *testing.T) {
	cases := map[string]string{
		"git_snapshot.collect_snapshot":     "collect_snapshot",
		"query.Search":                      "search",
		"The Agentic Fieldbook.Install TAF": "install taf",
		"Changelog.[Unreleased]#chunk-1":    "[unreleased]#chunk-1",
		"HTTPServer.parse_value-name":       "parse_value-name",
		"---":                               "---",
		"trailing.":                         "",
		"":                                  "",
	}
	for input, want := range cases {
		if got := QueryShortName(input); got != want {
			t.Fatalf("QueryShortName(%q) = %q, want %q", input, got, want)
		}
	}
}
