"""An HTTP client that refuses any response the specification does not allow."""
from . import spec

PREFIX = '/api/v1'


class Client:
    def __init__(self, base, http, csrf, headers=None):
        self.base, self.http, self.csrf = base, http, csrf
        # Sent with every request: a bearer token on a governed server.
        self.headers = headers or {}

    def raw(self, method, path, *, json=None, params=None, headers=None, origin=True, csrf=True, data=None,
            timeout=15):
        """Send one request below /api/v1. Every error must be a problem document."""
        sent = {}
        if origin:
            sent['Origin'] = self.base
        if csrf and self.csrf and method.upper() not in ('GET', 'HEAD'):
            sent['X-CSRF-Token'] = self.csrf
        sent.update(self.headers)
        sent.update(headers or {})
        response = self.http.request(method, self.base + PREFIX + path, json=json, params=params,
                                     data=data, headers=sent, timeout=timeout)
        if response.status_code >= 400 and method.upper() != 'HEAD':
            errors = spec.problem_errors(response.status_code, response.headers.get('Content-Type'), _body(response))
            assert not errors, f'{method} {path}: {errors}'
        return response

    def call(self, operation_id, *, path=None, expect=None, **kwargs):
        """Call an operation by ID. The response must match the specification."""
        op = spec.operation(operation_id)
        response = self.raw(op.method, op.url(**(path or {})), **kwargs)
        errors = spec.response_errors(op, response.status_code, response.headers.get('Content-Type'), _body(response))
        assert not errors, errors
        if expect is not None:
            assert response.status_code == expect, f'{operation_id}: {response.status_code} {response.text}'
        return response


def _body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text
