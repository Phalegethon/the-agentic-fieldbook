package query

import (
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
)

// normalize is deliberately locale-independent. Go's Unicode case mapping is
// stable and does not consult process locale; malformed internal values are
// rejected by callers before they can be emitted.
func normalize(value string) string { return store.NormalizeQueryText(value) }

// tokens keeps the lexical contract small and predictable. It recognizes
// ASCII identifier characters plus Unicode letters/digits, and splits common
// separator and camel-case forms without changing the original qualified term.
func tokens(value string) []string {
	return store.QueryTokens(value)
}

// editDistanceAtMost computes a Levenshtein distance with bounded rows. It
// returns limit+1 as soon as a row proves the candidate cannot qualify.
func editDistanceAtMost(left, right string, limit int) int {
	leftRunes, rightRunes := []rune(left), []rune(right)
	if limit < 0 || abs(len(leftRunes)-len(rightRunes)) > limit {
		return limit + 1
	}
	previous := make([]int, len(rightRunes)+1)
	current := make([]int, len(rightRunes)+1)
	for index := range previous {
		previous[index] = index
	}
	for leftIndex := 1; leftIndex <= len(leftRunes); leftIndex++ {
		current[0] = leftIndex
		rowMinimum := current[0]
		for rightIndex := 1; rightIndex <= len(rightRunes); rightIndex++ {
			cost := 0
			if leftRunes[leftIndex-1] != rightRunes[rightIndex-1] {
				cost = 1
			}
			current[rightIndex] = min(previous[rightIndex]+1, current[rightIndex-1]+1, previous[rightIndex-1]+cost)
			if current[rightIndex] < rowMinimum {
				rowMinimum = current[rightIndex]
			}
		}
		if rowMinimum > limit {
			return limit + 1
		}
		previous, current = current, previous
	}
	if previous[len(rightRunes)] > limit {
		return limit + 1
	}
	return previous[len(rightRunes)]
}

func min(values ...int) int {
	result := values[0]
	for _, value := range values[1:] {
		if value < result {
			result = value
		}
	}
	return result
}

func abs(value int) int {
	if value < 0 {
		return -value
	}
	return value
}
