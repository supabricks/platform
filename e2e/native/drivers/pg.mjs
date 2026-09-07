import pg from 'pg';
import { from, to } from 'pg-copy-streams';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import assert from 'node:assert/strict';

// The parent writes private connection options to stdin, never argv or logs.
let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
const client = new pg.Client(config);
try {
  await client.connect();
  await client.query('CREATE TABLE node_driver(id int PRIMARY KEY, name text)');
  await client.query('BEGIN');
  await client.query('INSERT INTO node_driver VALUES ($1,$2)', [1, 'committed']);
  await client.query('COMMIT');
  await client.query('BEGIN');
  await client.query('INSERT INTO node_driver VALUES (2,\'rolled back\')');
  await client.query('ROLLBACK');
  const prepared = { name: 'by-id', text: 'SELECT name FROM node_driver WHERE id=$1', values: [1] };
  assert.equal((await client.query(prepared)).rows[0].name, 'committed');
  assert.equal((await client.query(prepared)).rows[0].name, 'committed');
  await pipeline(Readable.from(['3,copied\n4,streamed\n']), client.query(from('COPY node_driver FROM STDIN CSV')));
  let copied = '';
  for await (const chunk of client.query(to('COPY (SELECT * FROM node_driver ORDER BY id) TO STDOUT CSV'))) copied += chunk;
  assert.equal(copied, '1,committed\n3,copied\n4,streamed\n');
  const cancel = new pg.Client(config);
  const query = new pg.Query('SELECT pg_sleep(30)');
  const cancelled = new Promise((resolve, reject) => {
    query.on('error', e => e.code === '57014' ? resolve() : reject(e));
    query.on('end', () => reject(new Error('query was not cancelled')));
  });
  client.query(query);
  const timer = setTimeout(() => cancel.cancel(client, query), 300);
  try { await cancelled; } finally { clearTimeout(timer); await cancel.end(); }
  console.log('PASS: Node pg transactions, prepared statements streaming COPY and cancellation');
} finally {
  await client.end();
}
