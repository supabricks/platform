// The MCP client's error contract, tested outside production e2e: each of
// the three server error layers (HTTP, JSON-RPC protocol, tool-level) plus
// network death and malformed responses must produce a readable message —
// never an unrelated parsing exception.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { callTool, setToken, token } from './mcp'

function mockFetch(impl: () => Promise<Partial<Response>>) {
  vi.stubGlobal('fetch', vi.fn(impl))
}

function jsonResponse(status: number, body: unknown): Promise<Partial<Response>> {
  return Promise.resolve({
    status,
    json: () => Promise.resolve(body),
  })
}

const toolSuccess = (payload: unknown) =>
  jsonResponse(200, {
    jsonrpc: '2.0',
    id: 1,
    result: { content: [{ type: 'text', text: JSON.stringify(payload) }], isError: false },
  })

const toolError = (payload: unknown) =>
  jsonResponse(200, {
    jsonrpc: '2.0',
    id: 1,
    result: { content: [{ type: 'text', text: JSON.stringify(payload) }], isError: true },
  })

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('callTool error layers', () => {
  it('returns the parsed payload on success', async () => {
    mockFetch(() => toolSuccess({ name: 'db1', status: 'ready' }))
    await expect(callTool('create_database', { name: 'db1' })).resolves.toEqual({
      name: 'db1',
      status: 'ready',
    })
  })

  it('network failure → platform unreachable', async () => {
    mockFetch(() => Promise.reject(new TypeError('fetch failed')))
    await expect(callTool('capabilities')).rejects.toThrow(/unreachable/i)
  })

  it('HTTP 401 → not authorized', async () => {
    mockFetch(() => jsonResponse(401, { error: 'invalid authorization token' }))
    await expect(callTool('capabilities')).rejects.toThrow(/not authorized/i)
  })

  it('non-JSON body → readable message with status', async () => {
    mockFetch(() =>
      Promise.resolve({ status: 502, json: () => Promise.reject(new SyntaxError('bad')) }),
    )
    await expect(callTool('capabilities')).rejects.toThrow(/non-JSON.*502/i)
  })

  it('JSON-RPC protocol error envelope → protocol error', async () => {
    mockFetch(() =>
      jsonResponse(200, { jsonrpc: '2.0', id: 1, error: { code: -32601, message: 'unknown method' } }),
    )
    await expect(callTool('capabilities')).rejects.toThrow(/protocol error.*-32601.*unknown method/i)
  })

  it('missing result content → malformed tool response', async () => {
    mockFetch(() => jsonResponse(200, { jsonrpc: '2.0', id: 1, result: {} }))
    await expect(callTool('capabilities')).rejects.toThrow(/malformed/i)
  })

  it('unparseable tool content → readable message', async () => {
    mockFetch(() =>
      jsonResponse(200, {
        jsonrpc: '2.0',
        id: 1,
        result: { content: [{ type: 'text', text: 'not json' }], isError: false },
      }),
    )
    await expect(callTool('capabilities')).rejects.toThrow(/unparseable/i)
  })

  it('tool error → reason + suggested_action', async () => {
    mockFetch(() =>
      toolError({
        reason: 'database x has 2 live branch(es)',
        retriable: false,
        suggested_action: 'delete those branches first',
      }),
    )
    await expect(callTool('delete_database', { name: 'x' })).rejects.toThrow(
      /2 live branch.*delete those branches first/,
    )
  })
})

describe('token handling', () => {
  it('stores and returns the token', () => {
    setToken('  abc123  ')
    expect(token()).toBe('abc123')
  })

  it('captures ?token= from the URL and strips it', () => {
    history.replaceState(null, '', '/?token=fromurl')
    expect(token()).toBe('fromurl')
    expect(window.location.search).not.toContain('token')
    expect(token()).toBe('fromurl') // persisted
  })
})
