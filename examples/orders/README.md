# Orders on Supabricks

This tiny Python HTTP API demonstrates ordinary PostgreSQL application code.
It uses the branch's stable URI, parameterized writes and a connection per
request. Python 3.11+ and the P06 native Supabricks binary are required.

Copy this directory into a new project. From that directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
supabricks init orders
supabricks up --bundle /absolute/engine --helpers /absolute/helpers
supabricks database create main --key first-main --wait
supabricks branch use main
supabricks sql --branch main --write --file migrations/001-orders.sql
export DATABASE_URL="$(supabricks connect --uri)"
.venv/bin/python app.py
```

From another terminal:

```sh
curl -s http://127.0.0.1:8080/orders
curl -s http://127.0.0.1:8080/orders -H 'Content-Type: application/json' \
  -d '{"customer":"Ada","total_cents":1299}'
```

Try a migration without changing the parent:

```sh
supabricks branch create status-preview --from main --wait
supabricks sql --branch status-preview --write --file migrations/002-status.sql
supabricks sql --branch status-preview --write --sql "UPDATE orders SET status='paid'"
supabricks sql --branch status-preview --sql 'SELECT * FROM orders'
supabricks catalog --branch main
supabricks branch delete status-preview --wait
supabricks down
supabricks up
```

The parent catalog has no `status` column and its rows survive restart. Stop and
restart the HTTP process with a different branch URI to preview that branch.
`002-status.sql` is a one-time migration: duplicate application correctly fails.
Use a migration framework for a larger application. The API is a local example,
with a 100-row list and 4 KiB POST body; it has no application authentication.

The database requires no model credentials. To work with a coding agent, see
[agent setup](../../agents/README.md) and [the smoke task](../../agents/smoke-task.md).

## Query an analytical snapshot

With the [locked analytical worker configured](../../docs/architecture/a01-frozen-exports.md#developer-setup),
query the application's tables through Sail. First access creates a frozen
snapshot while the application continues using Postgres:

```sh
supabricks analytics sql --branch main --sql \
  'SELECT customer, sum(total_cents) AS total_cents FROM public.orders GROUP BY customer'
supabricks spark shell --branch main
```

Inside the shell, `spark.table("public.orders")` is an ordinary DataFrame and
`epoch` describes its frozen source. After more application writes, publish a
new snapshot with `supabricks analytics refresh --branch main --wait`. Existing
shells keep their original snapshot; new sessions select the refreshed one.
See [analytical sessions](../../docs/architecture/a03-analytical-sessions.md) for
historical epochs, limits and cancellation. Worker packaging into the curl
installer remains release work.
