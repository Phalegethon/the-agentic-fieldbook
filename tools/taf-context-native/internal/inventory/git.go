package inventory

import (
	"crypto/sha1"
	endian "encoding/binary"
	"errors"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
)

const (
	maximumGitIndexBytes                int64 = 32 << 20
	maximumGitIndexEntries                    = 250000
	maximumGitIndexPathBytes                  = 4096
	maximumGitIndexDecodedPathBytes           = 16 << 20
	maximumGitIndexPathComponents             = 256
	maximumGitIndexDecodedComponents          = 1 << 20
	maximumGitIndexDerivedMetadataBytes       = 32 << 20
	gitIndexDerivedBytesPerPath               = 24 // string header, mode, and bounded slice overhead.
)

var errSplitIndex = errors.New("split Git index is unsupported")

type gitIndex struct {
	paths []string
	modes []uint32
}

func (index gitIndex) isTracked(relative string) bool {
	_, ok := index.mode(relative)
	return ok
}

func (index gitIndex) isGitlink(relative string) bool {
	mode, ok := index.mode(relative)
	return ok && mode == 0o160000
}

func (index gitIndex) hasTrackedDescendant(directory string) bool {
	prefix := directory + "/"
	position := sort.SearchStrings(index.paths, prefix)
	return position < len(index.paths) && strings.HasPrefix(index.paths[position], prefix)
}

func (index gitIndex) mode(relative string) (uint32, bool) {
	position := sort.SearchStrings(index.paths, relative)
	if position >= len(index.paths) || index.paths[position] != relative {
		return 0, false
	}
	return index.modes[position], true
}

func trackedRepositoryPaths(roots boundary.Roots) (gitIndex, string) {
	index, err := roots.OpenGitMetadataFile("index", maximumGitIndexBytes)
	if errors.Is(err, boundary.ErrGitMetadataNotFound) {
		return gitIndex{}, ""
	}
	if err != nil {
		return gitIndex{}, "git-index-unreadable"
	}
	tracked, err := parseGitIndex(index.Bytes)
	if errors.Is(err, errSplitIndex) {
		return gitIndex{}, "git-index-split-unsupported"
	}
	if err != nil {
		return gitIndex{}, "git-index-invalid"
	}
	return tracked, ""
}

func parseGitIndex(contents []byte) (gitIndex, error) {
	if int64(len(contents)) > maximumGitIndexBytes || len(contents) < 12+20 || string(contents[:4]) != "DIRC" {
		return gitIndex{}, errors.New("invalid index header")
	}
	if digest := sha1.Sum(contents[:len(contents)-20]); string(digest[:]) != string(contents[len(contents)-20:]) {
		return gitIndex{}, errors.New("invalid index checksum")
	}
	version, count := endian.BigEndian.Uint32(contents[4:8]), endian.BigEndian.Uint32(contents[8:12])
	if version < 2 || version > 4 {
		return gitIndex{}, errors.New("unsupported index version")
	}
	if count > maximumGitIndexEntries || uint64(count) > uint64((len(contents)-32)/64) {
		return gitIndex{}, errors.New("unbounded index entry count")
	}
	tracked := gitIndex{paths: make([]string, 0, int(count)), modes: make([]uint32, 0, int(count))}
	offset := 12
	previous := ""
	previousStage := uint16(0)
	decodedPathBytes := 0
	decodedComponents := 0
	derivedMetadataBytes := 0
	for entry := uint32(0); entry < count; entry++ {
		start := offset
		if offset+62 > len(contents)-20 {
			return gitIndex{}, errors.New("truncated index entry")
		}
		flags := endian.BigEndian.Uint16(contents[offset+60 : offset+62])
		mode := endian.BigEndian.Uint32(contents[offset+24 : offset+28])
		if mode != 0o100644 && mode != 0o100755 && mode != 0o120000 && mode != 0o160000 {
			return gitIndex{}, errors.New("unsupported index mode")
		}
		if version == 2 && flags&0x4000 != 0 {
			return gitIndex{}, errors.New("v2 extended flags")
		}
		nameLength := int(flags & 0x0fff)
		stage := (flags >> 12) & 0x3
		offset += 62
		if flags&0x4000 != 0 {
			if offset+2 > len(contents)-20 {
				return gitIndex{}, errors.New("truncated extended flags")
			}
			extended := endian.BigEndian.Uint16(contents[offset : offset+2])
			if extended & ^uint16(0x6000) != 0 {
				return gitIndex{}, errors.New("unsupported extended flags")
			}
			offset += 2
		}
		name := ""
		if version == 4 {
			strip, consumed, ok := gitIndexVarint(contents[offset : len(contents)-20])
			if !ok || strip > len(previous) {
				return gitIndex{}, errors.New("invalid v4 pathname prefix")
			}
			offset += consumed
			end := indexNUL(contents, offset)
			if end < 0 {
				return gitIndex{}, errors.New("unterminated v4 pathname")
			}
			kept := len(previous) - strip
			suffix := contents[offset:end]
			if kept > maximumGitIndexPathBytes || len(suffix) > maximumGitIndexPathBytes-kept {
				return gitIndex{}, errors.New("decoded index pathname too long")
			}
			var decoded strings.Builder
			decoded.Grow(kept + len(suffix))
			decoded.WriteString(previous[:kept])
			_, _ = decoded.Write(suffix)
			name = decoded.String()
			offset = end + 1
		} else {
			end := indexNUL(contents, offset)
			if end < 0 {
				return gitIndex{}, errors.New("unterminated pathname")
			}
			if end-offset > maximumGitIndexPathBytes {
				return gitIndex{}, errors.New("decoded index pathname too long")
			}
			name = string(contents[offset:end])
			offset = end + 1
			padded := start + ((offset-start+7)/8)*8
			if padded > len(contents)-20 {
				return gitIndex{}, errors.New("truncated index padding")
			}
			for index := offset; index < padded; index++ {
				if contents[index] != 0 {
					return gitIndex{}, errors.New("nonzero index padding")
				}
			}
			offset = padded
		}
		if (nameLength != 0x0fff && len(name) != nameLength) || (nameLength == 0x0fff && len(name) < 0x0fff) {
			return gitIndex{}, errors.New("invalid index pathname length")
		}
		components, safe := safeIndexPath(name)
		if !safe {
			return gitIndex{}, errors.New("unsafe tracked pathname")
		}
		if decodedPathBytes > maximumGitIndexDecodedPathBytes-len(name) {
			return gitIndex{}, errors.New("aggregate decoded index path limit")
		}
		if decodedComponents > maximumGitIndexDecodedComponents-components {
			return gitIndex{}, errors.New("aggregate decoded index component limit")
		}
		decodedPathBytes += len(name)
		decodedComponents += components
		if entry > 0 && (name < previous || (name == previous && stage <= previousStage)) {
			return gitIndex{}, errors.New("unordered index entries")
		}
		if len(tracked.paths) == 0 || tracked.paths[len(tracked.paths)-1] != name {
			derived := len(name) + gitIndexDerivedBytesPerPath
			if derivedMetadataBytes > maximumGitIndexDerivedMetadataBytes-derived {
				return gitIndex{}, errors.New("derived index metadata limit")
			}
			derivedMetadataBytes += derived
			tracked.paths = append(tracked.paths, name)
			tracked.modes = append(tracked.modes, mode)
		} else if mode == 0o160000 {
			// Any unmerged gitlink stage is conservatively a subtree boundary.
			tracked.modes[len(tracked.modes)-1] = mode
		}
		previous = name
		previousStage = stage
	}
	for offset < len(contents)-20 {
		if offset+8 > len(contents)-20 {
			return gitIndex{}, errors.New("truncated index extension")
		}
		signature := string(contents[offset : offset+4])
		size := endian.BigEndian.Uint32(contents[offset+4 : offset+8])
		offset += 8
		if uint64(size) > uint64(len(contents)-20-offset) {
			return gitIndex{}, errors.New("invalid index extension")
		}
		if signature == "link" {
			return gitIndex{}, errSplitIndex
		}
		if signature[0] < 'A' || signature[0] > 'Z' {
			return gitIndex{}, errors.New("unknown mandatory index extension")
		}
		offset += int(size)
	}
	return tracked, nil
}

func indexNUL(contents []byte, offset int) int {
	for index := offset; index < len(contents)-20; index++ {
		if contents[index] == 0 {
			return index
		}
	}
	return -1
}

func gitIndexVarint(contents []byte) (int, int, bool) {
	value := 0
	for index, byteValue := range contents {
		value = (value << 7) | int(byteValue&0x7f)
		if byteValue&0x80 == 0 {
			return value, index + 1, true
		}
		value++
		if index == 4 {
			break
		}
	}
	return 0, 0, false
}

func safeIndexPath(value string) (int, bool) {
	if value == "" || len(value) > maximumGitIndexPathBytes || !utf8.ValidString(value) || value[0] == '/' || strings.ContainsAny(value, "\\\x00") {
		return 0, false
	}
	components := 0
	start := 0
	for index := 0; index <= len(value); index++ {
		if index != len(value) && value[index] != '/' {
			continue
		}
		component := value[start:index]
		components++
		if component == "" || component == "." || component == ".." || strings.EqualFold(component, ".git") || components > maximumGitIndexPathComponents {
			return 0, false
		}
		start = index + 1
	}
	return components, true
}
