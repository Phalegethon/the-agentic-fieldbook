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
	Query          QueryIndex
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

type buildPhase string

const (
	buildPhasePreflight        buildPhase = "preflight"
	buildPhaseQueryKeys        buildPhase = "query-keys"
	buildPhaseSort             buildPhase = "sort"
	buildPhaseEncode           buildPhase = "encode"
	buildPhaseValidation       buildPhase = "validation"
	buildPhaseRangeCount       buildPhase = "range-count"
	buildPhaseRangeEncode      buildPhase = "range-encode"
	buildPhaseCompression      buildPhase = "compression"
	buildPhasePayloadDigest    buildPhase = "payload-digest"
	buildPhaseManifest         buildPhase = "manifest"
	buildPhaseGenerationDigest buildPhase = "generation-digest"
)

type buildHooks struct {
	materialized func()
	building     func(buildPhase)
}

func observeBuildContext(ctx context.Context, observed func(buildPhase), phase buildPhase) error {
	if observed != nil {
		observed(phase)
	}
	return ctx.Err()
}

func (hooks buildHooks) decodeIndex(ctx context.Context, payload []byte) ([]model.Record, map[string][]uint32, QueryIndex, error) {
	if hooks.materialized != nil {
		hooks.materialized()
	}
	return decodeIndexContext(ctx, payload)
}

// Build stages, verifies, installs, and atomically selects one immutable
// generation through the caller's retained state-root capability.
func Build(roots *boundary.Roots, manifest model.Manifest, records []model.Record) (Snapshot, error) {
	return BuildContext(context.Background(), roots, manifest, records)
}

// BuildContext observes cancellation throughout deterministic preparation,
// before state mutation, and immediately before CURRENT publication. Build
// remains the compatibility non-canceling entry point.
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
	artifacts, err := prepareGenerationContext(ctx, manifest, records, hooks.building)
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
		selected, materializeErr := materializeArtifacts(ctx, artifacts, hooks)
		if materializeErr != nil {
			return Snapshot{}, materializeErr
		}
		if err := ctx.Err(); err != nil {
			return Snapshot{}, err
		}
		return selected, nil
	}
	if previousExists {
		if err := validateGenerationMetadataContext(ctx, filesystem, generations, previousToken); err != nil {
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
	selected, err := materializeArtifacts(ctx, artifacts, hooks)
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
	return LoadContext(context.Background(), roots, expectedIndex)
}

// LoadContext keeps read-side callers from exposing a snapshot after their
// operation has been cancelled. The underlying capability reads are bounded;
// the post-load check closes the same-current cancellation window.
func LoadContext(ctx context.Context, roots *boundary.Roots, expectedIndex string) (Snapshot, error) {
	return loadContextObserved(ctx, roots, expectedIndex, nil)
}

func loadContextObserved(ctx context.Context, roots *boundary.Roots, expectedIndex string, observed func()) (Snapshot, error) {
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	if roots == nil || !validSHA256IdentityString(expectedIndex) {
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
	snapshot, selectedCurrent, exists, err := loadCurrentOptionalContextObserved(ctx, filesystem, state, generations, observed)
	if err != nil {
		return Snapshot{}, err
	}
	if !exists {
		return Snapshot{}, ErrNoCurrent
	}
	if snapshot.IndexIdentity != expectedIndex {
		return Snapshot{}, ErrIndexMismatch
	}
	_, current, stillExists, err := readCurrentPointer(filesystem, state)
	if err != nil {
		return Snapshot{}, err
	}
	if !stillExists || !bytes.Equal(current, selectedCurrent) {
		return Snapshot{}, ErrIndexMismatch
	}
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	return snapshot, nil
}

// Inspect validates the same complete state as Load without materializing a
// snapshot. It remains the compatibility non-canceling entry point.
func Inspect(roots *boundary.Roots) (Status, error) {
	return InspectContext(context.Background(), roots)
}

func InspectContext(ctx context.Context, roots *boundary.Roots) (Status, error) {
	if err := ctx.Err(); err != nil {
		return Status{}, err
	}
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
	token, _, exists, err := readCurrentPointer(filesystem, state)
	if err != nil {
		return Status{}, err
	}
	if !exists {
		return Status{}, ErrNoCurrent
	}
	return inspectGenerationContext(ctx, filesystem, generations, token)
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
	return prepareGenerationContext(context.Background(), input, inputRecords, nil)
}

func prepareGenerationContext(ctx context.Context, input model.Manifest, inputRecords []model.Record, observed func(buildPhase)) (generationArtifacts, error) {
	if err := ctx.Err(); err != nil {
		return generationArtifacts{}, err
	}
	// Bound all caller-controlled manifest collections and strings before
	// cloneManifest can traverse or allocate for them. Once bounded, manifest
	// cloning and JSON map ordering are fixed-size operations.
	if input.FormatVersion != "2" || !manifestVariableBounds(input) {
		return generationArtifacts{}, ErrInvalidManifest
	}
	postingCount := 0
	payload, err := encodeIndexObservedStatsContext(ctx, inputRecords, nil, &postingCount, observed)
	if err != nil {
		return generationArtifacts{}, err
	}
	if err := ctx.Err(); err != nil {
		return generationArtifacts{}, err
	}
	manifest := cloneManifest(input)
	manifest.FormatVersion = "2"
	manifest.RecordCount = len(inputRecords)
	manifest.PostingCount = postingCount
	payloadDigest, err := sha256IDContext(ctx, payload, observed, buildPhasePayloadDigest)
	if err != nil {
		return generationArtifacts{}, err
	}
	manifest.PayloadDigest = payloadDigest
	manifest.IndexIdentity = manifest.PayloadDigest
	manifest.GenerationIdentity = zeroSHA256Identity
	identity, err := computeGenerationIdentityContext(ctx, manifest, observed)
	if err != nil {
		return generationArtifacts{}, err
	}
	manifest.GenerationIdentity = identity
	manifestBytes, err := encodeManifestContext(ctx, manifest, observed)
	if err != nil {
		return generationArtifacts{}, err
	}
	if err := ctx.Err(); err != nil {
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

func materializeArtifacts(ctx context.Context, artifacts generationArtifacts, hooks buildHooks) (Snapshot, error) {
	records, postings, queryIndex, err := hooks.decodeIndex(ctx, artifacts.payload)
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return Snapshot{}, err
	}
	if err != nil || len(records) != artifacts.manifestValue.RecordCount || len(postings) != artifacts.manifestValue.PostingCount {
		return Snapshot{}, ErrStoreCorrupt
	}
	return Snapshot{
		Manifest: artifacts.manifestValue, Records: records, Postings: postings,
		Query:         queryIndex,
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
	return validateGenerationMetadataContext(context.Background(), filesystem, generations, token)
}

func validateGenerationMetadataContext(ctx context.Context, filesystem storeFilesystem, generations *boundary.StateDirectory, token string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
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
	recordCount, postingCount, err := validateIndexContext(ctx, payload)
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return err
	}
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

func inspectGenerationContext(ctx context.Context, filesystem storeFilesystem, generations *boundary.StateDirectory, token string) (Status, error) {
	if err := ctx.Err(); err != nil {
		return Status{}, err
	}
	if !generationNamePattern.MatchString(token) {
		return Status{}, ErrStoreCorrupt
	}
	directory, err := filesystem.openDirectory(generations, token)
	if err != nil {
		return Status{}, fmt.Errorf("open generation: %w", corrupt(err))
	}
	defer filesystem.closeDirectory(directory)
	wantNames := []string{readyFilename, indexFilename, manifestFilename}
	names, err := filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return Status{}, ErrStoreCorrupt
	}
	manifestBytes, err := filesystem.readFile(directory, manifestFilename, maximumManifestBytes)
	if err != nil {
		return Status{}, ErrStoreCorrupt
	}
	manifest, err := decodeManifest(manifestBytes)
	if err != nil || tokenFromIdentity(manifest.GenerationIdentity) != token {
		return Status{}, ErrStoreCorrupt
	}
	ready, err := filesystem.readFile(directory, readyFilename, 72)
	if err != nil || !bytes.Equal(ready, []byte(manifest.GenerationIdentity+"\n")) {
		return Status{}, ErrStoreCorrupt
	}
	payload, err := filesystem.readFile(directory, indexFilename, maximumEncodedIndexBytes)
	if err != nil || sha256ID(payload) != manifest.PayloadDigest || manifest.IndexIdentity != manifest.PayloadDigest {
		return Status{}, ErrStoreCorrupt
	}
	recordCount, postingCount, err := validateIndexContext(ctx, payload)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return Status{}, err
		}
		return Status{}, ErrStoreCorrupt
	}
	if recordCount != manifest.RecordCount || postingCount != manifest.PostingCount {
		return Status{}, ErrStoreCorrupt
	}
	identity, err := computeGenerationIdentity(manifest)
	if err != nil || identity != manifest.GenerationIdentity {
		return Status{}, ErrStoreCorrupt
	}
	names, err = filesystem.names(directory, len(wantNames))
	if err != nil || !slices.Equal(names, wantNames) {
		return Status{}, ErrStoreCorrupt
	}
	if err := ctx.Err(); err != nil {
		return Status{}, err
	}
	installedBytes := int64(len(payload)) + int64(len(manifestBytes)) + int64(len(ready))
	return Status{Ready: true, Manifest: manifest, IndexIdentity: manifest.IndexIdentity, GenerationIdentity: manifest.GenerationIdentity, InstalledBytes: installedBytes}, nil
}

func computeGenerationIdentity(manifest model.Manifest) (string, error) {
	return computeGenerationIdentityContext(context.Background(), manifest, nil)
}

func computeGenerationIdentityContext(ctx context.Context, manifest model.Manifest, observed func(buildPhase)) (string, error) {
	if !manifestVariableBounds(manifest) {
		return "", ErrInvalidManifest
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	identityManifest := cloneManifest(manifest)
	identityManifest.GenerationIdentity = zeroSHA256Identity
	encoded, err := encodeManifestContext(ctx, identityManifest, observed)
	if err != nil {
		return "", err
	}
	return sha256MaterialIDContext(ctx, []byte("taf-generation-v2\x00"), encoded, observed, buildPhaseGenerationDigest)
}

func loadCurrentOptional(filesystem storeFilesystem, state, generations *boundary.StateDirectory) (Snapshot, []byte, bool, error) {
	return loadCurrentOptionalContext(context.Background(), filesystem, state, generations)
}

func loadCurrentOptionalContext(ctx context.Context, filesystem storeFilesystem, state, generations *boundary.StateDirectory) (Snapshot, []byte, bool, error) {
	return loadCurrentOptionalContextObserved(ctx, filesystem, state, generations, nil)
}

func loadCurrentOptionalContextObserved(ctx context.Context, filesystem storeFilesystem, state, generations *boundary.StateDirectory, observed func()) (Snapshot, []byte, bool, error) {
	token, current, exists, err := readCurrentPointer(filesystem, state)
	if err != nil || !exists {
		return Snapshot{}, current, exists, err
	}
	snapshot, err := loadGenerationContextObserved(ctx, filesystem, generations, token, observed)
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
	return loadGenerationContext(context.Background(), filesystem, generations, token)
}

func loadGenerationContext(ctx context.Context, filesystem storeFilesystem, generations *boundary.StateDirectory, token string) (Snapshot, error) {
	return loadGenerationContextObserved(ctx, filesystem, generations, token, nil)
}

func loadGenerationContextObserved(ctx context.Context, filesystem storeFilesystem, generations *boundary.StateDirectory, token string, observed func()) (Snapshot, error) {
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
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
	records, postings, queryIndex, err := decodeIndexContextWithQueryObserved(ctx, payload, observed)
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return Snapshot{}, err
	}
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
	return Snapshot{Manifest: manifest, Records: records, Postings: postings, Query: queryIndex, IndexIdentity: manifest.IndexIdentity, InstalledBytes: installedBytes}, nil
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
