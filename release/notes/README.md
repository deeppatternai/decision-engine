# Decision Engine release notes

New release projections carry reviewed English notes in `release/notes/v<VERSION>.md`.
The filename and first line must match `VERSION`. The signed release tool reads
the file from the exact release commit, displays the complete body in its
publication dry-run, and uses that same body for GitHub Release reconciliation
and recovery. It appends the signed commit and release sequence automatically.

Use only these headings, omitting sections with no entries:

- `## Added`
- `## Changed`
- `## Fixed`
- `## Known limitations`

Each section must have at least one non-empty `- ` bullet. Continuation lines
may be indented by two spaces. Do not add a metadata section; the release tool
owns the signed provenance. Files must be UTF-8, regular files, and at most
128 KiB.

For example:

```markdown
# Decision Engine v1.2.3

## Added

- Added native Linux setup diagnostics.

## Fixed

- Fixed popup recovery after failed requests.

## Known limitations

- Linux GUI support requires a desktop session and a compatible WebView runtime.
```

Describe observable behavior from the actual public code diff against the
previous stable release. Commit subjects and filenames alone are insufficient.
Keep notes cumulative across all snapshots of the same unreleased version;
update them when later snapshots add changes. Keep prior version files intact.
Review notes for confidential information using the same public-content gates
as the code. Do not copy private development history into notes.

For a release with no functional or source-code changes, use a `## Changed`
bullet stating: `This release contains no functional or source-code changes.`
Mention metadata or documentation changes separately when relevant.

Legacy release commits without a versioned notes file retain their original
commit-title notes for recovery. New public projections containing this guide
must include the current version's reviewed notes. An existing malformed file
stops formal release instead of falling back to commit titles.

This format is introduced by a tooling change, which does not itself perform a
release projection or rewrite historical release bodies. Before the next
projection, prepare the safe current VERSION and its actual cumulative notes,
classify the notes file, and review its content digest before committing it.
