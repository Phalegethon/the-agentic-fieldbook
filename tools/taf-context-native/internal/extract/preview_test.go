package extract

import (
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

func previewOf(t *testing.T, records []model.Record, qualified string) string {
	t.Helper()
	return findRecord(t, records, qualified).Preview
}

func TestDefinitionPreviewIsTheSanitizedFirstLine(t *testing.T) {
	source := "class Runner:\r\n\tdef run(self,  value):\t# \x07bell\r\n\t\treturn value\r\n"
	records, _ := NewRegistry().Extract(stableFile("pkg/runner.py", source))
	if got := previewOf(t, records, "runner.Runner"); got != "class Runner:" {
		t.Fatalf("class preview = %q", got)
	}
	if got := previewOf(t, records, "runner.Runner.run"); got != "def run(self, value): # bell" {
		t.Fatalf("method preview = %q", got)
	}
}

func TestPreviewIsCutAtOneHundredSixtyCodePointsWithEllipsis(t *testing.T) {
	// 200 ASCII bytes: identifiers above 256 bytes are rejected by stableName.
	name := strings.Repeat("u", 200)
	source := "def " + name + "():\n    return 1\n"
	records, _ := NewRegistry().Extract(stableFile("pkg/long.py", source))
	got := previewOf(t, records, "long."+name)
	runes := []rune(got)
	if len(runes) != 160 || runes[159] != '…' || !strings.HasPrefix(got, "def uuu") {
		t.Fatalf("cut preview = %q (%d runes)", got, len(runes))
	}
}

func TestImportsAndConfigurationKeepEmptyPreviews(t *testing.T) {
	records, _ := NewRegistry().Extract(stableFile("pkg/mod.py", "import os\n\ndef f():\n    pass\n"))
	if got := previewOf(t, records, "os"); got != "" {
		t.Fatalf("import preview = %q", got)
	}
	configuration, _ := NewRegistry().Extract(stableFile("plugin.json", "{\"name\": \"taf\"}\n"))
	for _, record := range configuration {
		if record.Preview != "" {
			t.Fatalf("configuration preview = %#v", record)
		}
	}
}

func TestHeadingPreviewIsTheFirstBodyLine(t *testing.T) {
	source := "# Title\n\nFirst sentence here.\nSecond sentence.\n\n## Fenced\n```go\nfunc Example() {}\n```\n\n## Empty\n\n## Setext\n---\n\nBody after setext.\n\n## Last\n"
	records, _ := NewRegistry().Extract(stableFile("docs/guide.md", source))
	cases := map[string]string{
		"Title":        "First sentence here.",
		"Title.Fenced": "func Example() {}",
		"Title.Empty":  "",
		"Title.Setext": "Body after setext.",
		"Title.Last":   "",
	}
	for qualified, want := range cases {
		if got := previewOf(t, records, qualified); got != want {
			t.Fatalf("%s preview = %q, want %q", qualified, got, want)
		}
	}
}

func TestHeadingPreviewStopsWithinTwentyLines(t *testing.T) {
	source := "# Title\n" + strings.Repeat("\n", 25) + "Too far away.\n"
	records, _ := NewRegistry().Extract(stableFile("docs/far.md", source))
	if got := previewOf(t, records, "Title"); got != "" {
		t.Fatalf("far preview = %q, want empty", got)
	}
}

func TestHeadingPreviewStopsAtASetextHeading(t *testing.T) {
	source := "# Title\n\nSome Heading\n------------\n\nActual body content.\n\n# Second\n\nAnother Heading\n===============\n"
	records, _ := NewRegistry().Extract(stableFile("docs/setext.md", source))
	if got := previewOf(t, records, "Title"); got != "" {
		t.Fatalf("Title preview = %q, want empty because the next line is a setext heading", got)
	}
	if got := previewOf(t, records, "Second"); got != "" {
		t.Fatalf("Second preview = %q, want empty", got)
	}
	if got := previewOf(t, records, "Title.Some Heading"); got != "Actual body content." {
		t.Fatalf("setext section preview = %q", got)
	}
}

func TestHeadingPreviewSkipsThematicBreaksAndOrphanUnderlines(t *testing.T) {
	source := "# Title\n\n***\n\n===\n\nBody after breaks.\n"
	records, _ := NewRegistry().Extract(stableFile("docs/breaks.md", source))
	if got := previewOf(t, records, "Title"); got != "Body after breaks." {
		t.Fatalf("preview = %q", got)
	}
}

func TestSanitizePreviewRejectsInvalidUTF8(t *testing.T) {
	if got := sanitizePreview("def \xff():"); got != "" {
		t.Fatalf("invalid utf-8 preview = %q", got)
	}
}
