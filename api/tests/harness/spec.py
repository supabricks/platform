"""The specification as the tests see it: one document, and what it requires."""
import copy
import functools
import pathlib

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

API = pathlib.Path(__file__).resolve().parents[2]
METHODS = ('get', 'post', 'put', 'patch', 'delete')
SERVED = ('implemented', 'complete')
URN = 'urn:supabricks:api'


@functools.cache
def _load(path):
    return yaml.safe_load(path.read_text())


def _pointer(document, pointer):
    for part in [p for p in pointer.split('/') if p]:
        document = document[part.replace('~1', '/').replace('~0', '~')]
    return document


def _component(path, pointer):
    """shared/schemas.yaml#/Problem -> Shared.Problem"""
    return f"{path.parent.name.title().replace('-', '')}.{pointer.strip('/')}"


@functools.cache
def bundle():
    """openapi.yaml with every file reference resolved.

    Schemas move to components (so recursive ones stay references); parameters,
    responses and path items are copied in place.
    """
    root_path = API / 'openapi.yaml'
    root = copy.deepcopy(_load(root_path))
    schemas = root.setdefault('components', {}).setdefault('schemas', {})

    def walk(node, base):
        if isinstance(node, list):
            return [walk(item, base) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get('$ref')
        if not isinstance(ref, str):
            return {key: walk(value, base) for key, value in node.items()}
        file, _, pointer = ref.partition('#')
        target = (base.parent / file).resolve() if file else base
        if target == root_path:
            return node
        if target.name == 'schemas.yaml':
            name = _component(target, pointer)
            if name not in schemas:
                schemas[name] = None  # reserve before walking, for recursion
                schemas[name] = walk(_pointer(_load(target), pointer), target)
            return {'$ref': f'#/components/schemas/{name}'}
        return walk(_pointer(_load(target), pointer), target)

    root['paths'] = walk(root['paths'], root_path)
    return root


@functools.cache
def status():
    return yaml.safe_load((API / 'status.yaml').read_text())


@functools.cache
def source_domains():
    """operationId -> domain directory, from the unbundled files."""
    result = {}
    for item in _load(API / 'openapi.yaml')['paths'].values():
        file, _, key = item['$ref'].partition('#/')
        target = (API / file).resolve()
        for method in METHODS:
            if method in _load(target)[key]:
                result[_load(target)[key][method]['operationId']] = target.parent.name
    return result


class Operation:
    def __init__(self, path, method, body, shared_parameters):
        self.path, self.method, self.body = path, method.upper(), body
        self.id = body['operationId']
        self.parameters = shared_parameters + body.get('parameters', [])
        self.domain = source_domains()[self.id]
        override = (status().get('operations') or {}).get(self.id)
        self.stage = (override or status()['domains'][self.domain])['stage']
        self.served = self.stage in SERVED
        self.secured = body.get('security', bundle().get('security')) != []

    def __repr__(self):
        return self.id

    def has_parameter(self, name):
        return any(p['name'].lower() == name.lower() for p in self.parameters)

    def url(self, **parameters):
        path = self.path
        for name, value in parameters.items():
            path = path.replace('{' + name + '}', str(value))
        assert '{' not in path, f'{self.id}: missing a path parameter for {path}'
        return path


@functools.cache
def operations():
    found = []
    for path, item in bundle()['paths'].items():
        for method in METHODS:
            if method in item:
                found.append(Operation(path, method, item[method], item.get('parameters', [])))
    return found


def served():
    return [o for o in operations() if o.served]


def operation(operation_id):
    return next(o for o in operations() if o.id == operation_id)


@functools.cache
def _registry():
    return Registry().with_resource(URN, Resource.from_contents(bundle(), default_specification=DRAFT202012))


def schema_errors(schema, value):
    """Why `value` does not match a schema from the bundle, or []."""
    # References are made absolute so they resolve against the bundle, not
    # against the fragment being checked.
    validator = Draft202012Validator(_absolute(schema), registry=_registry(),
                                     format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(value)]


def _absolute(node):
    if isinstance(node, list):
        return [_absolute(item) for item in node]
    if isinstance(node, dict):
        return {k: (URN + v if k == '$ref' and isinstance(v, str) and v.startswith('#') else _absolute(v)) for k, v in node.items()}
    return node


def response_errors(op, status_code, content_type, body):
    """Why a response is not one the specification allows for `op`, or []."""
    declared = op.body['responses']
    entry = declared.get(str(status_code)) or declared.get('default')
    if entry is None:
        return [f'{op.id}: status {status_code} is not declared (declared: {sorted(declared)})']
    content = entry.get('content')
    if not content:
        return [] if body in (None, b'', '') else [f'{op.id}: {status_code} declares no body but one was sent']
    media = (content_type or '').split(';')[0].strip()
    if media not in content:
        return [f'{op.id}: {status_code} was sent as {media!r}; declared: {sorted(content)}']
    schema = content[media].get('schema')
    return schema_errors(schema, body) if schema else []


def problem_errors(status_code, content_type, body):
    """Why an error response is not a well-formed problem document, or []."""
    if (content_type or '').split(';')[0].strip() != 'application/problem+json':
        return [f'error {status_code} was sent as {content_type!r}, not application/problem+json']
    errors = schema_errors({'$ref': '#/components/schemas/Shared.Problem'}, body)
    if not errors and body['status'] != status_code:
        errors.append(f"problem says status {body['status']} but the response is {status_code}")
    return errors
