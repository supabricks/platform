import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, DataTable, Modal, NumberInput, Table, TableBody, TableCell, TableContainer,
  TableExpandHeader, TableExpandRow, TableExpandedRow, TableHead, TableHeader, TableRow,
  Tag, TextInput, Theme, ToastNotification,
} from '@carbon/react'
import { Add, Link as LinkIcon, TrashCan } from '@carbon/icons-react'
import { callTool, type BranchRow, type EstateRow, type EventRow } from './mcp'

const slug = (s: string) => s.toLowerCase().replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '').slice(0, 40)
import Rails from './Rails'

type Toast = { id: number; title: string; subtitle?: string; kind: 'success' | 'error' | 'info' }

function ttlRemaining(created?: string | null, ttl?: number | null): string {
  if (!created || !ttl) return '—'
  const end = new Date(created).getTime() + ttl * 1000
  const left = Math.max(0, Math.floor((end - Date.now()) / 1000))
  const m = Math.floor(left / 60), s = left % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function phaseTag(phase?: string | null) {
  const map: Record<string, { type: any; label: string }> = {
    Active: { type: 'green', label: '● Active' },
    Reachable: { type: 'green', label: '● Reachable' },
    Suspended: { type: 'cool-gray', label: '◌ Suspended — costing nothing' },
    Provisioning: { type: 'blue', label: '… Provisioning' },
    Unreachable: { type: 'red', label: '✕ Unreachable' },
    Failed: { type: 'red', label: '✕ Failed' },
  }
  const m = phase ? map[phase] : undefined
  return <Tag size="sm" type={m?.type ?? 'gray'}>{m?.label ?? phase ?? '—'}</Tag>
}

export default function App() {
  const [dbs, setDbs] = useState<EstateRow[]>([])
  const [branches, setBranches] = useState<BranchRow[]>([])
  const [events, setEvents] = useState<EventRow[]>([])
  const [toasts, setToasts] = useState<Toast[]>([])
  const [modal, setModal] = useState<'db' | 'branch' | 'enroll' | null>(null)
  const [form, setForm] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [, forceTick] = useState(0)

  const toast = useCallback((t: Omit<Toast, 'id'>) => {
    const id = Date.now() + Math.random()
    setToasts((p) => [...p.slice(-3), { ...t, id }])
    setTimeout(() => setToasts((p) => p.filter((x) => x.id !== id)), 6000)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [d, b, e] = await Promise.all([
        callTool<EstateRow[]>('list_databases'),
        callTool<BranchRow[]>('list_branches'),
        callTool<EventRow[]>('get_events'),
      ])
      setDbs(d); setBranches(b); setEvents(e)
    } catch {
      /* keep last good frame; ticker shows staleness implicitly */
    }
  }, [])

  useEffect(() => {
    refresh()
    const poll = setInterval(refresh, 2000)
    const tick = setInterval(() => forceTick((n) => n + 1), 1000)
    return () => { clearInterval(poll); clearInterval(tick) }
  }, [refresh])

  const cell = dbs.filter((d) => d.kind === 'cell-backed')
  const enrolled = dbs.filter((d) => d.kind === 'enrolled')
  const endpoints = cell.length + branches.length
  const running = [...cell, ...branches].filter((r) => r.phase === 'Active').length
  const governed = enrolled.reduce((acc, e) => {
    const m = /([\d.]+)\s*(kB|MB|GB|TB)/.exec(e.total_size ?? '')
    if (!m) return acc
    const mult = { kB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12 }[m[2] as 'kB' | 'MB' | 'GB' | 'TB']
    return acc + parseFloat(m[1]) * mult
  }, 0)
  const governedStr = governed >= 1e9 ? `${(governed / 1e9).toFixed(1)} GB` : `${Math.round(governed / 1e6)} MB`

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true)
    try { await fn(); toast({ kind: 'success', title: ok }); setModal(null); setForm({}); refresh() }
    catch (e) { toast({ kind: 'error', title: 'That didn’t work', subtitle: String(e) }) }
    finally { setBusy(false) }
  }

  const connect = async (name: string) => {
    try {
      const r = await callTool<{ connection_uri: string; woke_from_suspend?: boolean; wake_seconds?: string }>(
        'get_connection', { name })
      await navigator.clipboard.writeText(r.connection_uri)
      toast({
        kind: 'success',
        title: r.woke_from_suspend ? `Woke in ${r.wake_seconds} s` : 'Connection copied',
        subtitle: r.woke_from_suspend ? 'Connection copied' : undefined,
      })
    } catch (e) { toast({ kind: 'error', title: 'Couldn’t connect', subtitle: String(e) }) }
  }

  const headers = [
    { key: 'name', header: 'Name' }, { key: 'kind', header: 'Kind' },
    { key: 'phase', header: 'Phase' }, { key: 'detail', header: 'Detail' },
    { key: 'ttl', header: 'TTL' }, { key: 'actions', header: '' },
  ]
  const rows = useMemo(() => [...cell, ...enrolled].map((r) => ({ id: r.name, ...r })), [dbs])

  return (
    <Theme theme="g90">
      <div style={{ minHeight: '100vh', background: 'var(--cds-background)' }}>
        <header className="estate-header">
          <span>sspc <span style={{ color: 'var(--cds-text-secondary)' }}>· estate</span></span>
          <div>
            <Button kind="ghost" size="sm" renderIcon={Add} onClick={() => setModal('enroll')}>Enroll existing</Button>
            <Button kind="primary" size="sm" renderIcon={Add} onClick={() => setModal('db')}>New database</Button>
          </div>
        </header>

        <div className="tiles">
          {[
            [String(cell.length), 'databases'],
            [`${endpoints}:${running}`, 'endpoints : running pods'],
            [String(enrolled.length), 'enrolled — untouched'],
            [governedStr, 'governed data (enrolled)'],
          ].map(([n, l]) => (
            <div key={l} style={{ background: 'var(--cds-layer-01)', padding: '1rem' }}>
              <div className="tile-num">{n}</div>
              <div style={{ color: 'var(--cds-text-secondary)', fontSize: '0.75rem' }}>{l}</div>
            </div>
          ))}
        </div>

        <div className="estate-body">
          <DataTable rows={rows} headers={headers}>
            {({ rows: tr, headers: th, getHeaderProps, getRowProps, getTableProps }) => (
              <TableContainer>
                <Table {...getTableProps()} size="lg">
                  <TableHead>
                    <TableRow>
                      <TableExpandHeader />
                      {th.map((h) => (
                        <TableHeader {...getHeaderProps({ header: h })} key={h.key}>{h.header}</TableHeader>
                      ))}
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {tr.length === 0 && (
                      <TableRow><TableCell colSpan={7}>
                        No databases yet. Ask your agent for one — or create it here.
                      </TableCell></TableRow>
                    )}
                    {tr.map((row) => {
                      const r = rows.find((x) => x.id === row.id)!
                      const kids = branches.filter((b) => b.database === r.name)
                      return (
                        <>
                          <TableExpandRow {...getRowProps({ row })} key={r.name}>
                            <TableCell>{r.name}{kids.length > 0 && <span className="mono" style={{ color: 'var(--cds-text-secondary)' }}> +{kids.length}</span>}</TableCell>
                            <TableCell><Tag size="sm" type={r.kind === 'enrolled' ? 'purple' : 'teal'}>{r.kind}</Tag></TableCell>
                            <TableCell>{phaseTag(r.phase)}</TableCell>
                            <TableCell className="mono">
                              {r.kind === 'enrolled'
                                ? `PG ${r.server_version ?? '?'} · ${r.database_count ?? '?'} dbs · ${r.total_size ?? '?'}`
                                : r.node_port ? `:${r.node_port}` : ''}
                            </TableCell>
                            <TableCell className="mono">{ttlRemaining(r.created_at, r.ttl_seconds)}</TableCell>
                            <TableCell>
                              {r.kind === 'cell-backed' && (
                                <>
                                  <Button kind="ghost" size="sm" renderIcon={LinkIcon} onClick={() => connect(r.name)}>Connect</Button>
                                  <Button kind="ghost" size="sm" renderIcon={Add}
                                    onClick={() => { setForm({ database: r.name }); setModal('branch') }}>Branch</Button>
                                </>
                              )}
                              <Button kind="danger--ghost" size="sm" renderIcon={TrashCan} hasIconOnly iconDescription={r.kind === 'enrolled' ? 'Unenroll' : 'Delete'}
                                onClick={() => act(
                                  () => callTool(r.kind === 'enrolled' ? 'unenroll_database' : 'delete_database', { name: r.name }),
                                  r.kind === 'enrolled' ? `Unenrolled ${r.name}` : `Deleting ${r.name}`)} />
                            </TableCell>
                          </TableExpandRow>
                          <TableExpandedRow colSpan={7} key={`${r.name}-x`}>
                            {r.kind === 'enrolled'
                              ? <p style={{ padding: '0.5rem 0', color: 'var(--cds-text-secondary)' }}>
                                  Enrolled in place — inventoried and health-checked over SQL. Nothing was migrated or modified.
                                  {r.message ? ` Last error: ${r.message}` : ''}
                                </p>
                              : <Rails db={r} branches={kids} />}
                          </TableExpandedRow>
                        </>
                      )
                    })}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </DataTable>

          {branches.filter((b) => b.ttl_seconds).length > 0 && (
            <p className="mono" style={{ marginTop: '0.75rem', color: 'var(--cds-text-secondary)' }}>
              {branches.filter((b) => b.ttl_seconds).map((b) => `${b.name} reaps in ${ttlRemaining(b.created_at, b.ttl_seconds)}`).join(' · ')}
            </p>
          )}
        </div>

        <div className="ticker mono">
          {events.length === 0 ? 'no activity yet' : events.slice(0, 6).map((e) =>
            `${e.time ? new Date(e.time).toLocaleTimeString() : ''} ${e.message ?? e.reason ?? ''}`).join('  ·  ')}
        </div>

        <div className="toasts">
          {toasts.map((t) => (
            <ToastNotification key={t.id} kind={t.kind} title={t.title} subtitle={t.subtitle} lowContrast hideCloseButton timeout={0} />
          ))}
        </div>

        <Modal open={modal === 'db'} modalHeading="New database" primaryButtonText="Create" secondaryButtonText="Cancel"
          primaryButtonDisabled={busy || !form.name} onRequestClose={() => setModal(null)}
          onRequestSubmit={() => act(() => callTool('create_database', {
            name: form.name, ...(form.ttl ? { ttl_seconds: Number(form.ttl) } : {}),
            ...(form.suspend ? { suspend_after_seconds: Number(form.suspend) } : {}),
          }), `Created ${form.name}`)}>
          <TextInput id="db-name" labelText="Name" helperText="Lowercase letters, digits, hyphens" value={form.name ?? ''} onChange={(e) => setForm({ ...form, name: slug(e.target.value) })} />
          <NumberInput id="db-ttl" label="TTL seconds (optional — it will clean itself up)" value={form.ttl ?? ''} hideSteppers allowEmpty
            onChange={(_, { value }) => setForm({ ...form, ttl: String(value ?? '') })} />
          <NumberInput id="db-suspend" label="Suspend after idle seconds (default 300)" value={form.suspend ?? ''} hideSteppers allowEmpty
            onChange={(_, { value }) => setForm({ ...form, suspend: String(value ?? '') })} />
        </Modal>

        <Modal open={modal === 'branch'} modalHeading={`New branch of ${form.database ?? ''}`} primaryButtonText="Branch" secondaryButtonText="Cancel"
          primaryButtonDisabled={busy || !form.name} onRequestClose={() => setModal(null)}
          onRequestSubmit={() => act(() => callTool('create_branch', {
            name: form.name, database: form.database, ...(form.ttl ? { ttl_seconds: Number(form.ttl) } : {}),
          }), `Branched ${form.database} → ${form.name}`)}>
          <TextInput id="br-name" labelText="Branch name" helperText="Lowercase letters, digits, hyphens" value={form.name ?? ''} onChange={(e) => setForm({ ...form, name: slug(e.target.value) })} />
          <NumberInput id="br-ttl" label="TTL seconds (optional)" value={form.ttl ?? ''} hideSteppers allowEmpty
            onChange={(_, { value }) => setForm({ ...form, ttl: String(value ?? '') })} />
        </Modal>

        <Modal open={modal === 'enroll'} modalHeading="Enroll an existing Postgres" primaryButtonText="Enroll" secondaryButtonText="Cancel"
          primaryButtonDisabled={busy || !form.name || !form.uri} onRequestClose={() => setModal(null)}
          onRequestSubmit={() => act(() => callTool('enroll_database', { name: form.name, connection_uri: form.uri }), `Enrolled ${form.name}`)}>
          <TextInput id="en-name" labelText="Name (how it appears in the estate)" helperText="Lowercase letters, digits, hyphens" value={form.name ?? ''}
            onChange={(e) => setForm({ ...form, name: slug(e.target.value) })} />
          <TextInput id="en-uri" labelText="Connection string" helperText="A read-only role is enough. Nothing is migrated or modified."
            placeholder="postgresql://user:pass@host:5432/db" value={form.uri ?? ''}
            onChange={(e) => setForm({ ...form, uri: e.target.value })} />
        </Modal>
      </div>
    </Theme>
  )
}
