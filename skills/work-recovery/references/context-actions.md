# Optional Context Actions

Native Git recovery is complete without an index or third-party provider. Do not discover, install, build, update, query, or contact a provider automatically.

When the user explicitly asks to use indexed/provider context or supplies provider evidence:

- prefer an already-present project architecture/index manifest over building a new index;
- verify repository identity, HEAD/dirty binding, freshness, capabilities, and locality before using it;
- treat current Git facts as authoritative for worktree state;
- use fresh provider evidence only for relationships it actually covers;
- preserve stale, partial, denied, or unavailable provider status and continue with native evidence;
- request separate, exact consent before any provider execution, network access, installation, index build/update, or persistent write.

Never make recovery wait for a full-project index. Never place a full index or large source/document corpus into model context; request only the smallest relevant slice if separately authorized.
