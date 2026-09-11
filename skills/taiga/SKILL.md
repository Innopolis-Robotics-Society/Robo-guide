---
name: taiga
description: "Use this skill whenever the task involves the self-hosted Taiga tracker (corgi.sinorin.ru:11435): creating or publishing issues/epics (including bulk 'plan' publishing), updating issue statuses, assigning people, searching the board, or answering 'what is the state of <project>'. Wraps the no-dependency CLI helper at $HOME/.pi/agent/skills/taiga/taiga.py. Use it instead of hand-rolling curl/urllib against the Taiga API — the helper handles JWT auth, ref-vs-id resolution, and optimistic-lock versions."
---

# Taiga Skill

Operate the self-hosted **Taiga 6.10** instance via the bundled CLI
(`$HOME/.pi/agent/skills/taiga/taiga.py`, Python 3 stdlib only). The skill is
versioned in this repo at `skills/taiga/` and installed by symlinking:
`ln -s <repo>/skills/taiga ~/.pi/agent/skills/taiga`.

- Public API base (default): `https://corgi.sinorin.ru/api/v1` — works from the
  host and from any outside machine. Override with `--api` or `TAIGA_API`
  (on the host you may prefer `http://localhost:11435/api/v1` instead).
- Default project: **VLM** (id 1, slug `vlm`).
- Full external-agent instructions (for machines without this skill):
  `docs/taiga.md` in the `Robo-guide_vlm_team_one` repo.

## First run

```bash
TAIGA=$HOME/.pi/agent/skills/taiga/taiga.py
# login once per machine; stores JWT in ~/.taiga_token (chmod 600)
python3 $TAIGA --user andrei --password <pw> login
# or set TAIGA_USER / TAIGA_PASSWORD env vars and just run commands
python3 $TAIGA projects
```

Account table lives in `docs/taiga.md` (section 2). Never print passwords in
issue bodies, logs, or commits.

## Command reference

```bash
python3 $TAIGA list issues [--project 1] [--status "In progress"]
python3 $TAIGA list epics
python3 $TAIGA search "pointing" [--project 1]
python3 $TAIGA show issue 7            # id or #ref both work
python3 $TAIGA show issue 7 --json
python3 $TAIGA statuses issues
python3 $TAIGA types issues
python3 $TAIGA create issue --title "T" --body-file /tmp/body.md --type Enhancement
python3 $TAIGA create epic  --title "T" --body-file /tmp/epic.md
python3 $TAIGA set-status issue 7 "In progress"
python3 $TAIGA assign issue 7 andrei
python3 $TAIGA update issue 7 --body-file /tmp/new.md --set priority=High --set severity=Important
python3 $TAIGA publish-plan /tmp/plan.json
```

Notes:

- There is **no comments API** in this Taiga build — add notes to the issue
  description instead.
- `assign` only works for **project members** (the API rejects non-members);
  membership is managed via the UI/DB, not the issues API.
- `--set priority=`/`--set severity=` take **names** and the CLI resolves them to
  pk ids (`/priorities`, `/severities`). Raw API calls must send ids.

## Bulk-publishing a plan (the important workflow)

`publish-plan` creates an ordered list of issues + one parent epic with
predictable `#N` refs, then backlinks children to the epic. Manifest format:

```json
{
  "project": 1,
  "issues": [
    {"title": "...", "body": "markdown", "type": "Enhancement"},
    {"title": "...", "body_file": "/abs/path.md"}
  ],
  "epic": {"title": "...", "body": "- [ ] #1 ..."},
  "backlink_prefix": "**Parent:** #{ref}"
}
```

The repo copy of the CLI (`skills/taiga/taiga.py`) is identical to the installed
one, so it can be run directly from a clone on machines without the skill installed.

Rules that make this safe:

1. **Order defines refs.** The shared per-project counter assigns `ref` at
   creation time: children first (in dependency order), epic last. The script
   asserts each returned `ref` matches the expected number and aborts otherwise.
2. **Idempotent by title.** Re-running reuses issues/epics with the same `subject`;
   it never deletes. Never delete-and-recreate — it desyncs the ref sequence.
3. **Epic checklist convention:** one `- [ ] #N` line per child (renders as a
   live checkbox link).
4. **Backlinks:** each child body gets `**Parent:** #<epic>` prepended (skipped
   if already present).

## API gotchas (why the helper exists)

- Serialized reference field is **`ref`**, not `reference`.
- Detail URLs use DB **id**; `#N` is the `ref`. Helper resolves either.
- Every `PATCH` needs the current **`version`** (optimistic lock) — helper reads
  it fresh before patching.
- List endpoints omit `description`; detail endpoints have it.
- `POST /api/v1/auth` returns **`auth_token`** (not `auth`/`token`) plus
  `refresh`; use `Authorization: Bearer <auth_token>`. No CSRF with Bearer.
- Self-registration is disabled; accounts are provisioned by the admin.
- The VLM project is publicly readable (view-only) but writes require login.

## Statuses / types (VLM project)

Issue statuses: New (default), In progress, Ready for test, Closed, Needs Info,
Rejected, Postponed. Issue types: Bug, Question, Enhancement. Always confirm with
`statuses issues` / `types issues` — ids can change.

## Current board (as of 2026-09-11)

Epic #14 "Integrate a remotely served VLM as the RoboGuide skill orchestrator"
with 13 children #1–#13 (VLM orchestrator research MVP, dependency-ordered). See
`docs/taiga.md` section 9 for the list.
