package inventory

import (
	"crypto/sha1"
	endian "encoding/binary"
	"errors"
	"path"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
)

const maximumGitIndexBytes int64 = 32 << 20
const maximumGitIndexEntries = 250000

var errSplitIndex = errors.New("split Git index is unsupported")

type gitIndex struct {
	modes              map[string]uint32
	trackedDirectories map[string]struct{}
}

func (index gitIndex) isTracked(relative string) bool {
	_, ok := index.modes[relative]
	return ok
}

func (index gitIndex) isGitlink(relative string) bool {
	return index.modes[relative] == 0o160000
}

func (index gitIndex) hasTrackedDescendant(directory string) bool {
	_, ok := index.trackedDirectories[directory]
	return ok
}

func trackedRepositoryPaths(roots boundary.Roots) (gitIndex, string) {
	index, err := roots.OpenGitMetadataFile("index", maximumGitIndexBytes)
	if errors.Is(err, boundary.ErrGitMetadataNotFound) {
		return gitIndex{modes: map[string]uint32{}, trackedDirectories: map[string]struct{}{}}, ""
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
	if len(contents) < 12+20 || string(contents[:4]) != "DIRC" {
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
	tracked := gitIndex{modes: make(map[string]uint32, int(count)), trackedDirectories: make(map[string]struct{})}
	offset := 12
	previous := ""
	previousStage := uint16(0)
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
			name = previous[:len(previous)-strip] + string(contents[offset:end])
			offset = end + 1
		} else {
			end := indexNUL(contents, offset)
			if end < 0 {
				return gitIndex{}, errors.New("unterminated pathname")
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
		if !safeIndexPath(name) {
			return gitIndex{}, errors.New("unsafe tracked pathname")
		}
		if entry > 0 && (name < previous || (name == previous && stage <= previousStage)) {
			return gitIndex{}, errors.New("unordered index entries")
		}
		if existing, ok := tracked.modes[name]; !ok || stage == 0 || existing != 0o160000 {
			tracked.modes[name] = mode
		}
		for index := 0; index < len(name); index++ {
			if name[index] == '/' {
				tracked.trackedDirectories[name[:index]] = struct{}{}
			}
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

func safeIndexPath(value string) bool {
	return value != "" && !strings.ContainsAny(value, "\\\x00") && !strings.HasPrefix(value, "/") && path.Clean(value) == value && value != "." && value != ".." && !strings.HasPrefix(value, "../")
}
