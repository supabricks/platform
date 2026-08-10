// The browser is an MCP client (RFC 013): every read and mutation is
// tools/call against the same façade agents use.

const KEY = 'sspc-token'

export function token(): string {
  const url = new URL(window.location.href)
  const t = url.searchParams.get('token')
  if (t) {
    localStorage.setItem(KEY, t)
    url.searchParams.delete('token')
    history.replaceState(null, '', url.toString())
  }
  return localStorage.getItem(KEY) ?? ''
}

let seq = 1

export async function callTool<T = unknown>(name: string, args: object = {}): Promise<T> {
  const res = await fetch('/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token()}`,
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: seq++,
      method: 'tools/call',
      params: { name, arguments: args },
    }),
  })
  if (res.status === 401) throw new Error('Not authorized — reopen the page with ?token=…')
  const body = await res.json()
  const payload = JSON.parse(body.result.content[0].text)
  if (body.result.isError) {
    throw new Error(payload.suggested_action ? `${payload.reason}. ${payload.suggested_action}` : payload.reason)
  }
  return payload as T
}

export interface EstateRow {
  name: string
  kind: 'cell-backed' | 'enrolled'
  phase?: string | null
  node_port?: number | null
  tenant_id?: string | null
  timeline_id?: string | null
  created_at?: string | null
  ttl_seconds?: number | null
  suspend_after_seconds?: number | null
  last_activity?: string | null
  suspended_at?: string | null
  server_version?: string | null
  database_count?: number | null
  total_size?: string | null
  message?: string | null
}

export interface BranchRow extends EstateRow {
  database: string
}

export interface EventRow {
  time?: string | null
  reason?: string | null
  kind?: string | null
  name?: string | null
  message?: string | null
}
