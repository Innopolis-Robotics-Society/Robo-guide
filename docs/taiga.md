# Working with Taiga — agent setup instructions

Self-contained instructions for AI agents (and humans) on **other machines** that
need to create and manage issues in our self-hosted Taiga. Everything here works
over plain HTTPS/HTTP + JSON; no browser, no GUI, no special libraries — `curl` or
`urllib` are enough.

## 1. Instance

| What | Value |
|---|---|
| Public base URL | `https://corgi.sinorin.ru` |
| API base | `https://corgi.sinorin.ru/api/v1` |
| Version | Taiga 6.10.2 (Django REST, OpenAPI 3.0) |
| UI | `https://corgi.sinorin.ru/project/vlm` |
| Project | **VLM** — id `1`, slug `vlm` |
| Note | All endpoints below were verified against the running backend (6.10.2) |

The instance is reachable from the internet at the public URL. On the host machine
it is also at `http://localhost:11435`.

## 2. Accounts

All team accounts have full admin access to the VLM project (Product Owner role).
Login is by **username + password** (not email).

| Username | Password |
|---|---|
| `andrei` | `GRZMfXgoFepmMomq` |
| `daria` | `3uOtYXdy54DniWJM` |
| `evgenii` | `do87ak4QNfcGizMB` |
| `ilya` | `6wAhbW3eBDqQmqNG` |
| `taisiia` | `kr0b6I8lOubHhWq8` |

> ⚠️ This file contains live credentials. Treat it as a secrets file: do not echo
> passwords into logs, commits, or public issue bodies. Rotate by asking the server
> admin (`sinorin` account) if this file leaks.

## 3. Authentication

```bash
# Get a JWT (access token + refresh token)
curl -s -X POST https://corgi.sinorin.ru/api/v1/auth \
  -H 'Content-Type: application/json' \
  -d '{"type": "normal", "username": "andrei", "password": "GRZMfXgoFepmMomq"}'
```

Response: JSON user object containing **`auth_token`** (JWT access token) and
**`refresh`** (refresh token). Every subsequent request uses:

```
Authorization: Bearer <auth_token>
```

Notes:

- Public self-registration is **disabled** — accounts are provisioned by the admin.
- CSRF is only enforced for session auth; Bearer-JWT requests need no CSRF token,
  so `PATCH`/`POST` work with plain `curl`.
- Access tokens expire; re-login is cheap. Store the token in a file
  (e.g. `~/.taiga_token`) and re-auth on HTTP 401.
- The `refresh` field can be used via `/api/v1/auth/refresh` (standard
  JWT refresh) if you want to avoid re-login.

## 4. Key endpoints (all under `/api/v1`)

| Purpose | Endpoint |
|---|---|
| List projects | `GET /projects` |
| List issues of project | `GET /issues?project=1` |
| Issue detail | `GET /issues/<id>` |
| Create issue | `POST /issues` |
| Update issue | `PATCH /issues/<id>` |
| List epics | `GET /epics?project=1` |
| Epic detail | `GET /epics/<id>` |
| Create epic | `POST /epics` |
| Issue statuses | `GET /issue-statuses?project=1` |
| Issue types | `GET /issue-types?project=1` |
| Epic statuses | `GET /epic-statuses?project=1` |
| Milestones / tags | `GET /milestones?project=1`, `GET /tags?project=1` |
| Priorities / severities | `GET /priorities?project=1`, `GET /severities?project=1` |
| Issue comments | `POST /issues/<id>/comments` `{"comment": "..."}` |
| Assign | `PATCH /issues/<id>` with `{"assigned_to": <user_id>, "version": N}` |

Issue type ids in VLM: `Bug=1`, `Question=2`, `Enhancement=3`.
Default issue status is `New` (id 1); others: `In progress` (2), `Ready for test`
(3), `Closed` (4), `Needs Info` (5), `Rejected` (6), `Postponed` (7).
Always query the endpoints above instead of hardcoding ids — they can change.

## 5. Critical conventions (read before writing)

1. **`ref` vs `id`.** Issues/epics have a DB `id` (used in URLs) and a human
   `ref` (the `#N` shown in the UI, e.g. `#14`). The serializer field is **`ref`**
   (not `reference`). Detail URLs use the **id**: `GET /issues/14` returns the
   object with id 14, whatever its ref is.
2. **One shared reference sequence per project.** Issues, epics and user stories
   draw from the *same* per-project counter. Creating objects changes everyone's
   next `ref`. To get a predictable layout, create objects in the order you want
   the numbers (e.g. 13 child issues first → `#1`–`#13`, then the epic → `#14`).
3. **Optimistic locking.** Every `PATCH` must include the current **`version`**
   of the object (integer, from `GET`); otherwise you get
   `"version parameter is not valid"`. Read fresh, then patch.
4. **List vs detail serializers.** `GET /issues?project=1` omits
   `description`/`description_html`; use the detail endpoint for full bodies.
5. **Native `#N` markdown links.** In issue/epic markdown, `#14` renders as a live
   link to the object with that ref (per project). Epic checklists use
   `- [ ] #N` lines — they render as clickable checkboxes.
6. **Idempotency.** The API has no "create if missing". To make bulk publishes
   resumable, match existing objects by exact `subject` (title) and skip
   re-creation. Never delete-and-recreate: it desyncs the `ref` sequence.
7. **Public read.** The VLM project is currently publicly *readable* (view-only)
   by anonymous users. Writes still require login. If that must change, ask the
   admin to set `is_private=true`.

## 6. Workflows

### Create an issue

```bash
TOKEN=$(cat ~/.taiga_token)   # see section 3
curl -s -X POST https://corgi.sinorin.ru/api/v1/issues \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"subject": "My new issue",
       "description": "Markdown body, may contain #14-style refs",
       "project": 1,
       "type": 3}'
```

Response includes `id` and `ref`. No `status` needed (defaults to New).

### Update an issue's status

```bash
# 1) resolve status id
curl -s "https://corgi.sinorin.ru/api/v1/issue-statuses?project=1" \
  -H "Authorization: Bearer $TOKEN"
# 2) get current version
OBJ=$(curl -s https://corgi.sinorin.ru/api/v1/issues/<id> -H "Authorization: Bearer $TOKEN")
VER=$(echo "$OBJ" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
# 3) patch
curl -s -X PATCH https://corgi.sinorin.ru/api/v1/issues/<id> \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"status\": 2, \"version\": $VER}"
```

Other updates work the same way (body fields + `version`): `description`,
`subject`, `assigned_to`, `priority` (pk id from `/priorities`),
`severity` (pk id from `/severities`), `milestone` (pk id), `tags` (list of
tag ids), `due_date`.

⚠️ `priority` and `severity` are **pk values, not name strings** — resolve the id
first from `/priorities?project=1` / `/severities?project=1` (VLM: priorities
Low/Normal/High; severities Wishlist/Minor/Normal/Important/Critical). Also note:
`assigned_to` must reference a **project member** — the API rejects non-members
with `The user must be a project member.`

### Publish a whole plan (ordered bulk create)

To publish N issues + 1 parent epic with predictable refs:

1. Verify current state: `GET /issues?project=1`, `GET /epics?project=1`.
   Compute expected next refs (max existing `ref` + 1 …).
2. Create child issues in dependency order, asserting each response `ref` equals
   the expected number.
3. Create the epic; its ref lands right after the children.
4. Patch each child body to prepend `**Parent:** #<epic_ref>` (skip if already
   present — idempotent).
5. Verify: counts, unique titles, every child has the parent link, epic checklist
   `- [ ] #N` appears exactly once per child.

This repo ships the idempotent implementation of this flow as the `taiga` pi-skill:
`skills/taiga/taiga.py publish-plan /path/to/manifest.json` (see section 10). The
original VLM-plan publish script lived on the host machine at
`/tmp/taiga_publish/publish.py` (not in git).

### Comment / assign

```bash
curl -s -X POST https://corgi.sinorin.ru/api/v1/issues/<id>/comments \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"comment": "status update"}'
```

## 7. Minimal Python helper (no dependencies)

```python
import json, urllib.request, urllib.error

API = "https://corgi.sinorin.ru/api/v1"

def login(username, password):
    req = urllib.request.Request(API + "/auth",
        data=json.dumps({"type": "normal", "username": username,
                         "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))["auth_token"]

def call(token, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {method} {path}: {e.read().decode()[:400]}")
        raise

# examples
tok = login("andrei", "GRZMfXgoFepmMomq")
call(tok, "POST", "/issues", {"subject": "t", "description": "d", "project": 1, "type": 3})
o = call(tok, "GET", "/issues/1")
call(tok, "PATCH", f"/issues/{o['id']}", {"status": 2, "version": o["version"]})
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` | Token expired → re-login (section 3) |
| `"version parameter is not valid"` | PATCH without/with stale `version` → re-GET and retry |
| Wrong object returned | You used `ref` in a URL; use `id` |
| `description` missing | You hit the list endpoint; use the detail endpoint |
| `Public registration is disabled.` | Self-signup is off; use a provisioned account |
| New object got an unexpected `ref` | Someone else created objects in between; re-query state before assuming numbers |

## 9. Installing the bundled pi-skill (for pi agents)

The repo includes a pi-skill at `skills/taiga/` (`SKILL.md` + `taiga.py`,
stdlib-only CLI). On a pi machine, install it once:

```bash
ln -s /path/to/this/repo/skills/taiga ~/.pi/agent/skills/taiga
python3 ~/.pi/agent/skills/taiga/taiga.py --help
```

If the agent is not pi, just run `skills/taiga/taiga.py` directly — it has no
dependencies beyond Python 3 stdlib.

## 10. Current project layout (as of 2026-09-11)

- **Epic #14** — *Integrate a remotely served VLM as the RoboGuide skill
  orchestrator* — `https://corgi.sinorin.ru/project/vlm/epic/14`
- **Issues #1–#13** — the VLM orchestrator research MVP, in dependency order:
  #1 ADR/contract → #2 camera pipeline → #3 multimodal backend → #4 turn context
  & prompts → #5 strict action schema → #6 `describe_scene` → #7
  `resolve_pointing` → #8 interaction log v6 → #9 500-episode dataset → #10 eval
  harness → #11 experiment matrix → #12 integrated safety tests → #13 docs &
  red-team handoff.
- Every child carries a `Parent: #14` backlink; the epic checklist references all
  children.
