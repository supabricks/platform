# A database you can experiment with

This synthetic fixture is Apache-2.0, like the repository. All names and orders
are fictional. No download or external data service is needed.

1. Install the current preview using the signed localhost installer described in
   `install/native/README.md`. Run `supabricks init demo`, then
   `supabricks database create main --wait` and `supabricks console`.
2. Open **Database workspace → Import file** and choose `orders.csv`.
   Keep `order_id` as text to preserve its leading zeros. Choose decimal(18,2)
   for `amount` and boolean for `paid`. Leave the other columns as text.
3. Review the sample, choose `main → public.orders`, approve the mapping and
   select **Create table and import**. The completed job shows **4 committed
   rows** and opens the table. Refresh the page: the same job remains visible.
4. In branch management, create `experiment` as a child of `main`. Select it,
   open a SQL tab, explicitly enable writes, and run:

   ```sql
   UPDATE public.orders SET amount = amount * 0.9 WHERE paid
   ```

5. Run `SELECT * FROM public.orders ORDER BY order_id` on `experiment`, then
   open a separate SQL tab on `main` and run the same query. The parent keeps
   the original amounts. The CSV on your device is unchanged throughout.
6. Delete the child when finished. Importing again requires a new destination
   table; existing tables are never overwritten by the importer.
