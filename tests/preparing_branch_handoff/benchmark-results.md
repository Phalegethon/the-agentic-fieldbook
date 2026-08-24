# Collector Benchmark Results

Command:

```bash
python3 tests/preparing_branch_handoff/benchmark_collector.py
```

Environment: Git 2.53.0, Python 3.9.6, Apple Silicon (`arm64`).

Exact output:

```json
{
  "platform": "macOS-26.5.2-arm64-arm-64bit",
  "results": [
    {
      "name": "small",
      "files": 10,
      "lines_per_file": 20,
      "elapsed_seconds": 0.091,
      "dossier_chars": 5060,
      "ledger_files": 10
    },
    {
      "name": "medium",
      "files": 60,
      "lines_per_file": 40,
      "elapsed_seconds": 0.096,
      "dossier_chars": 42071,
      "ledger_files": 60
    },
    {
      "name": "large",
      "files": 500,
      "lines_per_file": 80,
      "elapsed_seconds": 0.233,
      "dossier_chars": 120000,
      "ledger_files": 500
    }
  ]
}
```

All ledger counts equal the requested file counts. Every dossier is at or
below the 120,000-character ceiling. Timings measure collector execution only;
they do not claim the model-synthesis target.
