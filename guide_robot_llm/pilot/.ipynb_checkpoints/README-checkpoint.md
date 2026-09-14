# Pilot manifest (30-episode offline pilot, 2026-09-13)

Format: **one JSON object per line** (`manifest.jsonl`), each object following
`episode_template.json`. The template is a pointing example; other tracks swap
`gold.type` and `gold`:

| track | `gold.type` | gold value |
|---|---|---|
| pointing | `target_box` | target `candidate_id` + `box_px` + `distractors` |
| scene | `claims` / `unanswerable` | atomic supported claims (≤ ~6 per episode) or explicit unanswerable label |
| audience | `count` (eval-only) | integer per the **frozen visible-person rule** (see below) |
| tool | `action` / `abstention` | exact `{tool, args}` per ADR-0001 + allowed-state, or justified abstention reason |

Rules:

- `episode_id` = `<TRACK>-<SRC>-<NNN>`; TRACK ∈ {POI, SCN, AUD, TOL}; SRC ∈ {SR (surrogate real), CC (controlled)}.
- `split_group_id`: all episodes using one image (or one staged session) share a
  group ID; a group is never split across halves.
- `mission_snapshot.allowed_tools` must match the per-state fixture in
  `docs/pilot_skillset_and_vlm_candidates.md` A.3 for the given state (it is a
  fixture field, derived from `tools/schema.py` — do not hand-invent tool names).
- `robot_pose`/`sensors` are **always `not_observed`** in this pilot (no robot
  recordings); the `not_observed` block lists what exactly is absent.
- `media.sha256` computed at ingest; recompute on any re-export.
- Rights: one row per media item in `rights_ledger.csv` (media_id, uri, source,
  consent_ref, identifiable_people, license_status, reviewer).
- Review: `review.status` ∈ pending → passed/failed; dual annotation for
  answerable/ambiguous/safe; disagreements go to `adjudication_record.md`.

## Frozen visible-person rule (audience track)

Count = number of **people whose face or upper torso is fully or partially
visible in the frame and whose body is not occluded to less than 50% by
foreground objects**. Reflections, photo prints, and mannequins do not count.
Partially occluded people count if ≥50% of the person is visible. (Adopted
2026-09-13, **approved by user 2026-09-14** — final for all audience labeling.)

## CC track status (Day-2, 2026-09-13)

Controlled (synthetic) track: 10 deterministic PIL frames
(`tools/cc_scene_gen.py`, seed 20260914, re-run byte-identical) with per-scene
ground truth in `fixtures/cc/`, 15 episode rows + rights rows built by
`tools/build_cc_manifest.py`. Dual review, all passed:

- `tools/check_cc.py` — gold ↔ GT/catalog/A.3 (schema, visible-person rule,
  `allowed_tools` projection, freeze-hash gate on `schema.py`); self-check via
  `--tamper sha256|tools` (must exit 1);
- `tools/verify_cc_pixels.py` — frame ↔ GT pixel conformance, 10/10 frames
  (object fills, head/torso vs. occluder, pointing tip vs. target, plaque text);
- visual pass: agent ASCII renderings of all 10 frames — layout matches GT.

No disagreements — see `adjudication_record.md`. Review written by
`tools/apply_cc_review.py` (re-runs both checkers first, refuses to write on
failure; sets `review.*` and `gold.annotated_by`).

| episode | state | gold.type | review |
|---|---|---|---|
| POI-CC-001 | IDLE | target_box | passed |
| POI-CC-002 | IDLE | target_box | passed |
| POI-CC-003 | IDLE | target_box | passed |
| SCN-CC-001 | IDLE | claims | passed |
| SCN-CC-002 | IDLE | claims | passed |
| SCN-CC-003 | IDLE | unanswerable | passed |
| AUD-CC-001 | IDLE | count | passed |
| AUD-CC-002 | IDLE | count | passed |
| TOL-CC-001 | IDLE | action | passed |
| TOL-CC-002 | NAVIGATING | action | passed |
| TOL-CC-003 | NARRATING | action | passed |
| TOL-CC-004 | NARRATING | action | passed |
| TOL-CC-005 | ANSWERING | action | passed |
| TOL-CC-006 | IDLE | action | passed |
| TOL-CC-007 | NARRATING | abstention | passed |
