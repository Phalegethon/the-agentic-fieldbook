package model

import (
	"reflect"
	"strings"
	"testing"
)

func TestFormatReferenceTableRendersTheCanonicalForm(t *testing.T) {
	table := FormatReferenceTable([]ReferenceEntry{
		{Name: "helpers.load", Line: 5, Count: 2},
		{Name: "osp.join", Line: 7, Count: 1},
	})
	if table != "helpers.load:5:2;osp.join:7:1" {
		t.Fatalf("table = %q", table)
	}
	if FormatReferenceTable(nil) != "" {
		t.Fatalf("empty table = %q", FormatReferenceTable(nil))
	}
}

func TestParseReferenceTableReturnsEveryEntry(t *testing.T) {
	entries, ok := ParseReferenceTable("helpers.load:5:2;osp.join:7:1;run:9:14")
	if !ok {
		t.Fatal("table did not parse")
	}
	want := []ReferenceEntry{
		{Name: "helpers.load", Line: 5, Count: 2},
		{Name: "osp.join", Line: 7, Count: 1},
		{Name: "run", Line: 9, Count: 14},
	}
	if !reflect.DeepEqual(entries, want) {
		t.Fatalf("entries = %#v, want %#v", entries, want)
	}
}

func TestParseReferenceTableRoundTripsWhatFormatWrites(t *testing.T) {
	want := []ReferenceEntry{{Name: "a", Line: 1, Count: 1}, {Name: "b.c", Line: 2, Count: 4294967294}}
	entries, ok := ParseReferenceTable(FormatReferenceTable(want))
	if !ok || !reflect.DeepEqual(entries, want) {
		t.Fatalf("entries = %#v, ok = %v, want %#v", entries, ok, want)
	}
}

func TestParseReferenceTableRejectsMalformedTables(t *testing.T) {
	for name, table := range map[string]string{
		"empty":                  "",
		"missing count":          "load:5",
		"missing line":           "load",
		"trailing separator":     "load:5:1;",
		"leading separator":      ";load:5:1",
		"empty entry":            "load:5:1;;join:6:1",
		"empty name":             ":5:1",
		"zero line":              "load:0:1",
		"zero count":             "load:5:0",
		"negative line":          "load:-5:1",
		"leading zero line":      "load:05:1",
		"leading zero count":     "load:5:01",
		"non numeric line":       "load:five:1",
		"non numeric count":      "load:5:one",
		"space in name":          "os path:5:1",
		"tab in name":            "os\tpath:5:1",
		"control byte in name":   "os\x00path:5:1",
		"newline in name":        "os\npath:5:1",
		"extra field":            "load:5:1:2",
		"line above uint32":      "load:4294967296:1",
		"count above uint32":     "load:5:4294967296",
		"name above the maximum": strings.Repeat("x", MaximumReferenceTargetBytes+1) + ":5:1",
	} {
		t.Run(name, func(t *testing.T) {
			if entries, ok := ParseReferenceTable(table); ok {
				t.Fatalf("table %q parsed as %#v", table, entries)
			}
		})
	}
}

func TestParseReferenceTableRejectsTablesBeyondTheBounds(t *testing.T) {
	entries := make([]ReferenceEntry, 0, MaximumReferenceTableEntries+1)
	for index := range MaximumReferenceTableEntries + 1 {
		entries = append(entries, ReferenceEntry{Name: "name" + strings.Repeat("0", index%3), Line: index + 1, Count: 1})
	}
	if _, ok := ParseReferenceTable(FormatReferenceTable(entries)); ok {
		t.Fatalf("a table of %d entries parsed", len(entries))
	}
	long := "a" + strings.Repeat("b", MaximumReferenceTargetBytes-1) + ":1:1"
	oversized := strings.Repeat(long+";", MaximumReferenceTableBytes/len(long)+1)
	if _, ok := ParseReferenceTable(strings.TrimSuffix(oversized, ";")); ok {
		t.Fatal("a table beyond the byte bound parsed")
	}
}

func TestScanReferenceTableReportsEntryCountAndTotal(t *testing.T) {
	count, total, ok := ScanReferenceTable([]byte("helpers.load:5:2;osp.join:7:1"))
	if !ok || count != 2 || total != 3 {
		t.Fatalf("count = %d, total = %d, ok = %v", count, total, ok)
	}
	if _, _, ok := ScanReferenceTable([]byte("load:5")); ok {
		t.Fatal("a malformed table scanned")
	}
}

func TestValidReferenceTargetNameRejectsUnstableNames(t *testing.T) {
	for _, name := range []string{"load", "helpers.load", "a.b.c", "_x", "Ünicöde"} {
		if !ValidReferenceTargetName(name) {
			t.Fatalf("%q rejected", name)
		}
	}
	for _, name := range []string{"", "a:b", "a;b", "a b", "a\tb", "a\nb", "a\x00b", " x", strings.Repeat("x", MaximumReferenceTargetBytes+1)} {
		if ValidReferenceTargetName(name) {
			t.Fatalf("%q accepted", name)
		}
	}
}
