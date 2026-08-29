package store

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
)

// Faults is a deterministic test-only publication seam. Production Build
// passes the zero value, so no production behavior depends on fault injection.
type Faults struct {
	BeforePayloadSync      error
	BeforePayloadReopen    error
	BeforeManifestSync     error
	BeforeManifestReopen   error
	BeforeReadySync        error
	BeforeGenerationSync   error
	BeforeGenerationRename error
	BeforeGenerationsSync  error
	BeforeCurrentSync      error
	BeforeCurrentReopen    error
	BeforeCurrentRename    error
	BeforeStateSync        error
}

type faultPoint uint8

const (
	faultNone faultPoint = iota
	faultBeforePayloadSync
	faultBeforePayloadReopen
	faultBeforeManifestSync
	faultBeforeManifestReopen
	faultBeforeReadySync
	faultBeforeGenerationSync
	faultBeforeGenerationRename
	faultBeforeGenerationsSync
	faultBeforeCurrentSync
	faultBeforeCurrentReopen
	faultBeforeCurrentRename
	faultBeforeStateSync
)

// storeFilesystem is deliberately narrower than os or io/fs. Its concrete
// implementation can operate only on retained boundary capabilities, while
// its fault points make every durability edge deterministic in tests.
type storeFilesystem interface {
	ensureState(*boundary.Roots) error
	openStateDirectory(*boundary.Roots, string) (*boundary.StateDirectory, error)
	createDirectory(*boundary.StateDirectory, string) error
	openDirectory(*boundary.StateDirectory, string) (*boundary.StateDirectory, error)
	closeDirectory(*boundary.StateDirectory) error
	readFile(*boundary.StateDirectory, string, int64) ([]byte, error)
	readAtomicCurrent(*boundary.StateDirectory, int64) ([]byte, error)
	writeSyncedFile(*boundary.StateDirectory, string, []byte, faultPoint) error
	verifyFile(*boundary.StateDirectory, string, []byte, int64, faultPoint) error
	syncDirectory(*boundary.StateDirectory, faultPoint) error
	renameNew(*boundary.StateDirectory, string, string, faultPoint) error
	replaceFile(*boundary.StateDirectory, string, string, faultPoint) error
	names(*boundary.StateDirectory, int) ([]string, error)
	removeFile(*boundary.StateDirectory, string) error
	removeEmptyDirectory(*boundary.StateDirectory, string) error
}

type boundaryFilesystem struct{ faults Faults }

var _ storeFilesystem = boundaryFilesystem{}

type injectedFilesystemFault struct{ cause error }

func (fault injectedFilesystemFault) Error() string { return fault.cause.Error() }
func (fault injectedFilesystemFault) Unwrap() error { return fault.cause }

func isInjectedFilesystemFault(err error) bool {
	var fault injectedFilesystemFault
	return errors.As(err, &fault)
}

func (filesystem boundaryFilesystem) ensureState(roots *boundary.Roots) error {
	return roots.EnsureState()
}

func (filesystem boundaryFilesystem) openStateDirectory(roots *boundary.Roots, relative string) (*boundary.StateDirectory, error) {
	return roots.OpenStateDirectory(relative)
}

func (filesystem boundaryFilesystem) createDirectory(directory *boundary.StateDirectory, name string) error {
	return directory.CreateDirectory(name)
}

func (filesystem boundaryFilesystem) openDirectory(directory *boundary.StateDirectory, name string) (*boundary.StateDirectory, error) {
	return directory.OpenDirectory(name)
}

func (filesystem boundaryFilesystem) closeDirectory(directory *boundary.StateDirectory) error {
	return directory.Close()
}

func (filesystem boundaryFilesystem) readFile(directory *boundary.StateDirectory, name string, maximum int64) ([]byte, error) {
	return directory.ReadFile(name, maximum)
}

func (filesystem boundaryFilesystem) readAtomicCurrent(directory *boundary.StateDirectory, exactLength int64) ([]byte, error) {
	return directory.ReadAtomicCurrent(exactLength)
}

func (filesystem boundaryFilesystem) writeSyncedFile(directory *boundary.StateDirectory, name string, contents []byte, point faultPoint) error {
	file, err := directory.CreateFile(name)
	if err != nil {
		return err
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	for remaining := contents; len(remaining) != 0; {
		written, writeErr := file.Write(remaining)
		if writeErr != nil || written <= 0 || written > len(remaining) {
			return ErrStoreCorrupt
		}
		remaining = remaining[written:]
	}
	if err := filesystem.before(point); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("%w: %v", ErrStoreCorrupt, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("%w: %v", ErrStoreCorrupt, err)
	}
	closed = true
	return nil
}

func (filesystem boundaryFilesystem) verifyFile(directory *boundary.StateDirectory, name string, expected []byte, maximum int64, point faultPoint) error {
	if err := filesystem.before(point); err != nil {
		return err
	}
	contents, err := filesystem.readFile(directory, name, maximum)
	if err != nil || !bytes.Equal(contents, expected) {
		return ErrStoreCorrupt
	}
	return nil
}

func (filesystem boundaryFilesystem) syncDirectory(directory *boundary.StateDirectory, point faultPoint) error {
	if err := filesystem.before(point); err != nil {
		return err
	}
	return directory.Sync()
}

func (filesystem boundaryFilesystem) renameNew(directory *boundary.StateDirectory, source, destination string, point faultPoint) error {
	if err := filesystem.before(point); err != nil {
		return err
	}
	return directory.RenameNew(source, destination)
}

func (filesystem boundaryFilesystem) replaceFile(directory *boundary.StateDirectory, source, destination string, point faultPoint) error {
	if err := filesystem.before(point); err != nil {
		return err
	}
	return directory.ReplaceFile(source, destination)
}

func (filesystem boundaryFilesystem) names(directory *boundary.StateDirectory, maximum int) ([]string, error) {
	return directory.Names(maximum)
}

func (filesystem boundaryFilesystem) removeFile(directory *boundary.StateDirectory, name string) error {
	return directory.RemoveFile(name)
}

func (filesystem boundaryFilesystem) removeEmptyDirectory(directory *boundary.StateDirectory, name string) error {
	return directory.RemoveEmptyDirectory(name)
}

func (filesystem boundaryFilesystem) before(point faultPoint) error {
	var fault error
	switch point {
	case faultNone:
		return nil
	case faultBeforePayloadSync:
		fault = filesystem.faults.BeforePayloadSync
	case faultBeforePayloadReopen:
		fault = filesystem.faults.BeforePayloadReopen
	case faultBeforeManifestSync:
		fault = filesystem.faults.BeforeManifestSync
	case faultBeforeManifestReopen:
		fault = filesystem.faults.BeforeManifestReopen
	case faultBeforeReadySync:
		fault = filesystem.faults.BeforeReadySync
	case faultBeforeGenerationSync:
		fault = filesystem.faults.BeforeGenerationSync
	case faultBeforeGenerationRename:
		fault = filesystem.faults.BeforeGenerationRename
	case faultBeforeGenerationsSync:
		fault = filesystem.faults.BeforeGenerationsSync
	case faultBeforeCurrentSync:
		fault = filesystem.faults.BeforeCurrentSync
	case faultBeforeCurrentReopen:
		fault = filesystem.faults.BeforeCurrentReopen
	case faultBeforeCurrentRename:
		fault = filesystem.faults.BeforeCurrentRename
	case faultBeforeStateSync:
		fault = filesystem.faults.BeforeStateSync
	default:
		return ErrStoreCorrupt
	}
	if fault == nil {
		return nil
	}
	return injectedFilesystemFault{cause: fault}
}

func randomEntryName(prefix string) (string, error) {
	var entropy [16]byte
	if _, err := io.ReadFull(rand.Reader, entropy[:]); err != nil {
		return "", fmt.Errorf("%w: random staging name", ErrStoreCorrupt)
	}
	return prefix + hex.EncodeToString(entropy[:]), nil
}

func cleanupStaging(filesystem storeFilesystem, generations *boundary.StateDirectory, name string) {
	staging, err := filesystem.openDirectory(generations, name)
	if errors.Is(err, boundary.ErrStateEntryNotFound) {
		return
	}
	if err != nil {
		return
	}
	for _, entry := range []string{indexFilename, manifestFilename, readyFilename} {
		_ = filesystem.removeFile(staging, entry)
	}
	_ = filesystem.closeDirectory(staging)
	_ = filesystem.removeEmptyDirectory(generations, name)
}

func cleanupFile(filesystem storeFilesystem, directory *boundary.StateDirectory, name string) {
	_ = filesystem.removeFile(directory, name)
}

func publishCurrent(filesystem storeFilesystem, state *boundary.StateDirectory, generationToken string, previous []byte) error {
	contents := []byte(generationToken + "\n")
	temporary, err := randomEntryName(".CURRENT-")
	if err != nil {
		return err
	}
	defer cleanupFile(filesystem, state, temporary)
	if err := filesystem.writeSyncedFile(state, temporary, contents, faultBeforeCurrentSync); err != nil {
		return err
	}
	if err := filesystem.verifyFile(state, temporary, contents, int64(len(contents)), faultBeforeCurrentReopen); err != nil {
		return err
	}
	if err := filesystem.replaceFile(state, temporary, currentFilename, faultBeforeCurrentRename); err != nil {
		if isInjectedFilesystemFault(err) {
			return err
		}
		return rollbackCurrentWithFilesystem(filesystem, state, previous, fmt.Errorf("%w: %v", ErrStoreCorrupt, err))
	}
	if err := filesystem.syncDirectory(state, faultBeforeStateSync); err != nil {
		original := error(err)
		if !isInjectedFilesystemFault(err) {
			original = fmt.Errorf("%w: %v", ErrStoreCorrupt, err)
		}
		return rollbackCurrentWithFilesystem(filesystem, state, previous, original)
	}
	return nil
}

func rollbackCurrentWithFilesystem(filesystem storeFilesystem, state *boundary.StateDirectory, previous []byte, original error) error {
	var rollbackErr error
	if previous == nil {
		rollbackErr = filesystem.removeFile(state, currentFilename)
		if rollbackErr == nil {
			rollbackErr = filesystem.syncDirectory(state, faultNone)
		}
	} else {
		var temporary string
		temporary, rollbackErr = randomEntryName(".CURRENT-rollback-")
		if rollbackErr == nil {
			defer cleanupFile(filesystem, state, temporary)
			rollbackErr = filesystem.writeSyncedFile(state, temporary, previous, faultNone)
		}
		if rollbackErr == nil {
			rollbackErr = filesystem.verifyFile(state, temporary, previous, int64(len(previous)), faultNone)
		}
		if rollbackErr == nil {
			rollbackErr = filesystem.replaceFile(state, temporary, currentFilename, faultNone)
		}
		if rollbackErr == nil {
			rollbackErr = filesystem.syncDirectory(state, faultNone)
		}
	}
	if rollbackErr != nil {
		return errors.Join(original, fmt.Errorf("pointer rollback failed: %w", rollbackErr))
	}
	return original
}
