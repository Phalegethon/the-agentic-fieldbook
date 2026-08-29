// Command taf-level1 executes one bounded native Level 1 request.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/engine"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

type executeFunc func(context.Context, wire.Envelope) (wire.Result, error)

func main() {
	installSIGPIPEHandling()
	os.Exit(runProduction(os.Stdin, os.Stdout, os.Stderr, os.Args[1:]))
}

func run(stdin io.Reader, stdout, stderr io.Writer) int {
	return runProduction(stdin, stdout, stderr, nil)
}

func runProduction(stdin io.Reader, stdout, stderr io.Writer, arguments []string) int {
	server, observeCounters, validArguments := productionFlags(arguments)
	if !validArguments {
		writeReason(stderr, "invalid-native-level1-request")
		return 2
	}
	dependencies := engine.ProductionDependencies()
	var observed *model.WorkCounters
	var observationErr error
	if observeCounters {
		dependencies.ObserveUpdateCounters = func(counters model.WorkCounters) {
			copy := counters
			observed = &copy
			if server {
				observationErr = writeUpdateCounters(stderr, counters)
			}
		}
	}
	production := engine.New(dependencies)
	if server {
		production = engine.NewCached(dependencies)
	}
	execute := production.Execute
	if server && observeCounters {
		execute = func(ctx context.Context, envelope wire.Envelope) (wire.Result, error) {
			observationErr = nil
			result, err := production.Execute(ctx, envelope)
			if err == nil && observationErr != nil {
				return wire.Result{}, observationErr
			}
			return result, err
		}
	}
	if server {
		if _, err := io.WriteString(stderr, "__TAF_LEVEL1_SERVER_READY_V1__\n"); err != nil {
			return 3
		}
		return runServerWithExecutor(stdin, stdout, stderr, execute)
	}
	exit := runWithExecutor(stdin, stdout, stderr, execute)
	if exit == 0 && observed != nil {
		if err := writeUpdateCounters(stderr, *observed); err != nil {
			return 3
		}
	}
	return exit
}

func productionFlags(arguments []string) (server, observeCounters, valid bool) {
	if len(arguments) > 2 {
		return false, false, false
	}
	for _, argument := range arguments {
		switch argument {
		case "--serve":
			if server {
				return false, false, false
			}
			server = true
		case "--observe-update-counters":
			if observeCounters {
				return false, false, false
			}
			observeCounters = true
		default:
			return false, false, false
		}
	}
	return server, observeCounters, true
}

func runServerWithExecutor(stdin io.Reader, stdout, stderr io.Writer, execute executeFunc) int {
	maximum := policy.ProductionLimits().MaximumWireBytes
	reader := bufio.NewReaderSize(stdin, maximum+1)
	for {
		frame, err := reader.ReadSlice('\n')
		if errors.Is(err, io.EOF) && len(frame) == 0 {
			return 0
		}
		if err != nil || len(frame) > maximum {
			writeReason(stderr, "invalid-native-level1-request")
			return 2
		}
		if exit := runWithExecutor(bytes.NewReader(frame), stdout, stderr, execute); exit != 0 {
			return exit
		}
	}
}

func runWithExecutor(stdin io.Reader, stdout, stderr io.Writer, execute executeFunc) (exit int) {
	defer func() {
		if recover() != nil {
			writeReason(stderr, "native-level1-internal-error")
			exit = 3
		}
	}()
	limits := policy.ProductionLimits()
	envelope, err := wire.DecodeEnvelope(io.LimitReader(stdin, int64(limits.MaximumWireBytes)+1))
	if err != nil {
		writeReason(stderr, "invalid-native-level1-request")
		return 2
	}
	result, err := execute(context.Background(), envelope)
	if err != nil {
		if unsafeInvocation(err) {
			writeReason(stderr, "invalid-native-level1-request")
			return 2
		}
		writeReason(stderr, "native-level1-internal-error")
		return 3
	}
	if err := wire.EncodeResult(stdout, result); err != nil {
		writeReason(stderr, "native-level1-output-error")
		return 3
	}
	return 0
}

func unsafeInvocation(err error) bool {
	return errors.Is(err, wire.ErrInvalidWire) ||
		errors.Is(err, wire.ErrDuplicateKey) ||
		errors.Is(err, wire.ErrRequiredCapability) ||
		errors.Is(err, boundary.ErrRootOverlap) ||
		errors.Is(err, boundary.ErrUnsafeRoot) ||
		errors.Is(err, boundary.ErrUnsafePath)
}

func writeReason(stderr io.Writer, reason string) {
	if len(reason)+1 > policy.ProductionLimits().MaximumStderrBytes {
		return
	}
	_, _ = io.WriteString(stderr, reason+"\n")
}

type updateCounterObservation struct {
	ChangedPaths          int   `json:"changed_paths"`
	OpenedRepositoryFiles int   `json:"opened_repository_files"`
	ParsedRepositoryFiles int   `json:"parsed_repository_files"`
	ReadRepositoryBytes   int64 `json:"read_repository_bytes"`
}

func writeUpdateCounters(writer io.Writer, counters model.WorkCounters) error {
	encoded, err := json.Marshal(updateCounterObservation{
		ChangedPaths:          counters.ChangedPaths,
		ParsedRepositoryFiles: counters.ParsedRepositoryFiles,
		OpenedRepositoryFiles: counters.OpenedRepositoryFiles,
		ReadRepositoryBytes:   counters.ReadRepositoryBytes,
	})
	if err != nil {
		return err
	}
	framed := "__TAF_LEVEL1_UPDATE_COUNTERS_V1__=" + string(encoded) + "\n"
	if len(framed) > policy.ProductionLimits().MaximumStderrBytes {
		return io.ErrShortBuffer
	}
	_, err = io.WriteString(writer, framed)
	return err
}
