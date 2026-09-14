#!/usr/bin/env python3
"""Print one step's `script` from a Tekton Task YAML.

Shared by the prepare-trace tests so the extraction logic cannot drift
between them. Usage: extract_step.py <task.yaml> <step-name>
"""
import sys

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_step.py <task.yaml> <step-name>", file=sys.stderr)
        return 2
    path, want = sys.argv[1], sys.argv[2]
    with open(path) as fh:
        task = yaml.safe_load(fh)
    for step in task["spec"]["steps"]:
        if step["name"] == want:
            sys.stdout.write(step["script"])
            return 0
    names = ", ".join(s["name"] for s in task["spec"]["steps"])
    print(f"no step named {want!r}; have: {names}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
