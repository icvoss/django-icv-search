# Releasing

How to cut a release for django-icv-search.

## TL;DR

```
branch  ->  PR  ->  review  ->  merge to main  ->  tag the merged commit  ->  CI publishes
```

The **tag is the trigger**. Pushing a tag of the form `v<semver>` runs the publish workflow, which tests, builds, uploads to PyPI (OIDC trusted publishing, no token), and creates a GitHub release. **Tagging is publishing. There is no separate "publish" step and no undo.**

## Canonical flow

1. **Branch** off `main`:
   `release/<version>` (e.g. `release/1.2.0`).
2. **Bump** on the branch:
   - `pyproject.toml` (`version` field)
   - `src/icv_search/__init__.py` (`__version__`: keep in sync)
   - `CHANGELOG.md`: rename `[Unreleased]` to `[<version>] - <YYYY-MM-DD>` (today's date)
3. **Open a PR** to `main`. CI runs lint and tests. Get it reviewed. This is the gate: do not skip it.
4. **Merge to `main`** (squash or merge, per repo norm).
5. **Tag the merged commit on `main`** and push the tag:
   ```bash
   git checkout main && git pull
   git tag v<version>
   git push origin v<version>
   ```
6. **Watch the publish run** and confirm PyPI:
   ```bash
   gh run watch <run-id> --exit-status
   curl -s https://pypi.org/pypi/django-icv-search/json | python -c \
     "import sys,json;print(json.load(sys.stdin)['info']['version'])"
   ```

Tag the commit that is on `main`, not a feature branch. Tags point at commits, not branches, so tagging a feature branch will publish code that may never have been merged. Always tag after the merge so what is on PyPI is exactly what is on `main`.

## Tag format (strict)

`v<semver>`: the letter `v` followed by the version number.

| Version | Tag example |
| --- | --- |
| 1.1.3 | `v1.1.3` |
| 1.2.0 | `v1.2.0` |
| 2.0.0 | `v2.0.0` |

## Versioning (SemVer)

[Semantic Versioning](https://semver.org/). The rules apply in full since this package is at 1.x:

- **Patch** (`1.1.3 -> 1.1.4`): bug fixes, doc-only changes, no API or behaviour change.
- **Minor** (`1.1.3 -> 1.2.0`): new public API, any behaviour change (even a safer one), or a raised minimum dependency floor (e.g. Django version).
- **Major** (`1.x -> 2.0`): breaking changes to the public API.

If in doubt between patch and minor, choose minor. Burning a version number is free; shipping a behaviour change as a patch surprises consumers.

## CHANGELOG (required for every release)

**A release must include a CHANGELOG entry for its version. No entry, no tag.** CI enforces this: the publish workflow will fail before building if the entry is missing.

[Keep a Changelog](https://keepachangelog.com/) format. Accumulate entries under `## [Unreleased]` as you work; at release time, rename that heading to `## [<version>] - <YYYY-MM-DD>`. Subsections: Added / Changed / Fixed / Removed.

Call out behaviour changes explicitly, including ones that are "safer": a consumer relying on the old behaviour still needs to know.

The GitHub release body is generated automatically from the tag, but that is **not** a substitute for the curated CHANGELOG entry. Write the CHANGELOG by hand so consumers reading the package on PyPI or GitHub get a human-authored summary, not just a commit list.

## Django pin in the publish workflow

The publish workflow's test job pins a specific Django version (`Django~=5.1.0`). When you raise the package's minimum Django floor in `pyproject.toml`, update the pin in the same PR, or the tagged build's test job can fail to resolve dependencies and block the publish.

## Pre-tag checklist

Before pushing the tag (the irreversible step):

- [ ] **CHANGELOG has a `[<version>] - <date>` entry** (renamed from `[Unreleased]`). Mandatory: every release ships with a changelog.
- [ ] Behaviour changes and breaking changes called out in that CHANGELOG entry.
- [ ] Version bumped in `pyproject.toml` **and** `src/icv_search/__init__.py`, and they match.
- [ ] CI Django pin matches the package's minimum, if the floor changed.
- [ ] Tests pass locally and the package builds (`python -m build` at the repo root).
- [ ] The PR is **merged to `main`** and you are tagging that commit.
- [ ] Tag format is `v<version>`.
- [ ] This exact version has never been published (PyPI rejects re-uploads).

## If something goes wrong

- **PyPI rejects the upload (version exists).** That version is permanently taken: you cannot re-upload, even after deleting. Bump to the next patch and re-tag.
- **The test/build job fails after tagging.** Nothing was published (publish is the last job and depends on test and build). Fix on a new PR, merge, delete the bad tag (`git push --delete origin <tag>`), and re-tag the new commit with the **same** version (since nothing reached PyPI).
- **Published, but the code is not on `main`.** Open a PR from the release branch to `main` immediately and merge, so `main` reflects what is on PyPI. Avoid this by always tagging after the merge.

## Optional hardening

Consider adding a **manual approval gate** to the `publish` job via a protected GitHub Environment (`pypi`), so "push tag" and "irreversibly upload to PyPI" are decoupled: a human approves the upload after seeing test and build go green. The workflow already declares `environment: pypi`; add a required-reviewer protection rule to that environment to enable the gate.
