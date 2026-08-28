//go:build tree_sitter_grammars

package wire

// Keep the production parser grammar modules pinned and vendored even before
// later engine tasks bind them to language-specific extraction.
import (
	_ "github.com/tree-sitter/go-tree-sitter"
	_ "github.com/tree-sitter/tree-sitter-javascript/bindings/go"
	_ "github.com/tree-sitter/tree-sitter-python/bindings/go"
	_ "github.com/tree-sitter/tree-sitter-rust/bindings/go"
	_ "github.com/tree-sitter/tree-sitter-typescript/bindings/go"
)
