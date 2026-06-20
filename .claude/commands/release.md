Release a new version of v-helper.

## Steps

### 1. Determine the next version

If the user specified a version explicitly in their message, use that.

Otherwise, check the latest tag and decide the bump by reviewing the diff (step 2 below). Apply semver rules:

- **major** (x+1.0.0) — breaking changes: removed/renamed env vars or API endpoints, incompatible config changes
- **minor** (x.y+1.0) — new user-visible features: new endpoints, new env vars, new services
- **patch** (x.y.z+1) — bug fixes, internal refactors, documentation only

When in doubt between two levels, pick the higher one. State your reasoning in one sentence before proceeding.

### 2. Collect changes since the last release

Run both commands to understand what changed:
```bash
git log $(git describe --tags --abbrev=0)..HEAD --oneline
git diff $(git describe --tags --abbrev=0)..HEAD
```

Group the changes into categories: Fixed, Security, Changed, Added, Removed.
Write clear human-readable entries — not just commit hashes.

Then finalize the version bump decision from step 1 based on what you found.

### 3. Update CHANGELOG.md

Prepend a new section after the `# Changelog` header line:

```
## NEW_VERSION

### Fixed
- ...

### Changed
- ...
```

Only include categories that have entries. Keep the style consistent with existing entries (bold lead phrase, em dash, explanation).

### 4. Update README.md and other docs

Scan for any references to the old version number and update them.
If new env vars, API endpoints, or services were added, update the relevant sections.

### 5. Stage, commit, tag, push

```bash
git add -A
git commit -m "NEW_VERSION - BRIEF_SUMMARY

LONGER_DESCRIPTION_IF_NEEDED"
git tag NEW_VERSION
git push
git push origin NEW_VERSION
```

The commit message subject should be `{version} - {one-line summary of the most significant change}`.

## Important

- Do not skip the diff review — changelog entries must reflect actual code changes, not guesses.
- Do not create a release if there are uncommitted changes unrelated to the release (ask the user first).
- Confirm the tag and push steps with the user before running them, since they are not reversible.
