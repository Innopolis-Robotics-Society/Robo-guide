#!/usr/bin/env python3
"""Render a config template by substituting ${section.key} from robot_params.yaml.

Everything outside placeholders - comments included - is copied verbatim.
An unknown key is a hard error: the build fails instead of shipping a
silently wrong config.
"""

import re
import sys
import os
import yaml

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\}")


def lookup(params, dotted):
    node = params
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"'{dotted}' not found in robot_params.yaml")
        node = node[key]
    return node


def as_yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return f"'{value}'"


def render(params_file, template_file, out_file):
    with open(params_file) as f:
        params = yaml.safe_load(f)
    with open(template_file) as f:
        template = f.read()

    # First pass: collect every missing key so one build reports them all.
    missing = []
    for m in PLACEHOLDER.finditer(template):
        try:
            lookup(params, m.group(1))
        except KeyError:
            missing.append(m.group(1))
    if missing:
        names = "\n  ".join(sorted(set(missing)))
        raise SystemExit(
            f"{template_file}: keys not found in {params_file}:\n  {names}"
        )

    rendered = PLACEHOLDER.sub(
        lambda m: as_yaml_scalar(lookup(params, m.group(1))), template
    )

    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    with open(out_file, "w") as f:
        f.write(rendered)

def main():
    render(*sys.argv[1:4])


if __name__ == "__main__":
    main()