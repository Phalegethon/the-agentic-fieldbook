# Conditional Tool Setup

Load this reference only when Git or a working Python 3 command is missing.
Detect first, in this order:

```text
git --version
python3 --version
python --version
```

Python is available only when either Python command reports Python 3. Identify
the operating system and available package manager. Before asking for explicit
user confirmation, show the applicable install command, its purpose, and the
verification commands. Do not execute any install, download, or package-manager
command until the user has explicitly confirmed it.

Use these exact commands, keeping the numbered commands separate and ordered:

```text
macOS Homebrew: brew install git python
Debian/Ubuntu 1: sudo apt-get update
Debian/Ubuntu 2: sudo apt-get install -y git python3
Fedora: sudo dnf install -y git python3
Arch: sudo pacman -S --needed git python
Windows winget 1: winget install --id Git.Git -e
Windows winget 2: winget install --id Python.Python.3 -e
```

After a user-approved installation completes, verify:

```text
git --version
python3 --version
python --version
```

Confirm Git and that one Python command is Python 3, then rerun the collector.

`gh` is only a fallback for user-authorized GitHub reads/comments and is never
required; connection and posting rules live in `platform-actions.md`. `rg` is
only for a user-approved, changed-file-scoped deep dive. `ast-grep` is only for
an explicitly requested semantic deep dive and never for the main handoff.
All are optional and never block the core report. No project-language toolchain
or pip package is required.
