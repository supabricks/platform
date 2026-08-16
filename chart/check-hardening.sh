#!/usr/bin/env bash
# Rendered-manifest hardening check: the chart's security contract,
# enforced. Run locally or in CI: ./chart/check-hardening.sh
# Fails if a workload weakens the baseline without updating hardening.md's
# named exceptions below.
set -euo pipefail
cd "$(dirname "$0")/.."
helm template sspc chart -n sspc-cell > /tmp/sspc-hardening-render.yaml
python3 - <<'PY'
import sys, yaml

docs = [d for d in yaml.safe_load_all(open('/tmp/sspc-hardening-render.yaml')) if d]
fails = []

# Named exceptions (mirror chart/hardening.md):
TOKEN_ALLOWED = {'sspc-operator'}          # the only API consumer
NONROOT_EXEMPT = {'minio', 'minio-create-bucket', 'controller-pg',
                  'broker', 'storage-controller', 'pageserver', 'safekeeper'}

pods = []   # (workload_name, pod_template)
for d in docs:
    k = d.get('kind')
    if k in ('Deployment', 'StatefulSet'):
        pods.append((d['metadata']['name'], d['spec']['template']))
    elif k == 'Job':
        pods.append((d['metadata']['name'], d['spec']['template']))

for name, tpl in pods:
    spec = tpl.get('spec', {})
    meta = tpl.get('metadata', {})
    # Token automount is explicit everywhere; only the operator gets one.
    am = spec.get('automountServiceAccountToken')
    if name in TOKEN_ALLOWED:
        if am is not True:
            fails.append(f'{name}: operator must set automountServiceAccountToken: true explicitly')
    elif am is not False:
        fails.append(f'{name}: automountServiceAccountToken must be false')
    # Pod seccomp everywhere.
    if spec.get('securityContext', {}).get('seccompProfile', {}).get('type') != 'RuntimeDefault':
        fails.append(f'{name}: pod seccompProfile RuntimeDefault missing')
    # Every container drops privileges.
    for c in spec.get('containers', []) + spec.get('initContainers', []):
        ctx = c.get('securityContext', {})
        if ctx.get('allowPrivilegeEscalation') is not False:
            fails.append(f'{name}/{c["name"]}: allowPrivilegeEscalation false missing')
        if 'ALL' not in ctx.get('capabilities', {}).get('drop', []):
            fails.append(f'{name}/{c["name"]}: capabilities drop ALL missing')
        # No credential-looking literal env values.
        for e in c.get('env', []) or []:
            if 'value' in e and any(t in e['name'].upper() for t in ('PASSWORD', 'SECRET', 'ACCESS_KEY', 'TOKEN')):
                fails.append(f'{name}/{c["name"]}: credential env {e["name"]} is a literal value — use secretKeyRef')
    # Expected-digest annotation on every pod.
    if 'sspc.io/image-digest' not in (meta.get('annotations') or {}):
        fails.append(f'{name}: sspc.io/image-digest annotation missing')

# The network boundary renders by default.
np = [d['metadata']['name'] for d in docs if d.get('kind') == 'NetworkPolicy']
if 'default-deny-ingress' not in np:
    fails.append('NetworkPolicy default-deny-ingress not rendered')
if len(np) < 10:
    fails.append(f'expected >=10 NetworkPolicies, got {len(np)}: {np}')

# Operator RBAC must not grow without review (verb-set pin).
EXPECTED_ROLE = {
    ('', 'pods'): {'get', 'list', 'watch', 'create', 'patch', 'delete'},
    ('', 'services'): {'get', 'list', 'watch', 'create', 'patch', 'delete'},
    ('', 'configmaps'): {'get', 'list', 'watch', 'create', 'patch', 'delete'},
    ('', 'secrets'): {'get', 'list', 'watch', 'create', 'patch', 'delete'},
    ('', 'events'): {'get', 'list', 'watch', 'create', 'patch'},
}
for d in docs:
    if d.get('kind') == 'Role' and d['metadata']['name'] == 'sspc-operator':
        for rule in d.get('rules', []):
            for res in rule.get('resources', []):
                key = ('', res)
                if key in EXPECTED_ROLE and not set(rule.get('verbs', [])) <= EXPECTED_ROLE[key]:
                    fails.append(f'RBAC grew for {res}: {sorted(rule.get("verbs", []))} — review required')

if fails:
    print('HARDENING CHECK FAILED:')
    for f in fails:
        print(' -', f)
    sys.exit(1)
print(f'hardening check OK: {len(pods)} workloads, {len(np)} network policies')
PY
