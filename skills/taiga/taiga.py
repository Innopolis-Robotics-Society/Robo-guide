#!/usr/bin/env python3
"""taiga.py — minimal no-dependency CLI for the self-hosted Taiga instance.

Stdlib only (urllib). Token file default: ~/.taiga_token.
Credentials for auto-login: --user/--password flags or TAIGA_USER/TAIGA_PASSWORD.

Commands:
  login                          store JWT in token file
  projects                       list projects
  list issues [--project N] [--status NAME]
  list epics [--project N]
  statuses [issues|epics] [--project N]
  types issues [--project N]
  search <query> [--project N]
  show issue <id-or-ref> [--project N]
  show epic  <id-or-ref> [--project N]
  create issue --project N --title T [--body-file F | --body TEXT] [--type NAME|ID]
  create epic  --project N --title T [--body-file F | --body TEXT]
  set-status issue|epic <id-or-ref> <status-name> [--project N]
  assign issue|epic <id-or-ref> <username> [--project N]
  update issue|epic <id-or-ref> [--subject S] [--body-file F] [--set k=v ...]
  publish-plan MANIFEST.json     bulk publish: ordered issues + epic + backlinks
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_API = "https://corgi.sinorin.ru/api/v1"
TOKEN_FILE = os.path.expanduser("~/.taiga_token")


# ---------------- auth / http ----------------

def load_token():
    try:
        return json.load(open(TOKEN_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(payload):
    with open(TOKEN_FILE, "w") as f:
        json.dump(payload, f)
    os.chmod(TOKEN_FILE, 0o600)


def login(api, user, password):
    req = urllib.request.Request(
        api + "/auth",
        data=json.dumps({"type": "normal", "username": user,
                         "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    payload = json.load(urllib.request.urlopen(req))
    save_token(payload)
    return payload["auth_token"]


def get_token(api, args, force=False):
    if not force:
        stored = load_token()
        if stored and stored.get("auth_token"):
            return stored["auth_token"]
    user = getattr(args, "user", None) or os.environ.get("TAIGA_USER")
    password = getattr(args, "password", None) or os.environ.get("TAIGA_PASSWORD")
    if not user or not password:
        die("no token file and no credentials (--user/--password or "
            "TAIGA_USER/TAIGA_PASSWORD)")
    return login(api, user, password)


def call(api, token, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(api + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:500]}",
              file=sys.stderr)
        raise


def call_maybe_relogin(api, args, method, path, payload=None):
    token = get_token(api, args)
    try:
        return call(api, token, method, path, payload)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        token = get_token(api, args, force=True)
        return call(api, token, method, path, payload)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------- resolvers ----------------

def resolve(api, token, kind, key, project):
    """kind: 'issue'|'epic'; key: int id or #ref. Returns detail object."""
    base = {"issue": "issues", "epic": "epics"}[kind]
    key = key.lstrip("#")
    if key.isdigit():
        obj = call(api, token, "GET", f"/{base}/{key}")
        if obj.get("project") == project or project is None:
            return obj
    # fall back: find by ref within project
    for o in call(api, token, "GET", f"/{base}?project={project}"):
        if str(o.get("ref")) == key:
            return call(api, token, "GET", f"/{base}/{o['id']}")
    die(f"{kind} with id/ref {key} not found in project {project}")


def patch(api, token, kind, obj, fields):
    base = {"issue": "issues", "epic": "epics"}[kind]
    payload = dict(fields)
    payload["version"] = obj["version"]
    return call(api, token, "PATCH", f"/{base}/{obj['id']}", payload)


def status_id(api, token, kind, name, project):
    base = {"issue": "issue-statuses", "epic": "epic-statuses"}[kind]
    for s in call(api, token, "GET", f"/{base}?project={project}"):
        if s["name"].lower() == name.lower():
            return s["id"]
    die(f"status {name!r} not found; run: taiga.py statuses {kind}")


def type_id(api, token, spec, project):
    for t in call(api, token, "GET", f"/issue-types?project={project}"):
        if t["name"].lower() == str(spec).lower() or str(t["id"]) == str(spec):
            return t["id"]
    die(f"issue type {spec!r} not found; run: taiga.py types issues")


def user_id(api, token, username):
    for u in call(api, token, "GET", f"/users?query={username}"):
        if u["username"].lower() == username.lower():
            return u["id"]
    die(f"user {username!r} not found")


def option_id(api, token, endpoint, name, project):
    """priority/severity name -> pk id (case-insensitive)."""
    if str(name).isdigit():
        return int(name)
    for o in call(api, token, "GET", f"/{endpoint}?project={project}"):
        if o["name"].lower() == name.lower():
            return o["id"]
    avail = ", ".join(
        o["name"] for o in call(api, token, "GET", f"/{endpoint}?project={project}"))
    die(f"{endpoint.rstrip('s')} {name!r} not found (available: {avail})")


def read_body(args):
    if getattr(args, "body_file", None):
        return open(args.body_file).read()
    if getattr(args, "body", None):
        return args.body
    return ""


# ---------------- commands ----------------

def cmd_login(api, args):
    token = get_token(api, args, force=True)
    print(f"logged in as {args.user or os.environ.get('TAIGA_USER')}; "
          f"token -> {TOKEN_FILE}")


def cmd_projects(api, args):
    for p in call_maybe_relogin(api, args, "GET", "/projects"):
        print(f"{p['id']}\t{p['slug']}\t{p['name']}")


def cmd_list(api, args):
    kind = args.kind  # issues | epics
    project = args.project
    token = get_token(api, args)
    items = call(api, token, "GET", f"/{kind}?project={project}")
    if args.status:
        sid = status_id(api, token,
                        "issue" if kind == "issues" else "epic",
                        args.status, project)
        items = [o for o in items if o.get("status") == sid]
    for o in items:
        print(f"#{o.get('ref')}\t(id {o['id']})\t{o.get('status', '')}\t"
              f"{o['subject']}")
    print(f"— {len(items)} {kind}", file=sys.stderr)


def cmd_statuses(api, args):
    kind = args.kind or "issues"
    base = {"issues": "issue-statuses", "epics": "epic-statuses"}[kind]
    for s in call_maybe_relogin(api, args, "GET", f"/{base}?project={args.project}"):
        mark = " (default)" if s.get("is_default") else ""
        print(f"{s['id']}\t{s['name']}{mark}")


def cmd_types(api, args):
    for t in call_maybe_relogin(api, args, "GET", f"/issue-types?project={args.project}"):
        print(f"{t['id']}\t{t['name']}\t{t['color']}")


def cmd_search(api, args):
    for o in call_maybe_relogin(api, args, "GET",
                                f"/issues?project={args.project}&query={args.query}"):
        print(f"#{o.get('ref')}\t(id {o['id']})\t{o['subject']}")


def cmd_show(api, args):
    token = get_token(api, args)
    obj = resolve(api, token, args.kind, args.key, args.project)
    if args.json:
        out(obj)
    else:
        print(f"#{obj.get('ref')} (id {obj['id']})  {obj['subject']}")
        print(f"status: {obj.get('status')}  type: {obj.get('type')}  "
              f"version: {obj['version']}")
        print(obj["description"])


def cmd_create(api, args):
    kind = args.kind
    body = read_body(args)
    payload = {"subject": args.title, "description": body,
               "project": args.project}
    if kind == "issue" and args.type:
        payload["type"] = type_id(api, get_token(api, args),
                                  args.type, args.project)
    obj = call_maybe_relogin(api, args, "POST",
                             {"issue": "/issues", "epic": "/epics"}[kind], payload)
    print(f"created {kind} #{obj.get('ref')} (id {obj['id']}): {obj['subject']}")


def cmd_set_status(api, args):
    token = get_token(api, args)
    obj = resolve(api, token, args.kind, args.key, args.project)
    sid = status_id(api, token, args.kind, args.status, args.project)
    patch(api, token, args.kind, obj, {"status": sid})
    print(f"#{obj.get('ref')} -> status {args.status}")


def cmd_assign(api, args):
    token = get_token(api, args)
    obj = resolve(api, token, args.kind, args.key, args.project)
    uid = user_id(api, token, args.username)
    try:
        patch(api, token, args.kind, obj, {"assigned_to": uid})
    except urllib.error.HTTPError:
        die(f"assign failed — note: {args.username} must be a member of the "
            f"project (ask the admin to add a membership)")
    print(f"#{obj.get('ref')} assigned to {args.username}")


def cmd_update(api, args):
    token = get_token(api, args)
    obj = resolve(api, token, args.kind, args.key, args.project)
    fields = {}
    if args.subject:
        fields["subject"] = args.subject
    if args.body_file:
        fields["description"] = open(args.body_file).read()
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k == "priority":
            fields[k] = option_id(api, token, "priorities", v, args.project)
        elif k == "severity":
            fields[k] = option_id(api, token, "severities", v, args.project)
        elif k == "milestone":
            fields[k] = int(v)
        else:
            die(f"unsupported --set field {k!r} (use: priority, severity, milestone)")
    if not fields:
        die("nothing to update (--subject / --body-file / --set)")
    patch(api, token, args.kind, obj, fields)
    print(f"#{obj.get('ref')} updated: {list(fields)}")


def cmd_publish_plan(api, args):
    man = json.load(open(args.manifest))
    project = man["project"]
    issues_spec = man["issues"]
    epic_spec = man.get("epic")

    token = get_token(api, args)
    existing = {o["subject"]: o for o in
                call(api, token, "GET", f"/issues?project={project}")}
    existing_epics = call(api, token, "GET", f"/epics?project={project}")

    created = []  # (ref, id, title)
    for i, spec in enumerate(issues_spec, start=1):
        body = spec.get("body") or (open(spec["body_file"]).read()
                                    if spec.get("body_file") else "")
        title = spec["title"]
        if title in existing:
            obj = existing[title]
            assert obj["ref"] == i, \
                f"issue {i}: existing ref {obj['ref']} != expected {i}"
            created.append((i, obj["id"], title))
            print(f"reuse   #{i}  {title[:70]}")
            continue
        payload = {"subject": title, "description": body, "project": project}
        if spec.get("type"):
            payload["type"] = type_id(api, token, spec["type"], project)
        obj = call(api, token, "POST", "/issues", payload)
        assert obj["ref"] == i, f"issue {i}: expected ref {i}, got {obj['ref']}"
        created.append((i, obj["id"], title))
        print(f"created #{i}  {title[:70]}")

    if epic_spec:
        ebody = epic_spec.get("body") or (open(epic_spec["body_file"]).read()
                                          if epic_spec.get("body_file") else "")
        match = [e for e in existing_epics if e["subject"] == epic_spec["title"]]
        if match:
            epic = call(api, token, "GET", f"/epics/{match[0]['id']}")
            print(f"reuse   epic #{epic.get('ref')}  {epic_spec['title'][:60]}")
        else:
            epic = call(api, token, "POST", "/epics",
                        {"subject": epic_spec["title"], "description": ebody,
                         "project": project, "epics_order": 1})
            print(f"created epic #{epic.get('ref')}  {epic_spec['title'][:60]}")
        # backlinks (idempotent: skip if the line already present)
        prefix = man.get("backlink_prefix", "**Parent:** #{ref}")
        line = prefix.replace("{ref}", str(epic["ref"]))
        for ref, iid, title in created:
            obj = call(api, token, "GET", f"/issues/{iid}")
            if line in obj["description"]:
                continue
            patch(api, token, "issue", obj,
                  {"description": f"{line}\n\n{obj['description']}"})
        print(f"backlink '{line}' ensured on {len(created)} children")

    # verification
    issues = call(api, token, "GET", f"/issues?project={project}")
    epics = call(api, token, "GET", f"/epics?project={project}")
    problems = []
    if len(issues) < len(issues_spec):
        problems.append(f"expected >= {len(issues_spec)} issues, got {len(issues)}")
    if epic_spec and epic_spec["title"] not in [e["subject"] for e in epics]:
        problems.append("epic title missing")
    if problems:
        die("; ".join(problems))
    print(f"\nRESULT OK: {len(issues)} issues, {len(epics)} epics in project {project}")


# ---------------- main ----------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=os.environ.get("TAIGA_API", DEFAULT_API))
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")

    sub.add_parser("projects")

    sp = sub.add_parser("list")
    sp.add_argument("kind", choices=["issues", "epics"])
    sp.add_argument("--project", type=int, default=1)
    sp.add_argument("--status", default=None)

    sp = sub.add_parser("statuses")
    sp.add_argument("kind", nargs="?", choices=["issues", "epics"])
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("types")
    sp.add_argument("what", choices=["issues"])
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("show")
    sp.add_argument("kind", choices=["issue", "epic"])
    sp.add_argument("key", help="id or #ref")
    sp.add_argument("--project", type=int, default=1)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("create")
    sp.add_argument("kind", choices=["issue", "epic"])
    sp.add_argument("--project", type=int, default=1)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", default=None)
    sp.add_argument("--body-file", default=None)
    sp.add_argument("--type", default=None,
                    help="issue only: type name or id (default: project default)")

    sp = sub.add_parser("set-status")
    sp.add_argument("kind", choices=["issue", "epic"])
    sp.add_argument("key", help="id or #ref")
    sp.add_argument("status")
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("assign")
    sp.add_argument("kind", choices=["issue", "epic"])
    sp.add_argument("key")
    sp.add_argument("username")
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("update")
    sp.add_argument("kind", choices=["issue", "epic"])
    sp.add_argument("key")
    sp.add_argument("--subject", default=None)
    sp.add_argument("--body-file", default=None)
    sp.add_argument("--set", action="append", default=[])
    sp.add_argument("--project", type=int, default=1)

    sp = sub.add_parser("publish-plan")
    sp.add_argument("manifest", help="JSON: {project, issues:[{title,body|body_file,type?}], epic:{title,body|body_file}?}")

    args = p.parse_args()
    if args.cmd == "login":
        return cmd_login(args.api, args)
    cmds = {
        "projects": cmd_projects, "list": cmd_list, "statuses": cmd_statuses,
        "types": cmd_types, "search": cmd_search, "show": cmd_show,
        "create": cmd_create, "set-status": cmd_set_status,
        "assign": cmd_assign, "update": cmd_update,
        "publish-plan": cmd_publish_plan,
    }
    cmds[args.cmd](args.api, args)


if __name__ == "__main__":
    main()
