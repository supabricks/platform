// The signature visual (RFC 013): a database as a horizontal timeline rail,
// branches forking downward at tick marks. Rail heads carry the live phase.
import type { BranchRow, EstateRow } from './mcp'

// Carbon categorical data-viz palette — color encodes lineage, nothing else.
const PALETTE = ['#8a3ffc', '#33b1ff', '#007d79', '#ff7eb6', '#fa4d56', '#6fdc8c', '#d2a106', '#ba4e00']

export function familyColor(name: string): string {
  let h = 0
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 997
  return PALETTE[h % PALETTE.length]
}

function Head({ x, y, phase, color }: { x: number; y: number; phase?: string | null; color: string }) {
  if (phase === 'Suspended')
    return (
      <g>
        <line x1={x - 14} y1={y} x2={x} y2={y} stroke={color} strokeWidth={2} strokeDasharray="3 3" opacity={0.45} />
        <circle cx={x} cy={y} r={4} fill="none" stroke={color} strokeWidth={1.5} opacity={0.45} />
      </g>
    )
  return <circle className="pulse" cx={x} cy={y} r={4.5} fill={color} />
}

function ttlLabel(b: BranchRow): string {
  if (!b.ttl_seconds || !b.created_at) return ''
  const left = Math.max(0, Math.floor((new Date(b.created_at).getTime() + b.ttl_seconds * 1000 - Date.now()) / 1000))
  return ` · reaps in ${Math.floor(left / 60)}m${String(left % 60).padStart(2, '0')}s`
}

export default function Rails({ db, branches }: { db: EstateRow; branches: BranchRow[] }) {
  const color = familyColor(db.name)
  const W = 640
  const rowH = 34
  const H = rowH * (branches.length + 1) + 10
  const forkX = 200

  return (
    <div className="rails">
      <svg width={W} height={H} role="img" aria-label={`Timeline of ${db.name} and ${branches.length} branches`}>
        {/* parent rail */}
        <circle cx={16} cy={20} r={3} fill={color} />
        <line x1={16} y1={20} x2={W - 40} y2={20} stroke={color} strokeWidth={2} opacity={db.phase === 'Suspended' ? 0.45 : 0.9} />
        <Head x={W - 40} y={20} phase={db.phase} color={color} />
        <text x={W - 28} y={24} fill="var(--cds-text-primary)" fontSize={12} fontFamily="system-ui, sans-serif">
          {db.name}
        </text>
        {branches.map((b, i) => {
          const y = rowH * (i + 1) + 20
          const bx = forkX + i * 36
          return (
            <g key={b.name}>
              {/* fork curve from the branch point */}
              <path d={`M ${bx} 20 Q ${bx} ${y} ${bx + 24} ${y}`} fill="none" stroke={color} strokeWidth={1.5} opacity={0.55} />
              <line x1={bx + 24} y1={y} x2={W - 40} y2={y} stroke={color} strokeWidth={2}
                strokeDasharray={b.phase === 'Suspended' ? '4 4' : undefined}
                opacity={b.phase === 'Suspended' ? 0.4 : 0.75} />
              <text x={bx - 4} y={y + 16} fill="var(--cds-text-secondary)" fontSize={10} fontFamily="ui-monospace, monospace" textAnchor="end">
                {b.timeline_id ? b.timeline_id.slice(0, 8) : ''}
              </text>
              <Head x={W - 40} y={y} phase={b.phase} color={color} />
              <text x={W - 28} y={y + 4} fill="var(--cds-text-primary)" fontSize={12} fontFamily="system-ui, sans-serif">
                {b.name}{ttlLabel(b) && <tspan fill="var(--cds-text-secondary)" fontSize={10} fontFamily="ui-monospace, monospace">{ttlLabel(b)}</tspan>}
              </text>
            </g>
          )
        })}
      </svg>
      {branches.length === 0 && (
        <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.75rem' }}>
          No branches yet. Ask your agent for one — a full copy costs nothing.
        </p>
      )}
    </div>
  )
}
