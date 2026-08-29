// Package store publishes and validates deterministic immutable Level 1 generations.
package store

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

var (
	ErrNoCurrent           = errors.New("level1 current generation is absent")
	ErrStoreCorrupt        = errors.New("level1 generation store is corrupt")
	ErrIndexMismatch       = errors.New("level1 index identity mismatch")
	ErrGenerationCollision = errors.New("level1 generation identity collision")
	generationNamePattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

const (
	currentFilename      = "CURRENT"
	generationsDirectory = "generations"
	manifestFilename     = "manifest.json"
	indexFilename        = "index.bin"
	readyFilename        = "READY"
	zeroSHA256Identity   = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
)

type Snapshot struct {
	Manifest       model.Manifest
	Records        []model.Record
	Postings       map[string][]uint32
	IndexIdentity  string
	InstalledBytes int64
}

type Status struct {
	Ready              bool
	Manifest           model.Manifest
	IndexIdentity      string
	GenerationIdentity string
	InstalledBytes     int64
}

type generationArtifacts struct {
	payload        []byte
	manifest       []byte
	ready          []byte
	manifestValue  model.Manifest
	token          string
	installedBytes int64
}

type buildHooks struct{ materialized func() }

func (hooks buildHooks) decodeIndex(payload []byte) ([]model.Record, map[string][]uint32, error) {
	if hooks.materialized != nil {
		hooks.materialized()
	}
	return decodeIndex(payload)
}

// Build stages, verifies, installs, and atomically selects one immutable
// generation through the caller's retained state-root capability.
func Build(roots *boundary.Roots, manifest model.Manifest, records []model.Record) (Snapshot, error) {
	return BuildContext(context.Background(), roots, manifest, records)
}

// BuildContext observes cancellation before preparation, before state mutation,
// and immediately before CURRENT publication. Build remains the compatibility
// non-canceling entry point.
func BuildContext(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record) (Snapshot, error) {
	return buildWithFilesystemObservedContext(ctx, boundaryFilesystem{}, roots, manifest, records, buildHooks{})
}

// BuildWithFaults is the deterministic durability-test entry point.
func BuildWithFaults(roots *boundary.Roots, manifest model.Manifest, records []model.Record, faults Faults) (Snapshot, error) {
	return buildWithFilesystem(boundaryFilesystem{faults: faults}, roots, manifest, records)
}

func buildWithFilesystem(filesystem storeFilesystem, roots *boundary.Roots, manifest model.Manifest, records []model.Record) (Snapshot, error) {
	return buildWithFilesystemObserved(filesystem, roots, manifest, records, buildHooks{})
}

func buildWithFilesystemObserved(filesystem storeFilesystem, roots *boundary.Roots, manifest model.Manifest, records []model.Record, hooks buildHooks) (Snapshot, error) {
	return buildWithFilesystemObservedContext(context.Background(), filesystem, roots, manifest, records, hooks)
}

func buildWithFilesystemObservedContext(ctx context.Context, filesystem storeFilesystem, roots *boundary.Roots, manifest model.Manifest, records []model.Record, hooks buildHooks) (Snapshot, error) {
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	if roots == nil {
		return Snapshot{}, ErrStoreCorrupt
	}
	// Build and validate the complete deterministic byte image before the first
	// state mutation. Invalid caller input must not create even an empty store.
	artifacts, err := prepareGeneration(manifest, records)
	if err != nil {
		return Snapshot{}, err
	}
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.ensureState(roots); err != nil {
		return Snapshot{}, corrupt(err)
	}
	state, err := filesystem.openStateDirectory(roots, "")
	if err != nil {
		return Snapshot{}, corrupt(err)
	}
	defer filesystem.closeDirectory(state)
	previousToken, previousCurrent, previousExists, err := readCurrentPointer(filesystem, state)
	if err != nil {
		return Snapshot{}, err
	}
	var generations *boundary.StateDirectory
	if previousExists {
		generations, err = filesystem.openDirectory(state, generationsDirectory)
		if err != nil {
			return Snapshot{}, corrupt(err)
		}
	} else {
		generations, err = openOrCreateGenerations(filesystem, state)
		if err != nil {
			return Snapshot{}, err
		}
	}
	defer filesystem.closeDirectory(generations)
	if previousExists && previousToken == artifacts.token {
		if err := verifyGenerationArtifacts(filesystem, generations, artifacts); err != nil {
			return Snapshot{}, err
		}
		if err := ctx.Err(); err != nil {
			return Snapshot{}, err
		}
		selected, materializeErr := materializeArtifacts(artifacts, hooks)
		if materializeErr != nil {
			return Snapshot{}, materializeErr
		}
		if err := ctx.Err(); err != nil {
			return Snapshot{}, err
		}
		return selected, nil
	}
	if previousExists {
		if err := validateGenerationMetadata(filesystem, generations, previousToken); err != nil {
			return Snapshot{}, err
		}
	}

	stagingName, err := randomEntryName(".stage-")
	if err != nil {
		return Snapshot{}, err
	}
	defer cleanupStaging(filesystem, generations, stagingName)
	if err := filesystem.createDirectory(generations, stagingName); err != nil {
		return Snapshot{}, corrupt(err)
	}
	staging, err := filesystem.openDirectory(generations, stagingName)
	if err != nil {
		return Snapshot{}, corrupt(err)
	}
	stagingOpen := true
	defer func() {
		if stagingOpen {
			_ = filesystem.closeDirectory(staging)
		}
	}()

	if err := filesystem.writeSyncedFile(staging, indexFilename, artifacts.payload, faultBeforePayloadSync); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.verifyFile(staging, indexFilename, artifacts.payload, maximumEncodedIndexBytes, faultBeforePayloadReopen); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.writeSyncedFile(staging, manifestFilename, artifacts.manifest, faultBeforeManifestSync); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.verifyFile(staging, manifestFilename, artifacts.manifest, maximumManifestBytes, faultBeforeManifestReopen); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.writeSyncedFile(staging, readyFilename, artifacts.ready, faultBeforeReadySync); err != nil {
		return Snapshot{}, err
	}
	if err := filesystem.syncDirectory(staging, faultBeforeGenerationSync); err != nil {
		if isInjectedFilesystemFault(err) {
			return Snapshot{}, err
		}
		return Snapshot{}, corrupt(err)
	}
	if err := filesystem.closeDirectory(staging); err != nil {
		return Snapshot{}, corrupt(err)
	}
	stagingOpen = false
	if err := filesystem.renameNew(generations, stagingName, artifacts.token, faultBeforeGenerationRename); err != nil {
		if isInjectedFilesystemFault(err) {
			return Snapshot{}, err
		}
		if verifyErr := verifyGenerationArtifacts(filesystem, generations, artifacts); verifyErr != nil {
			return Snapshot{}, fmt.Errorf("%w: immutable bytes differ", ErrGenerationCollision)
		}
	}
	if err := filesystem.syncDirectory(generations, faultBeforeGenerationsSync); err != nil {
		if isInjectedFilesystemFault(err) {
			return Snapshot{}, err
		}
		return Snapshot{}, corrupt(err)
	}
	if err := verifyGenerationArtifacts(filesystem, generations, artifacts); err != nil {
		return Snapshot{}, corrupt(err)
	}
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	if err := publishCurrent(filesystem, state, artifacts.token, previousCurrent); err != nil {
		return Snapshot{}, err
	}
	selectedToken, currentBytes, exists, err := readCurrentPointer(filesystem, state)
	if err != nil || !exists || selectedToken != artifacts.token || !bytes.Equal(currentBytes, []byte(artifacts.token+"\n")) {
		original := corrupt(err)
		if rollbackErr := rollbackCurrentWithFilesystem(filesystem, state, previousCurrent, original); rollbackErr != nil {
			return Snapshot{}, rollbackErr
		}
		return Snapshot{}, original
	}
	if err := verifyGenerationArtifacts(filesystem, generations, artifacts); err != nil {
		original := corrupt(err)
		if rollbackErr := rollbackCurrentWithFilesystem(filesystem, state, previousCurrent, original); rollbackErr != nil {
			return Snapshot{}, rollbackErr
		}
		return Snapshot{}, original
	}
	selected, err := materializeArtifacts(artifacts, hooks)
	if err != nil {
		original := corrupt(err)
		if rollbackErr := rollbackCurrentWithFilesystem(filesystem, state, previousCurrent, original); rollbackErr != nil {
			return Snapshot{}, rollbackErr
		}
		return Snapshot{}, original
	}
	if err := ctx.Err(); err != nil {
		if rollbackErr := rollbackCurrentWithFilesystem(filesystem, state, previousCurrent, err); rollbackErr != nil {
			return Snapshot{}, rollbackErr
		}
		return Snapshot{}, err
	}
	return selected, nil
}

// Load validates CURRENT and its complete generation before exposing records.
func Load(roots *boundary.Roots, expectedIndex string) (Snapshot, error) {
	if roots == nil || !sha256Identity.MatchString(expectedIndex) {
		return Snapshot{}, ErrIndexMismatch
	}
	filesystem := boundaryFilesystem{}
	state, err := filesystem.openStateDirectory(roots, "")
	if errors.Is(err, boundary.ErrStateUnavailable) {
		return Snapshot{}, ErrNoCurrent
	}
	if err != nil {
		return Snapshot{}, corrupt(err)
	}
	defer filesystem.closeDirectory(state)
	generations, err := filesystem.openDirectory(state, generationsDirectory)
	if errors.Is(err, boundary.ErrStateEntryNotFound) {
		return Snapshot{}, ErrStoreCorrupt
	}
	if err != nil {
		return Snapshot{}, corrupt(err)
	}
	defer filesystem.closeDirectory(generations)
	snapshot, _, exists, err := loadCurrentOptional(filesystem, state, generations)
	if err != nil {
		return Snapshot{}, err
	}
	if !exists {
		return Snapshot{}, ErrNoCurrent
	}
	if snapshot.IndexIdentity != expectedIndex {
		return Snapshot{}, ErrIndexMismatch
	}
	return snapshot, nil
}

// Inspect validates the same complete state as Load without an expected index.
func Inspect(roots *boundary.Roots) (Status, error) {
	if roots == nil {
		return Status{}, ErrStoreCorrupt
	}
	filesystem := boundaryFilesystem{}
	state, err := filesystem.openStateDirectory(roots, "")
	if errors.Is(err, boundary.ErrStateUnavailable) {
		return Status{}, ErrNoCurrent
	}
	if err != nil {
		return Status{}, corrupt(err)
	}
	defer filesystem.closeDirectory(state)
	generations, err := filesystem.openDirectory(state, generationsDirectory)
	if errors.Is(err, boundary.ErrStateEntryNotFound) {
		return Status{}, ErrStoreCorrupt
	}
	if err != nil {
		return Status{}, corrupt(err)
	}
	defer filesystem.closeDirectory(generations)
	snapshot, _, exists, err := loadCurrentOptional(filesystem, state, generations)
	if err != nil {
		return Status{}, err
	}
	if !exists {
		return Status{}, ErrNoCurrent
	}
	return Status{
		Ready: true, Manifest: snapshot.Manifest, IndexIdentity: snapshot.IndexIdentity,
		GenerationIdentity: snapshot.Manifest.GenerationIdentity, InstalledBytes: snapshot.InstalledBytes,
	}, nil
}

func openOrCreateGenerations(filesystem storeFilesystem, state *boundary.StateDirectory) (*boundary.StateDirectory, error) {
	directory, err := filesystem.openDirectory(state, generationsDirectory)
	if err == nil {
		return directory, nil
	}
	if !errors.Is(err, boundary.ErrStateEntryNotFound) {
		return nil, corrupt(err)
	}
	if err := filesystem.createDirectory(state, generationsDirectory); err != nil {
		return nil, corrupt(err)
	}
	directory, err = filesystem.openDirectory(state, generationsDirectory)
	if err != nil {
		return nil, corrupt(err)
	}
	return directory, nil
}

func prepareGeneration(input model.Manifest, inputRecords []model.Record) (generationArtifacts, error) {
	if input.FormatVersion != "1" {
		return generationArtifacts{}, ErrInvalidManifest
	}
	postingCount := 0
	payload, err := encodeIndexObservedStats(inputRecords, nil, &postingCount)
	if err != nil {
		return generationArtifacts{}, err
	}
	manifest := cloneManifest(input)
	manifest.FormatVersion = "1"
	manifest.RecordCount = len(inputRecords)
	manifest.PostingCount = postingCount
	manifest.PayloadDigest = sha256ID(payload)
	manifest.IndexIdentity = manifest.PayloadDigest
	manifest.GenerationIdentity = zeroSHA256Identity
	identity, err := computeGenerationIdentity(manifest)
	if err != nil {
		return generationArtifacts{}, err
	}
	manifest.GenerationIdentity = identity
	manifestBytes, err := encodeManifest(manifest)
	if err != nil {
		return generationArtifacts{}, err
	}
	ready := []byte(identity + "\n")
	token := tokenFromIdentity(identity)
	if !generationNamePattern.MatchString(token) {
		return generationArtifacts{}, ErrInvalidManifest
	}
	installedBytes := int64(len(payload)) + int64(len(manifestBytes)) + int64(len(ready))
	return generationArtifacts{
		payload: payload, manifest: manifestBytes, ready: ready, token: token,
		manifestValue: manifest, installedBytes: installedBytes,
	}, nil
}

func materializeArtifacts(artifacts generationArtifacts, hooks buildHooks) (Snapshot, error) {
	records, postings, err := hooks.decodeIndex(artifacts.payload)
	if err != nil || len(records) != artifacts.manifestValue.RecordCount || len(postings) != artifacts.manifestValue.PostingCount {
		return Snapshot{}, ErrStoreCorrupt
	}
	return Snapshot{
		Manifest: artifacts.manifestValue, Records: records, Postings: postings,
		IndexIdentity: artifacts.manifestValue.IndexIdentity, InstalledBytes: artifacts.installedBytes,
	}, nil
}

func verifyGenerationArtifacts(filesystem storeFilesystem, generations *boundary.StateDirectory, artifacts generationArtifacts) error {
	if !generationNamePattern.MatchString(artifacts.token) {
		return ErrStoreCorrupt
	}
	directory, err := filesystem.openDirectory(generations, artifacts.token)
	if err != nil {
		return fmt.Errorf("open generation: %w", corrupt(err))
	}
	defer filesystem.closeDirectory(directory)
	wantNames := []string{readyFilename, indexFilename, manifestFilename}
	names, err := filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return ErrStoreCorrupt
	}
	for _, file := range []struct {
		name    string
		value   []byte
		maximum int64
	}{
		{name: indexFilename, value: artifacts.payload, maximum: maximumEncodedIndexBytes},
		{name: manifestFilename, value: artifacts.manifest, maximum: maximumManifestBytes},
		{name: readyFilename, value: artifacts.ready, maximum: 72},
	} {
		if err := filesystem.verifyFile(directory, file.name, file.value, file.maximum, faultNone); err != nil {
			return ErrStoreCorrupt
		}
	}
	names, err = filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return ErrStoreCorrupt
	}
	return nil
}

func validateGenerationMetadata(filesystem storeFilesystem, generations *boundary.StateDirectory, token string) error {
	if !generationNamePattern.MatchString(token) {
		return ErrStoreCorrupt
	}
	directory, err := filesystem.openDirectory(generations, token)
	if err != nil {
		return fmt.Errorf("open generation: %w", corrupt(err))
	}
	defer filesystem.closeDirectory(directory)
	wantNames := []string{readyFilename, indexFilename, manifestFilename}
	names, err := filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return ErrStoreCorrupt
	}
	manifestBytes, err := filesystem.readFile(directory, manifestFilename, maximumManifestBytes)
	if err != nil {
		return ErrStoreCorrupt
	}
	manifest, err := decodeManifest(manifestBytes)
	if err != nil || tokenFromIdentity(manifest.GenerationIdentity) != token {
		return ErrStoreCorrupt
	}
	ready, err := filesystem.readFile(directory, readyFilename, 72)
	if err != nil || !bytes.Equal(ready, []byte(manifest.GenerationIdentity+"\n")) {
		return ErrStoreCorrupt
	}
	payload, err := filesystem.readFile(directory, indexFilename, maximumEncodedIndexBytes)
	if err != nil || sha256ID(payload) != manifest.PayloadDigest || manifest.IndexIdentity != manifest.PayloadDigest {
		return ErrStoreCorrupt
	}
	recordCount, postingCount, err := validateIndex(payload)
	if err != nil || recordCount != manifest.RecordCount || postingCount != manifest.PostingCount {
		return ErrStoreCorrupt
	}
	identity, err := computeGenerationIdentity(manifest)
	if err != nil || identity != manifest.GenerationIdentity {
		return ErrStoreCorrupt
	}
	names, err = filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return ErrStoreCorrupt
	}
	return nil
}

func computeGenerationIdentity(manifest model.Manifest) (string, error) {
	identityManifest := cloneManifest(manifest)
	identityManifest.GenerationIdentity = zeroSHA256Identity
	encoded, err := encodeManifest(identityManifest)
	if err != nil {
		return "", err
	}
	material := make([]byte, 0, len(encoded)+18)
	material = append(material, []byte("taf-generation-v1\x00")...)
	material = append(material, encoded...)
	return sha256ID(material), nil
}

func loadCurrentOptional(filesystem storeFilesystem, state, generations *boundary.StateDirectory) (Snapshot, []byte, bool, error) {
	token, current, exists, err := readCurrentPointer(filesystem, state)
	if err != nil || !exists {
		return Snapshot{}, current, exists, err
	}
	snapshot, err := loadGeneration(filesystem, generations, token)
	if err != nil {
		return Snapshot{}, nil, false, fmt.Errorf("validate selected generation: %w", err)
	}
	return snapshot, slices.Clone(current), true, nil
}

func readCurrentPointer(filesystem storeFilesystem, state *boundary.StateDirectory) (string, []byte, bool, error) {
	current, err := filesystem.readAtomicCurrent(state, 65)
	if errors.Is(err, boundary.ErrStateEntryNotFound) {
		return "", nil, false, nil
	}
	if err != nil {
		return "", nil, false, fmt.Errorf("read current pointer: %w", corrupt(err))
	}
	if len(current) != 65 || current[64] != '\n' {
		return "", nil, false, ErrStoreCorrupt
	}
	token := string(current[:64])
	if !generationNamePattern.MatchString(token) {
		return "", nil, false, ErrStoreCorrupt
	}
	return token, slices.Clone(current), true, nil
}

func loadGeneration(filesystem storeFilesystem, generations *boundary.StateDirectory, token string) (Snapshot, error) {
	if !generationNamePattern.MatchString(token) {
		return Snapshot{}, ErrStoreCorrupt
	}
	directory, err := filesystem.openDirectory(generations, token)
	if err != nil {
		return Snapshot{}, fmt.Errorf("open generation: %w", corrupt(err))
	}
	defer filesystem.closeDirectory(directory)
	wantNames := []string{readyFilename, indexFilename, manifestFilename}
	names, err := filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return Snapshot{}, fmt.Errorf("list generation: %w", corrupt(err))
	}
	manifestBytes, err := filesystem.readFile(directory, manifestFilename, maximumManifestBytes)
	if err != nil {
		return Snapshot{}, fmt.Errorf("read manifest: %w", corrupt(err))
	}
	manifest, err := decodeManifest(manifestBytes)
	if err != nil || tokenFromIdentity(manifest.GenerationIdentity) != token {
		return Snapshot{}, ErrStoreCorrupt
	}
	ready, err := filesystem.readFile(directory, readyFilename, 72)
	if err != nil || !bytes.Equal(ready, []byte(manifest.GenerationIdentity+"\n")) {
		return Snapshot{}, fmt.Errorf("read ready marker: %w", corrupt(err))
	}
	payload, err := filesystem.readFile(directory, indexFilename, maximumEncodedIndexBytes)
	if err != nil || sha256ID(payload) != manifest.PayloadDigest || manifest.IndexIdentity != manifest.PayloadDigest {
		return Snapshot{}, fmt.Errorf("read index payload: %w", corrupt(err))
	}
	records, postings, err := decodeIndex(payload)
	if err != nil || len(records) != manifest.RecordCount || len(postings) != manifest.PostingCount {
		return Snapshot{}, ErrStoreCorrupt
	}
	identity, err := computeGenerationIdentity(manifest)
	if err != nil || identity != manifest.GenerationIdentity {
		return Snapshot{}, ErrStoreCorrupt
	}
	names, err = filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return Snapshot{}, ErrStoreCorrupt
	}
	installedBytes := int64(len(payload)) + int64(len(manifestBytes)) + int64(len(ready))
	return Snapshot{Manifest: manifest, Records: records, Postings: postings, IndexIdentity: manifest.IndexIdentity, InstalledBytes: installedBytes}, nil
}

func cloneManifest(manifest model.Manifest) model.Manifest {
	manifest.ParserIdentities = cloneStringMap(manifest.ParserIdentities)
	manifest.Coverage.ExclusionReasonCounts = cloneIntMap(manifest.Coverage.ExclusionReasonCounts)
	return manifest
}

func tokenFromIdentity(identity string) string { return strings.TrimPrefix(identity, "sha256:") }

func corrupt(err error) error {
	if err == nil {
		return ErrStoreCorrupt
	}
	if errors.Is(err, ErrStoreCorrupt) {
		return err
	}
	return fmt.Errorf("%w: %v", ErrStoreCorrupt, err)
}
