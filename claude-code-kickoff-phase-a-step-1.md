# Kickoff: Phase A — Step 1

You are starting work on **OmniLab**, a bootc-based Linux OS for ROS 2 + Gazebo developers. The full spec is in `project-spec-v1.md` (in this directory). **Read it first** — it defines the architecture, scope, and conventions. Do not skim.

## Scope of THIS session

Phase A, Step 1 ONLY. Quoting the spec:

> Repo scaffold + GH Actions `build-host-iso.yml`. Directory structure, mkdocs skeleton, README, and a working CI workflow that produces a *minimal* host ISO. The host image at this point is just Fedora bootc + XFCE + Podman + a `hello-omnilab` script. No CLI yet, no skill-packs. The point is to prove the build-and-publish pipeline works.

Do **not** start Phase B work (project images, full CLI, real udev rules, NVIDIA tier, smoke tests beyond "ISO boots"). Stop at the end of Phase A Step 1 and report back.

## Identity

- GitHub org: `dhworg`
- Repo: `github.com/dhworg/omnilab` (already created, empty, public)
- Image namespace: `ghcr.io/dhworg/`
- License: Apache 2.0
- Default branch: `main`

## Acceptance criteria

This step is done when ALL are true:

1. Repo has the directory structure from the spec's "Repo layout" section, populated with skeleton files. Empty placeholders are fine — mark them with a `TODO` comment that references the spec section.
2. `host/Containerfile` builds a minimal bootc image: `quay.io/fedora/fedora-bootc:42` base + XFCE + Podman + a single `/usr/bin/hello-omnilab` script that prints a banner. Nothing else.
3. `.github/workflows/build-host-iso.yml` runs on push to `main` and on PRs:
   - Builds the host OCI image
   - On push to `main`: pushes to `ghcr.io/dhworg/omnilab-host:latest` and `:sha-<commit>`
   - Generates an ISO using `bootc-image-builder` (`quay.io/centos-bootc/bootc-image-builder`)
   - Uploads the ISO as a workflow artifact (downloadable from the run page)
4. `CLAUDE.md` exists at repo root containing: project summary, current phase, architecture in ~5 lines, identity, locked stack, conventions, stop-and-ask rules, pointer to `project-spec-v1.md`. Future Claude Code sessions will read this on start.
5. `README.md` exists with: project pitch, status (Phase A bootstrap), quickstart for downloading the ISO from Actions, link to docs site (placeholder OK).
6. `LICENSE` (Apache 2.0).
7. `mkdocs.yml` + `docs/index.md` skeleton, mkdocs-material theme. `mkdocs serve` works locally.
8. `.gitignore` appropriate for Python + container artifacts.
9. First commit pushed, first CI run completes **green**, ISO artifact is downloadable from the Actions UI.

## Tools you have

- `gh` CLI is authenticated. Use it for: triggering workflows (`gh workflow run`), watching runs (`gh run watch`), checking status (`gh run list`), downloading artifacts (`gh run download`), editing repo settings.
- `git` is configured.
- CI uses `GITHUB_TOKEN` automatically for GHCR pushes — don't ask the user for the PAT they created (it's for later manual pushes from the test machine).

## Stop-and-ask rules

Pause and ask the user when:

- Something contradicts `project-spec-v1.md`
- Scope creep beyond Phase A Step 1 is tempting (defer with a `TODO` and a note in the session summary)
- The first CI run fails for non-obvious reasons (read logs, try **one** fix, then ask)
- You're choosing between options where the wrong choice creates rework

Do **not** ask about:
- Style choices the spec or conventions already cover
- Implementation details inside a single component
- Which tool to use when the spec already named one

## Workflow

1. Read `project-spec-v1.md` end-to-end.
2. **Propose a concrete plan** for this session — file list, ordering, acceptance check at each milestone. Show it to the user before writing any code.
3. Wait for approval (or pushback) on the plan.
4. Execute. Commit in logical chunks. Conventional Commits format (`feat:`, `chore:`, `ci:`, `docs:`).
5. Push to `main`, watch the CI run with `gh run watch`, fix if it fails, confirm the ISO artifact downloads.
6. Print a session summary: what was built, what passed acceptance, what didn't, TODOs left, recommended next step (Phase A Step 2 — test machine bootstrap, which is a human-driven step).

**Begin by reading the spec and proposing the plan.**
