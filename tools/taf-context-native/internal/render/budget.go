package render

// Budget handling is intentionally kept in render.go: the output loop must
// update ranks, counts, character accounting, and encoded bytes atomically.
