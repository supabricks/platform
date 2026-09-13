# Query PostgreSQL data with Spark SQL

Open `supabricks console`, then **Database workspace**. Import a CSV, TSV, JSON or
Parquet file into a new table with an approved mapping. In **PostgreSQL** mode,
query the live branch normally.

Switch to **Analytics**, select the source branch, and choose **Publish fresh
snapshot**. Wait for `published`. Inspect the source observation time, epoch and
table inventory, then choose **Open latest session**. Wait for `ready` and run:

```sql
SELECT * FROM public.sales ORDER BY id
```

Use Spark SQL syntax here. Original PostgreSQL schema/table names are available;
quote identifiers with backticks when needed. These tables are read only. The
`_supabricks.epoch` view identifies the session's immutable source.

To demonstrate snapshot isolation, import `id,amount` values `1,10` and `2,20`,
then run `SELECT sum(amount) AS total FROM public.sales` in Analytics. It returns
30. Choose **Keep result for comparison**. In PostgreSQL mode, enable writes and
run `INSERT INTO public.sales VALUES (3,40)`. The original analytical session
still returns 30, including after **Publish fresh snapshot** completes. Choose
**Open latest session** and run the same Spark SQL: it returns 70. The retained
result and each session show their separate epochs. To compare a child branch,
select it as the snapshot source, publish it, and explicitly open its session.

Publishing and rebinding are separate actions. Snapshot age does not prove whether
the live source changed. JSONB and other unsupported source features currently
block publication; map scalar values to supported PostgreSQL columns where
appropriate. A failed or cancelled refresh preserves the previous publication.

Two analytical session slots are shared with notebooks and CLI. Close an unused
session before opening another. **Cancel analytical session** stops its query and
the whole worker. Row and byte limits may truncate displayed results; use ORDER BY
for stable comparisons. PostgreSQL collation semantics are not reproduced by Sail.

Reloading the console reconnects to its owned sessions; select one to inspect its
latest result. SQL is never replayed automatically after connection trouble.
Sessions expire after 15 minutes, and after two minutes without browser heartbeats.
After a runtime restart, open a new session explicitly. Historical epochs remain.

For detailed errors, use the displayed identity:

```bash
supabricks analytics session SESSION_ID
supabricks analytics status REFRESH_ID
```
