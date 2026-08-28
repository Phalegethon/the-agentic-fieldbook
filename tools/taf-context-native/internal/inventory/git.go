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

func trackedRepositoryPaths(roots boundary.Roots) (map[string]struct{}, string) {
	index, err := roots.OpenGitMetadataFile("index", maximumGitIndexBytes)
	if errors.Is(err, boundary.ErrGitMetadataNotFound) {
		return map[string]struct{}{}, ""
	}
	if err != nil {
		return nil, "git-index-unreadable"
	}
	tracked, err := parseGitIndex(index.Bytes)
	if err != nil {
		return nil, "git-index-invalid"
	}
	return tracked, ""
}

func parseGitIndex(contents []byte) (map[string]struct{}, error) {
	if len(contents) < 12+20 || string(contents[:4]) != "DIRC" {
		return nil, errors.New("invalid index header")
	}
	if digest := sha1.Sum(contents[:len(contents)-20]); string(digest[:]) != string(contents[len(contents)-20:]) {
		return nil, errors.New("invalid index checksum")
	}
	version, count := endian.BigEndian.Uint32(contents[4:8]), endian.BigEndian.Uint32(contents[8:12])
	if version < 2 || version > 4 {
		return nil, errors.New("unsupported index version")
	}
	tracked := make(map[string]struct{}, count)
	offset := 12
	previous := ""
	for entry := uint32(0); entry < count; entry++ {
		start := offset
		if offset+62 > len(contents)-20 {
			return nil, errors.New("truncated index entry")
		}
		flags := endian.BigEndian.Uint16(contents[offset+60 : offset+62])
		offset += 62
		if flags&0x4000 != 0 {
			if offset+2 > len(contents)-20 {
				return nil, errors.New("truncated extended flags")
			}
			offset += 2
		}
		name := ""
		if version == 4 {
			strip, consumed, ok := gitIndexVarint(contents[offset : len(contents)-20])
			if !ok || strip > len(previous) {
				return nil, errors.New("invalid v4 pathname prefix")
			}
			offset += consumed
			end := indexNUL(contents, offset)
			if end < 0 {
				return nil, errors.New("unterminated v4 pathname")
			}
			name = previous[:len(previous)-strip] + string(contents[offset:end])
			offset = end + 1
		} else {
			end := indexNUL(contents, offset)
			if end < 0 {
				return nil, errors.New("unterminated pathname")
			}
			name = string(contents[offset:end])
			offset = end + 1
			for (offset-start)%8 != 0 {
				offset++
			}
		}
		if !safeIndexPath(name) {
			return nil, errors.New("unsafe tracked pathname")
		}
		tracked[name] = struct{}{}
		previous = name
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
