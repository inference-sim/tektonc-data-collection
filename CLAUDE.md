# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **tektonc-data-collection**, a Tekton pipeline orchestration project for LLM benchmarking. The core tool is `tektonc`, a Jinja2-based template expander that generates valid Tekton Pipeline YAML from templates with loop constructs.

## Commands

### Install dependencies
```bash
pip install -r tektonc/requirements.txt
```

### Test template compilation (dry run)
```bash
python tektonc/tektonc.py \
  -t tektoncsample/quickstart/pipeline.yaml.j2 \
  -f tektoncsample/quickstart/values.yaml \
  --explain
```

### Build a pipeline
```bash
python tektonc/tektonc.py \
  -t TEMPLATE.yaml.j2 \
  -f values.yaml \
  -o output/pipeline.yaml
```

### Deploy tasks and run pipeline (requires Tekton cluster)
```bash
# Deploy steps and tasks
for step in tekton/steps/*.yaml; do kubectl apply -f $step; done
for task in tekton/tasks/*.yaml; do kubectl apply -f $task; done

# Deploy pipeline and run
kubectl apply -f pipeline.yaml
kubectl apply -f pipelinerun.yaml

# Monitor
tkn pr list
tkn tr logs <taskrun_name> -f
```

## Architecture

### Directory Structure
- `tektonc/` - Template compiler (Python, ~540 lines)
- `tekton/tasks/` - Reusable Tekton Task definitions (25+ YAML files)
- `tekton/steps/` - Tekton StepAction definitions
- `tektoncsample/` - Example pipeline templates with values files

### tektonc Template Engine

The compiler uses a **two-pass rendering system**:
1. **Outer pass**: Renders template variables from values.yaml, preserves undefined loop variables
2. **Inner pass**: Strict mode, resolves loop variables during expansion

**Loop construct** (the only extension to standard Tekton YAML):
```yaml
loopName: <id>
foreach:
  domain:
    var1: [a, b, c]
    var2: [x, y]
tasks:
  - name: "task-{{ var1 }}-{{ var2|dns }}"
    ...
```

**Custom Jinja filters**:
- `dns` - DNS-1123 compatible string (lowercase, alnum, dash, max 63 chars)
- `slug` - Looser slug for params (keeps letters/numbers/._-)

**Special features**:
- `__jinja__` blocks for inline Jinja that gets evaluated during inner pass
- `vars` key in loops for computed variables scoped to loop iterations
- Cartesian product expansion (deterministic, keys sorted)
- Nested loops supported with variable scoping

### CLI Reference
```
tektonc -t TEMPLATE -f VALUES [-r PIPELINERUN] [-o OUTPUT] [--explain] [--debug]
```

| Flag | Description |
|------|-------------|
| `-t, --template` | Jinja template file (pipeline.yaml.j2) |
| `-f, --values` | YAML values file |
| `-r, --pipelinerun` | Optional PipelineRun for parameter overrides |
| `-o, --out` | Output file (default: stdout) |
| `--explain` | Print task/dependency table |
| `--debug` | Enable full traceback |

## Key Tekton Tasks

Tasks in `tekton/tasks/` cover: model deployment (vLLM), gateway configuration (Istio/KGateway), inference testing, data collection, OTEL integration, and S3 uploads.

## Contributing

- Keep new features minimal and Tekton-native
- Avoid adding new syntax unless absolutely necessary
- Add new examples under `tektoncsample/` with clear templates and values files
