---
description: "Generate and commit the dependency SBOM for xspct_db (CycloneDX JSON via cyclonedx-bom)."
agent: agent
tools: [execute, read]
---

Generate the dependency SBOM for xspct_db and commit the result.

## Prerequisites

The active virtualenv must have all optional extras installed for full dependency coverage:

```bash
pip install -e ".[all,dev,docs]"
```

## Steps (run in order — stop and report on failure)

### 1. Generate dependency SBOM (CycloneDX JSON)

Covers the installed dependency tree:

```bash
.venv/bin/cyclonedx-py environment --of JSON --pyproject pyproject.toml \
  -o bom.json .venv/bin/python
```

`--pyproject` is required: without it the SBOM has no root component and the
`metadata.component` entry is empty, so nothing records which version of
xspct_db the dependency tree belongs to.

Report the number of components listed and confirm `metadata.component` reads
`xspct_db` at the current version:

```bash
python3 -c "import json; m=json.load(open('bom.json'))['metadata']['component']; print(m['name'], m['version'])"
```

### 2. Verify REUSE compliance still passes

```bash
.venv/bin/reuse lint
```

Stop on any violations.

### 3. Stage the file

```bash
git add bom.json
git diff --cached --stat
```

### 4. Commit

```bash
git commit -S -m "chore: update dependency SBOM"
```

Remind the user to verify their GPG key is available (`gpg --list-secret-keys`) before committing.
