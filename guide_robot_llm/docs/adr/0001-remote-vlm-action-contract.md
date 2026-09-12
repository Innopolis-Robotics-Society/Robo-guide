# ADR-0001: Remote VLM orchestrator contract

- **Status:** accepted
- **Date:** 2026-09-11
- **Deciders:** VLM team (issue #1, parent #14)
- **Scope:** `guide_robot_llm` — the architectural boundary between the remote
  multimodal VLM and the robot's action pipeline (`tool_broker`).

## Context

The dialog agent (`dialog_agent_node`) is currently text-only: one
"action" LLM call picks a tool under a GBNF grammar, the call is executed
through `tool_broker`, and a second call formulates the spoken reply.
The VLM epic (#14) adds a **remote multimodal model** (vision) in front of
the same action pipeline. Before any pipeline code (image transport,
prompting, caching) is written, this ADR fixes the boundary: what the
model is allowed to emit, what the host validates, and what the model is
never trusted to do.

## Decision

### 1. Transport

The VLM is reached over an **OpenAI-compatible
`/v1/chat/completions`** endpoint (external server, not onboard).
Rationale (research evidence, recorded here per the issue):

- The pilot deployment already runs llama.cpp/Ollama-class servers that
  speak this protocol; OpenAI-compatible JSON keeps the client
  stack-agnostic (no vendor SDK in the robot image) and lets us swap
  model families without touching `tool_broker`.
- Vision is delivered as base64 data URLs in `image_url` content parts —
  no side channel for images.
- Constrained decoding (GBNF grammar in `grammar`) is a server feature
  (`grammar` field), so the strict action schema below can be enforced
  at generation time *and* re-validated host-side.

Transport details (timeouts, retries, image size) are pipeline
concerns and live under the reserved `vision.*` parameter namespace
(Decision 5). They are deliberately **not** part of this contract.

### 2. Final action contract (the single execution contract)

Every action the model emits — text-only or multimodal — is exactly this
JSON object, in this field order, nothing else:

```json
{
  "tool": "string",
  "args": {},
  "confidence": 0.0,
  "abstain": false
}
```

- `tool` — one of the mission-state-allowed tools (catalog in
  `tools/schema.py`).
- `args` — object; validated against the tool catalog and the live
  location/tour catalog.
- `confidence` — finite number in `[0, 1]`. The model's calibrated
  self-assessment that the chosen action matches the visitor's intent.
  **It is never used as trust for authorization**: a tool is executed if
  and only if the catalog, the mission state and the args validation all
  pass, regardless of `confidence`.
- `abstain` — boolean. `true` means "I cannot determine a safe,
  unambiguous action". When `abstain=true`, **mutating tools are
  forbidden** and the host performs the safe fallback (Decision 4);
  the turn does not execute any tool.

Invariants are **enforced by the validator, not the prompt**
(`tools/validate.py`: `parse_action` + `verify_action`). The prompt
describes the contract for the model; the grammar pins it at generation
time (`llm_client/grammar.py`: `build_action_grammar`); the validator is
the only authority that decides whether an action reaches
`tool_broker`.

### 3. `think` is out of the contract

The former free-form `think` field is **removed from the final action
contract**. Extra keys (including `think`) make the action
`malformed_output` and trigger the repair path. Rationale: free-form
chain-of-thought in the execution contract is unbounded latency and a
prompt-injection surface that no invariant can check. Where research or
debugging needs "why", the host records **explicit, enumerable evidence
fields and reason codes** (Decision 6) instead — measurable, not prose.

### 4. Abstention and the safe fallback

Three situations force abstention; all of them end in the **safe
fallback: a short clarification or uncertainty response, never a
mutating action**:

| trigger | reason code |
| --- | --- |
| model emitted `abstain: true` | `abstain_from_model` |
| `confidence < llm.action_confidence_threshold` | `low_confidence` |
| action rejected by catalog/state/args validation after repair exhausted | `illegal_state` / `unknown_id` / `invalid_args` |

A **low-confidence motor action must never execute**: `confidence`
below threshold converts the action to a safe abstention *before* the
broker is even consulted.

### 5. Parameter namespaces

- `vision.*` — reserved for the multimodal pipeline (prompt assembly,
  image handling, per-stage tuning). Not read by the action contract.
- `llm.action_*` — the action-contract knobs, currently:
  - `llm.action_confidence_threshold` (default `0.5`) — below this the
    action is a safe abstention;
  - `llm.action_repair_attempts` (default `1`) — how many times a
    malformed/rejected action output may be regenerated before the safe
    fallback.

### 6. Reason codes (final set)

`malformed_output`, `low_confidence`, `unknown_id`, `illegal_state`,
`abstain_from_model`, `invalid_args`. Emitted by the validator,
propagated on the turn result, logged per turn. They replace any
free-form "why" in execution records.

### 7. Backward compatibility with the text-only path

The text-only dialog keeps working unchanged in behavior: the same
action phase, same broker gates (`tools_allowed` by mission state),
same repair/answer flow. The only contract change is the envelope
(4 fields instead of 2) and the new abstention policy. Read-only tools
and mission-state gates are untouched — the broker's existing legality
checks remain the state authority; the validator adds the strict
envelope and pre-broker rejection.

## Terminology

- **model** — the remote LLM/VLM instance (one or more backends behind
  `complete_with_fallback`).
- **visual skill** — a vision capability the model applies when images
  are present (e.g. "which exhibit is the visitor pointing at"); always
  ends in the same action contract as text.
- **tool** — a broker-executable capability (`tools/schema.py`).
- **action** — one `{tool, args, confidence, abstain}` object.
- **abstention** — the model's or the policy's refusal to execute
  (explicit `abstain`, low confidence, exhausted repair).
- **safe fallback** — clarification/uncertainty response to the
  visitor; the only outcome of an abstention.

## Examples

Valid (executes if catalog/state allow):

```json
{"tool": "guide_to", "args": {"location_id": "cafe"}, "confidence": 0.92, "abstain": false}
```

Abstained (safe fallback, no execution):

```json
{"tool": "reply", "args": {}, "confidence": 0.3, "abstain": true}
```

Malformed (extra key `think` + trailing text → `malformed_output`,
repair):

```json
{"think": "hmm", "tool": "guide_to", "args": {"location_id": "cafe"}}
```

Unknown ID (`args` references a location not in the catalog →
`unknown_id`):

```json
{"tool": "guide_to", "args": {"location_id": "kitchen_42"}, "confidence": 0.9, "abstain": false}
```

Illegal state (tool not allowed in the current mission state →
`illegal_state`):

```json
{"tool": "start_tour", "args": {"tour_id": "lab_demo"}, "confidence": 0.9, "abstain": false}
```

## Research evidence (pilot, to be filled by #11)

- Raw vs repaired schema validity per turn:
  `action_first_attempt_valid` / `repair_used` / `action_reason_code`
  on `TurnResult` (measured in the interaction log, issue #8).
- Confidence threshold sweep: raw and repaired validity vs safe-fallback
  rate, recorded against `llm.action_confidence_threshold`.
