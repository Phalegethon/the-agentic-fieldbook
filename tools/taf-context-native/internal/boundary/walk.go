package boundary

import (
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
)

// RepositoryEntry is metadata observed through the retained repository
// capability. RelativePath always uses slash separators.
type RepositoryEntry struct {
	RelativePath string
	Mode         os.FileMode
	Size         int64
	GitMetadata  bool
}

// StablePrefix is a post-validated bounded read of a regular repository file.
// Size is the complete file size, while Bytes has at most the requested limit.
type StablePrefix struct {
	RelativePath string
	Bytes        []byte
	Size         int64
}

// IOObservation reports repository I/O performed through this retained root.
// Copies of Roots share the same monotonic observation state.
type IOObservation struct {
	ReadDirectoryEntries         int
	MaterializedSnapshotBytes    uint64
	PeakSnapshotReservationBytes uint64
	ReadPrefixBytes              uint64
	FullBodyOpens                int
	FullBodyBytes                uint64
}

func (r *Roots) observeSnapshotReservation(amount uint64) {
	if r.ioObservation == nil {
		return
	}
	r.ioObservation.mu.Lock()
	if amount > r.ioObservation.value.PeakSnapshotReservationBytes {
		r.ioObservation.value.PeakSnapshotReservationBytes = amount
	}
	r.ioObservation.mu.Unlock()
}

type ioObservationState struct {
	mu    sync.Mutex
	value IOObservation
}

func (r *Roots) observeIO(delta IOObservation) {
	if r.ioObservation == nil {
		return
	}
	r.ioObservation.mu.Lock()
	r.ioObservation.value.ReadDirectoryEntries += delta.ReadDirectoryEntries
	r.ioObservation.value.MaterializedSnapshotBytes += delta.MaterializedSnapshotBytes
	r.ioObservation.value.ReadPrefixBytes += delta.ReadPrefixBytes
	r.ioObservation.value.FullBodyOpens += delta.FullBodyOpens
	r.ioObservation.value.FullBodyBytes += delta.FullBodyBytes
	r.ioObservation.mu.Unlock()
	if repositoryIOHook != nil {
		repositoryIOHook(delta)
	}
}

// IOObservation returns an atomic snapshot of repository I/O observations.
func (r *Roots) IOObservation() IOObservation {
	if r.ioObservation == nil {
		return IOObservation{}
	}
	r.ioObservation.mu.Lock()
	defer r.ioObservation.mu.Unlock()
	return r.ioObservation.value
}

// directoryReadHook provides a deterministic mutation seam for boundary
// tests. Production leaves it nil.
var directoryReadHook func()
var repositoryIOHook func(IOObservation)

const maximumDirectoryBatch = 256

const (
	maximumRepositoryDepth               = 256
	maximumRepositoryPathBytes           = 4096
	maximumRepositoryEmittedPathBytes    = 64 << 20
	maximumRepositorySnapshotBytes       = 64 << 20
	repositorySnapshotEntryOverheadBytes = 128
	// Reopening components plus rereading entries may consume at most twice
	// the caller's discovery observation ceiling after callbacks finish.
	repositoryVerificationWorkFactor = 2
)

// PolicyDescriptor exposes only immutable walk ceilings/tokens for consumers
// binding inventory policy; it never exposes root capabilities.
func PolicyDescriptor() string {
	return fmt.Sprintf("%s walk-v3 depth=%d batch=%d snapshot=%d path=%d emitted=%d overhead=%d verify=%d", RootPolicyDescriptor(), maximumRepositoryDepth, maximumDirectoryBatch, maximumRepositorySnapshotBytes, maximumRepositoryPathBytes, maximumRepositoryEmittedPathBytes, repositorySnapshotEntryOverheadBytes, repositoryVerificationWorkFactor)
}

type repositoryObservationBudget struct {
	remaining int
}

type repositorySnapshotByteBudget struct {
	maximum   uint64
	remaining uint64
	roots     *Roots
}

func (budget *repositorySnapshotByteBudget) consume(amount uint64) bool {
	reserved, ok := budget.reserve(amount)
	if !ok {
		return false
	}
	return budget.commit(reserved, amount)
}

func (budget *repositorySnapshotByteBudget) reserve(amount uint64) (uint64, bool) {
	if budget == nil || amount > budget.remaining {
		return 0, false
	}
	budget.remaining -= amount
	budget.roots.observeSnapshotReservation(budget.maximum - budget.remaining)
	return amount, true
}

func (budget *repositorySnapshotByteBudget) commit(reserved, materialized uint64) bool {
	if budget == nil || materialized > reserved {
		return false
	}
	budget.remaining += reserved - materialized
	budget.roots.observeIO(IOObservation{MaterializedSnapshotBytes: materialized})
	return true
}

type repositorySnapshotEntry struct {
	name string
	info os.FileInfo
}

type repositoryDirectorySnapshot struct {
	relative string
	identity os.FileInfo
	entries  []repositorySnapshotEntry
}

type repositoryWalkState struct {
	discovery        *repositoryObservationBudget
	snapshotBytes    *repositorySnapshotByteBudget
	path             []byte
	depth            int
	emittedPathBytes uint64
	snapshots        []repositoryDirectorySnapshot
}

// WalkRepository enumerates repository metadata through the captured root
// capability. It never follows symlinks and never descends into Git metadata.
func (r *Roots) WalkRepository(maximumObservations int, visit func(RepositoryEntry) error) error {
	return r.walkRepository(maximumObservations, maximumRepositorySnapshotBytes, visit)
}

func (r *Roots) walkRepository(maximumObservations int, maximumSnapshotBytes uint64, visit func(RepositoryEntry) error) error {
	if r.repositoryRoot == nil || visit == nil {
		return ErrUnsafePath
	}
	if maximumObservations <= 0 {
		return ErrRepositoryEnumerationLimit
	}
	if maximumSnapshotBytes == 0 {
		return ErrRepositoryTraversalLimit
	}
	state := &repositoryWalkState{
		discovery:     &repositoryObservationBudget{remaining: maximumObservations},
		snapshotBytes: &repositorySnapshotByteBudget{maximum: maximumSnapshotBytes, remaining: maximumSnapshotBytes, roots: r},
	}
	if err := r.walkRepositoryDirectory(r.repositoryRoot, state, visit); err != nil {
		return err
	}
	verificationMaximum, ok := boundedMultiply(maximumObservations, repositoryVerificationWorkFactor)
	if !ok {
		return ErrRepositoryEnumerationLimit
	}
	return r.validateRepositorySnapshots(
		state.snapshots,
		&repositoryObservationBudget{remaining: verificationMaximum},
		state.snapshotBytes,
	)
}

func (r *Roots) walkRepositoryDirectory(current *os.Root, state *repositoryWalkState, visit func(RepositoryEntry) error) error {
	if r.entersGitDirectory([]*os.Root{current}) {
		return ErrUnsafePath
	}
	entries, identity, err := r.readRootDirectory(current, state.discovery, state.snapshotBytes, len(state.path), true)
	if err != nil {
		return err
	}
	sort.Slice(entries, func(i, j int) bool { return repositoryEntryLess(entries[i], entries[j]) })
	state.retainSnapshot(identity, entries)
	for _, entry := range entries {
		name := entry.name
		if _, err := safeComponents(name, false); err != nil {
			return ErrUnsafePath
		}
		info, err := current.Lstat(name)
		if err != nil || !sameSnapshot(entry.info, info) {
			return fmt.Errorf("%w: %v", ErrUnsafePath, err)
		}
		mark := len(state.path)
		if !state.pushPath(name) {
			return ErrRepositoryTraversalLimit
		}
		relative := string(state.path)
		isGitMetadata := r.isGitMetadata(info)
		if err := visit(RepositoryEntry{RelativePath: relative, Mode: info.Mode(), Size: info.Size(), GitMetadata: isGitMetadata}); err != nil {
			if errors.Is(err, ErrSkipRepositoryDirectory) && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
				state.path = state.path[:mark]
				continue
			}
			state.path = state.path[:mark]
			return err
		}
		// A .git component is repository metadata even for nested repositories.
		if isGitMetadata || strings.EqualFold(name, ".git") || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			state.path = state.path[:mark]
			continue
		}
		if descendBeforeOpenHook != nil {
			descendBeforeOpenHook(name)
		}
		next, err := current.OpenRoot(name)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrUnsafePath, err)
		}
		opened, statErr := next.Stat(".")
		latest, latestErr := current.Lstat(name)
		if statErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !latest.IsDir() || !sameIdentity(info, opened) || !sameIdentity(info, latest) || !sameIdentity(opened, latest) {
			_ = next.Close()
			return ErrUnsafePath
		}
		if r.entersGitDirectory([]*os.Root{next}) {
			_ = next.Close()
			state.path = state.path[:mark]
			continue
		}
		state.depth++
		err = r.walkRepositoryDirectory(next, state, visit)
		state.depth--
		closeErr := next.Close()
		state.path = state.path[:mark]
		if err != nil {
			return err
		}
		if closeErr != nil {
			return fmt.Errorf("%w: %v", ErrUnsafePath, closeErr)
		}
	}
	return nil
}

func (state *repositoryWalkState) pushPath(name string) bool {
	depth := state.depth + 1
	if depth > maximumRepositoryDepth {
		return false
	}
	additional := len(name)
	if len(state.path) != 0 {
		additional++
	}
	newLength := len(state.path) + additional
	if additional > maximumRepositoryPathBytes-len(state.path) || uint64(newLength) > maximumRepositoryEmittedPathBytes-state.emittedPathBytes {
		return false
	}
	if len(state.path) != 0 {
		state.path = append(state.path, '/')
	}
	state.path = append(state.path, name...)
	state.emittedPathBytes += uint64(len(state.path))
	return true
}

func (state *repositoryWalkState) retainSnapshot(identity os.FileInfo, entries []repositorySnapshotEntry) {
	state.snapshots = append(state.snapshots, repositoryDirectorySnapshot{
		relative: string(state.path),
		identity: identity,
		entries:  entries,
	})
}

func boundedMultiply(value, factor int) (int, bool) {
	if value <= 0 || factor <= 0 || value > int(^uint(0)>>1)/factor {
		return 0, false
	}
	return value * factor, true
}

func (r *Roots) validateRepositorySnapshots(snapshots []repositoryDirectorySnapshot, budget *repositoryObservationBudget, snapshotBytes *repositorySnapshotByteBudget) error {
	for _, snapshot := range snapshots {
		current := r.repositoryRoot
		var closers []*os.Root
		if snapshot.relative != "" {
			components, err := safeComponents(snapshot.relative, true)
			if err != nil || len(components) > budget.remaining {
				return ErrRepositoryEnumerationLimit
			}
			budget.remaining -= len(components)
			current, closers, err = descend(r.repositoryRoot, components)
			if err != nil {
				return ErrRepositoryChanged
			}
		}
		lineage := closers
		if len(lineage) == 0 {
			lineage = []*os.Root{current}
		}
		if r.entersGitDirectory(lineage) {
			closeRoots(closers)
			return ErrRepositoryChanged
		}
		entries, identity, readErr := r.readRootDirectory(current, budget, snapshotBytes, len(snapshot.relative), false)
		closeRoots(closers)
		if readErr != nil || !sameSnapshot(snapshot.identity, identity) {
			if errors.Is(readErr, ErrRepositoryEnumerationLimit) || errors.Is(readErr, ErrRepositoryTraversalLimit) {
				return readErr
			}
			return ErrRepositoryChanged
		}
		sort.Slice(entries, func(i, j int) bool { return repositoryEntryLess(entries[i], entries[j]) })
		if !sameRepositorySnapshot(snapshot.entries, entries) {
			return ErrRepositoryChanged
		}
	}
	return nil
}

func repositoryEntryLess(left, right repositorySnapshotEntry) bool {
	leftIgnore, rightIgnore := left.name == ".gitignore", right.name == ".gitignore"
	if leftIgnore != rightIgnore {
		return leftIgnore
	}
	return left.name < right.name
}

func (r *Roots) isGitMetadata(info os.FileInfo) bool {
	return (r.gitDirectoryInfo != nil && sameIdentity(info, r.gitDirectoryInfo)) ||
		(r.gitCommonInfo != nil && sameIdentity(info, r.gitCommonInfo)) ||
		(r.gitMetadataInfo != nil && sameIdentity(info, r.gitMetadataInfo))
}

func (r *Roots) readRootDirectory(root *os.Root, budget *repositoryObservationBudget, snapshotBytes *repositorySnapshotByteBudget, relativeBytes int, invokeHook bool) ([]repositorySnapshotEntry, os.FileInfo, error) {
	directoryBytes := uint64(relativeBytes + repositorySnapshotEntryOverheadBytes)
	if !snapshotBytes.consume(directoryBytes) {
		return nil, nil, ErrRepositoryTraversalLimit
	}
	before, err := root.Stat(".")
	if err != nil || !before.IsDir() {
		return nil, nil, ErrUnsafePath
	}
	directory, err := root.Open(".")
	if err != nil {
		return nil, nil, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	defer directory.Close()
	opened, err := directory.Stat()
	after, afterErr := root.Stat(".")
	if err != nil || afterErr != nil || !opened.IsDir() || !sameIdentity(before, opened) || !sameIdentity(before, after) || !sameIdentity(opened, after) {
		return nil, nil, ErrUnsafePath
	}
	var entries []repositorySnapshotEntry
	for {
		if budget.remaining == 0 {
			return nil, nil, ErrRepositoryEnumerationLimit
		}
		batchMaximum := maximumDirectoryBatch
		if budget.remaining < batchMaximum {
			batchMaximum = budget.remaining
		}
		maximumEntryBytes := uint64(maximumRepositoryPathBytes + repositorySnapshotEntryOverheadBytes)
		snapshotBatchMaximum := int(snapshotBytes.remaining / maximumEntryBytes)
		if snapshotBatchMaximum == 0 {
			return nil, nil, ErrRepositoryTraversalLimit
		}
		if snapshotBatchMaximum < batchMaximum {
			batchMaximum = snapshotBatchMaximum
		}
		reservedBytes := uint64(batchMaximum) * maximumEntryBytes
		reservation, ok := snapshotBytes.reserve(reservedBytes)
		if !ok {
			return nil, nil, ErrRepositoryTraversalLimit
		}
		batch, readErr := directory.ReadDir(batchMaximum)
		budget.remaining -= len(batch)
		r.observeIO(IOObservation{ReadDirectoryEntries: len(batch)})
		materializedBytes := uint64(0)
		for _, entry := range batch {
			if len(entry.Name()) > maximumRepositoryPathBytes {
				_ = snapshotBytes.commit(reservation, reservation)
				return nil, nil, ErrRepositoryTraversalLimit
			}
			entryBytes := uint64(len(entry.Name()) + repositorySnapshotEntryOverheadBytes)
			if entryBytes > reservation-materializedBytes {
				_ = snapshotBytes.commit(reservation, reservation)
				return nil, nil, ErrRepositoryTraversalLimit
			}
			materializedBytes += entryBytes
		}
		if !snapshotBytes.commit(reservation, materializedBytes) {
			return nil, nil, ErrRepositoryTraversalLimit
		}
		for _, entry := range batch {
			info, statErr := root.Lstat(entry.Name())
			if statErr != nil {
				return nil, nil, fmt.Errorf("%w: %v", ErrUnsafePath, statErr)
			}
			entries = append(entries, repositorySnapshotEntry{name: entry.Name(), info: info})
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return nil, nil, fmt.Errorf("%w: %v", ErrUnsafePath, readErr)
		}
		if len(batch) == 0 {
			return nil, nil, ErrUnsafePath
		}
	}
	if invokeHook && directoryReadHook != nil {
		directoryReadHook()
	}
	afterRead, afterReadErr := root.Stat(".")
	if afterReadErr != nil || !sameSnapshot(before, afterRead) || !sameSnapshot(opened, afterRead) {
		return nil, nil, ErrUnsafePath
	}
	return entries, afterRead, nil
}

func sameRepositorySnapshot(first, second []repositorySnapshotEntry) bool {
	if len(first) != len(second) {
		return false
	}
	for index := range first {
		if first[index].name != second[index].name || !sameSnapshot(first[index].info, second[index].info) {
			return false
		}
	}
	return true
}

// ReadRepositoryPrefix reads no more than maximum bytes from a regular file
// through the retained repository capability. The file is identity-checked
// before opening, after opening, and after the read.
func (r *Roots) ReadRepositoryPrefix(relative string, maximum int64) (StablePrefix, error) {
	components, err := safeComponents(relative, true)
	if err != nil || r.repositoryRoot == nil || maximum < 0 || protectedRepositoryPath(r, relative) {
		return StablePrefix{}, ErrUnsafePath
	}
	current, closers, err := descend(r.repositoryRoot, components[:len(components)-1])
	if err != nil {
		return StablePrefix{}, err
	}
	defer closeRoots(closers)
	if r.entersGitDirectory(append(closers, current)) {
		return StablePrefix{}, ErrUnsafePath
	}
	name := components[len(components)-1]
	before, err := current.Lstat(name)
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() || (r.gitMetadataInfo != nil && sameIdentity(before, r.gitMetadataInfo)) {
		return StablePrefix{}, ErrUnsafePath
	}
	if repositoryOpenHook != nil {
		repositoryOpenHook()
	}
	file, err := current.Open(name)
	if err != nil {
		return StablePrefix{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameSnapshot(before, opened) {
		return StablePrefix{}, ErrUnstableFile
	}
	contents, err := io.ReadAll(io.LimitReader(file, maximum))
	r.observeIO(IOObservation{ReadPrefixBytes: uint64(len(contents))})
	if err != nil {
		return StablePrefix{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	after, err := file.Stat()
	pathAfter, pathErr := current.Lstat(name)
	if err != nil || pathErr != nil || pathAfter.Mode()&os.ModeSymlink != 0 || !pathAfter.Mode().IsRegular() || !sameSnapshot(before, after) || !sameSnapshot(before, pathAfter) {
		return StablePrefix{}, ErrUnstableFile
	}
	return StablePrefix{RelativePath: relative, Bytes: contents, Size: before.Size()}, nil
}
