# Research note: synthesizing the 500-episode VLM pilot dataset

**Date:** 2026-09-13
**Related:** Taiga issue [#9](https://corgi.sinorin.ru/project/vlm) "Defining and collecting the 500-episode RoboGuide VLM pilot dataset" (epic #14), ADR-0001 (remote VLM action contract).
**Status:** research note, not a design decision. Proposes synthesis as the primary (or partial) route for the pilot dataset; the issue itself defers collection-vs-synthesis.

## 1. What the dataset must be

Per issue #9, a frozen set of **500 episodes**:

| Track | Episodes | Gold fields (per episode) |
|---|---|---|
| Pointing | 150 | pointing box or ray, target candidate, visible candidate IDs |
| Scene questions | 120 | scene atomic claims, answerable/ambiguous flags, gold tool + args |
| Audience | 80 | audience count + label (eval-only, never runtime — ADR-0001) |
| Passage diagnostic | 50 | sensor-derived passage labels (diagnostic only) |
| Orchestration + red-team | 100 | gold tool + args or justified abstention |

Each episode: image or video clip, utterance (text + optional audio reference), mission snapshot (state, allowed tools, IDs of visible candidates), robot pose, session ID, annotator IDs, subjective labels (answerability, ambiguity, safety) with dual annotation + adjudication and Cohen's kappa. Splits are by **session / room / visitor / exhibit group** (no item-level splits); a hidden challenge set is generated *after* prompts are frozen. The dataset lives outside the main model repo; this repo keeps only the manifest schema, validation/split scripts, and unit tests (under `guide_robot_llm/scripts/`, package layout deferred by the issue).

## 2. Core insight

Almost every gold field is **programmatically derivable from a scene whose ground truth is controlled by the generator**:

- visible candidate IDs, robot pose, allowed tools → scene manifest + mission snapshot;
- pointing box / ray → object pose projected through camera intrinsics;
- audience count → number of humans placed in the scene;
- passage labels → simulated occupancy / sensor data;
- scene claims → scene attributes, verifiable by code.

In real collection these are the expensive annotations (manual boxes, claims, candidate lists). In synthesis, **the scene generator is the annotator**. This is an established pattern (see §4 prior art), not speculative.

## 3. Synthesis pipeline (proposed)

```
scene YAML (room, exhibits w/ STL+descriptions, visitor placement, mission snapshot)
  │
  ▼
render backend (Gazebo | Blender | Gaussian splat of real rooms)
  → image/clip + ground-truth poses, masks, boxes, sensor data
  │
  ▼
utterance generator (LLM, conditioned on scene manifest + task template)
  │
  ▼
gold-label derivation (pure code: projection, counts, claim checks,
                      tool/args from mission state)
  │
  ▼
verification loop (LLM-proposed gold rejected unless the manifest check passes)
  │
  ▼
human dual annotation + adjudication (subjective labels only:
                      answerability, ambiguity, safety) → kappa recorded
  │
  ▼
split assignment at generation time (session IDs per room/exhibit group)
  → validation/split scripts verify, never fix
```

### 3.1 Render backends

| Backend | Pros | Cons | Verdict |
|---|---|---|---|
| **Gazebo** (Classic 11, already in repo: `simulation.launch.py`, robot URDF, full sensor stack) | Poses/scans/sonar are ground truth; episodes match the *runtime* pipeline exactly; zero new infra | Render quality is the weak point for a VLM-facing image set | Workhorse for passage + any sensor-consistent tracks |
| **Blender** (procedural museum rooms) | Photoreal stills; real exhibit STL/CAD already LFS-tracked; boxes/masks/poses free; camera fully controlled | New scene-asset work; no sensors (labels from manifest, not from simulated sonar) | Workhorse for pointing / scene / audience / red-team |
| **Gaussian splatting of the real Innopolis venue** | Real geometry + textures of *this* venue at arbitrary robot viewpoints → smallest synthetic→real gap | One-time capture per room; must capture without visitors (or mask them) — privacy; render cost per viewpoint | Realism booster / domain-gap probe, not the main axis |
| Text-to-image + grounding (SDXL/Flux + SAM) | Cheapest to start | Geometry unreliable; multi-episode scene consistency hard | Filler for red-team cases only |

Recommendation: **Gazebo or Blender as workhorse (decide by which scene-asset cost is lower for our exhibits), GS of real rooms as a realism booster.** Build 2–3 representative rooms once and reuse across all tracks — at 500 episodes the cost center is scene assets, not episode count.

### 3.2 Per-track synthesis

- **Pointing (150).** LLM generates referring expressions from object attributes + distractors in the scene; gold box = object pose → camera projection; gold ray = robot head pose. Real-egocentric pointing exists (EgoPoint-Ground, arXiv 2603.26646) but synthetic gives exact boxes for free.
- **Scene questions (120).** LLM generates visitor utterances conditioned on exhibit descriptions from the scene manifest. Museum-domain precedent: **MUSEUM-65** (arXiv 2412.01370) — 65M exhibit images / 200M QA pairs, expert-labeled, five VQA task families mirroring real visitor inquiries (exhibit description, style, material, function, conservation). Gold answers verified **against the manifest by code**, not by an LLM judge.
- **Audience (80).** Place N synthetic humans; count label = placement count. Synthetic→real crowd transfer is well established (SynMVCrowd benchmark; arXiv 2201.08992, 1903.03303). Labels are eval-only per ADR-0001, so renders only need to look plausible.
- **Orchestration (50).** **τ-bench pattern** (arXiv 2406.12045): scripted multi-turn tasks, LLM-simulated user, domain tools + policy, evaluated by pass^k reliability. Tools = the mission-state tool catalog; gold = correct tool/args or justified abstention per ADR-0001.
- **Red-team (50).** **InjecAgent** (arXiv 2403.02691) is the direct precedent for tool-integrated agents. Unique VLM twist: **indirect injection via text rendered in the image** — exhibit plaques, signage, "staff notes" in the scene — plus direct injection in the utterance. Gold = abstain / safe fallback, never a mutating action.
- **Passage (50).** Corridor scenes with occupancy ground truth; labels sensor-derived (sim sonar/scan, or short real corridor recordings with no visitors — diagnostic track, lowest bar).

### 3.3 Utterance generation and the verification loop

LLMs propose utterances (and, where needed, draft gold labels); **code disposes**. Every LLM-proposed gold field is checked against the scene manifest (candidate visible? claim consistent with scene attributes? tool allowed in this mission snapshot?) and rejected on mismatch. This keeps the manifest machine-verifiable and makes the "answerability/ambiguity" labels partly *derivable*: occlusion is computable (raycast — is the candidate actually visible?), and utterance ambiguity is partially computable (does the expression resolve to exactly one candidate?). What remains genuinely subjective (is the utterance natural, is the request safely answerable) goes to dual human annotation + adjudication, with kappa recorded — required by the issue even for synthetic episodes, because it validates the *label definitions*.

### 3.4 Splits come free

Episodes are generated in separate "recording sessions" per (room, exhibit group). Session IDs are assigned to train/dev/eval **at generation time**; whole room-exhibit combos are held out by construction; the hidden challenge set is a batch generated after prompts are frozen. The validation script then *verifies* the split rather than fixing it, and produces the leakage report required by the issue.

## 4. Prior art

| Source | What it shows | Relevance |
|---|---|---|
| arXiv 2210.00858 — 3D VLA synthetic tabletop dataset | Sim-generated 3D scenes → language-conditioned data; train in sim, evaluate real | The sim→real pattern for robot tasks |
| arXiv 2507.08513 — Ultimate3D | 3D assets → photorealistic renders (render + diffusion) → LLM instructions → 240K VQAs with exact camera-object annotations | Template for "3D assets → image + gold labels + text" |
| arXiv 2412.01370 — MUSEUM-65 | 65M exhibit images / 200M QA pairs, expert-labeled museum VQA, five task families | Museum-domain precedent for the scene-question track |
| arXiv 2406.12045 — τ-bench | Synthetic user + scripted tasks + tool catalog, pass^k reliability | Orchestration track + reliability metric |
| arXiv 2403.02691 — InjecAgent | Indirect prompt-injection benchmark for tool-integrated LLM agents | Red-team track; we add the image-text-injection twist |
| arXiv 2201.08992, 1903.03303, SynMVCrowd | Synthetic crowd data → real-domain transfer | Audience track |
| arXiv 2410.11285, large-scale indoor NVS (PMC11397877) | Novel-view synthesis from indoor reconstructions (incl. GS) | GS-of-real-rooms realism booster |
| github.com/remyxai/VQASynth — LLM-composed synthetic VQA over image corpora | Cheap LLM-only VQA composition | Filler, not core |
| github.com/ziyaow1010/vla-datasets-benchmarks | Survey of VLA dataset construction strategies | Landscape map |

## 5. Honest limitations (to record in the dataset card)

1. **Synthetic→real domain gap.** A VLM scoring well on Blender/Gazebo renders may not transfer to real Innopolis photos. Mitigation: GS renders of real rooms + a small real-photo validation slice (no identifiable visitors) to *measure* the gap explicitly; report per-split scores separately.
2. **Utterance naturalness.** LLM-generated visitor phrasing is more uniform than real speech; real red-team sessions should contribute a subset of real phrasings to the red-team track.
3. **What still has to be real.** The passage track can use real corridor recordings; anything that must exercise the real sensor stack (sonar behavior, latency) stays out of scope for the VLM pilot eval.

## 6. Proposed repo layout

Issue #9 keeps in this repo: manifest schema, validation/split scripts, unit tests; the dataset itself lives outside the main model repo.

```
guide_robot_llm/scripts/dataset/          # or sibling package — decision deferred by issue
  manifest_schema.json                    # episode manifest (v1), checksummed
  scene/
    schema.yaml                           # scene YAML spec
    rooms/<room>/…                        # 2–3 representative rooms (exhibits, STL refs)
  gen_episodes.py                         # scene YAML → episode JSONL
  gen_utterances.py                       # LLM utterance + gold draft, manifest-verified
  gen_redteam.py                          # InjecAgent-style cases (utterance + plaque injection)
  derive_gold.py                          # pure-code gold labels (projection, counts, claims)
  split_validate.py                       # split checks + leakage report
test/test_dataset.py                      # pure-Python, no ROS (per repo convention)
```

Build order: (1) schema + generator skeleton + unit tests (issue #9's in-repo deliverables, independent of render choice); (2) scene packs for 2–3 rooms; (3) utterance/red-team pipeline with the verification loop; (4) human dual annotation + kappa; (5) split validation report, manifest freeze; (6) hidden challenge batch after prompt freeze.

## 7. Open questions

- Gazebo vs Blender as workhorse — depends on exhibit asset availability (real STLs vs Gazebo models); needs a spike.
- Where do exhibit descriptions (for utterance generation) come from — the real tour catalog, or written per scene?
- Do we need video clips at all in the pilot, or do static frames suffice (the issue allows "image or video clip")? Frames are far cheaper and the ADR-0001 contract takes `image` anyway.
- Real-photo validation slice: who approves the privacy review (no identifiable visitors)?
