# Import a local CSV or TSV

For JSON, JSONL and Parquet, see [file ingestion](file-ingestion.md).

Run `supabricks console` from your application project, open **Database workspace**
and choose **Import file**. The full alpha.8 preview bundles everything needed.

1. Choose a CSV/TSV file, or drop it into the picker. Keyboard selection uses the
   same upload path. Files must be nonempty and no larger than 100 MiB.
2. Review the sample and parser options. **Inspect again** applies delimiter,
   header and null-string changes. A null-string value is a JSON array, such as
   `["NULL"]`; `[]` preserves unquoted empty strings. Quoted values remain strings.
3. Edit column names and types. Text is the conservative default and preserves
   leading zeros. Decimal precision/scale and nullability are explicit choices.
   Samples show at most 100 rows; import validates the entire file.
4. Choose a running branch, existing schema and **new** table name. Review the
   project, branch revision and destination, approve the columns, then select
   **Create table and import**.
5. Watch copied and committed rows separately. Only confirmed committed rows
   indicate success. The new table opens in a SQL tab on its destination branch.

Reloading or closing the browser does not retry an accepted import. Reopen the
console and inspect **Recent imports**. **Cancel import** fences the worker and
checks the database outcome; it may take time to resolve an in-flight commit.
A failed import can offer **Retry retained source**. Retry is explicit and retains
the job ID; it requires the source and original branch revision to remain valid.

**Dispose source** removes an unused or failed staged copy. Failed sources remain
available for up to 24 hours, while completed/cancelled imports release their
copies automatically. The original device file is unchanged. An interrupted
upload must be selected again; abandoned receiving slots are reclaimed after
30 idle seconds. A runtime restart or expired console session can require a fresh
upload for work that was not yet accepted.

If a submission response is lost, inspect recent jobs before starting another
import. If the destination already exists, choose another table name. If staging
space is exhausted, dispose unused sources and free disk space. The uploader
preserves a 64 MiB free-space reserve in addition to its staging limits.

Try the [four-row orders walkthrough](../../examples/console/README.md) to import,
branch, mutate the child and compare the unchanged parent. JSON/JSONL/Parquet
support is planned for I03.
