# Share data between two local projects

Install Supabricks from the locally served installer and run `supabricks console`.
The installed catalog starts automatically with its private Java runtime. Open the
console URL printed by the command; no catalog server or credentials need to be
configured manually.

1. Click **+ New project**, name it `catalog-producer`, and create it.
2. In **Database workspace**, choose **Import file**. Select the installed
   `examples/console/sales.csv`, name the new table `sales`, review the columns,
   and choose **Create table and import**. Expect two committed rows.
3. Choose **Publish imported data** to open **Data**. Click **Refresh analytical
   snapshot**, then **Review publication**, and **Publish reviewed snapshot**.
   The publication is an immutable revision; live database writes do not change it.
4. Create a second project named `catalog-consumer`. Open **Data**, choose
   **Add existing dataset**, and select the producer's revision. Name the dataset
   `sales`, then **Review dataset binding** and **Apply reviewed dataset plan**.
5. Choose **Query dataset.sales.sales**, open the analytical session, and run:

   ```sql
   SELECT sum(amount) AS total FROM dataset_sales.public.sales
   ```

   Expect `30`. The query runs through Sail's Spark Connect interface over the
   catalog revision explicitly bound to this project.
6. Back in **Data**, choose **Notebook dataset.sales.sales**, then **Open prepared
   dataset notebook**. Start the kernel and run:

   ```python
   spark.table("dataset_sales.public.sales").show()
   ```

   Stop the kernel when finished. Opening a notebook does not execute its cells.
7. Change the producer's PostgreSQL data and publish a new snapshot. The consumer
   keeps its selected revision. Use **Review update dataset.sales** to inspect
   changes and explicitly apply them. Existing readers keep their original
   revision; new sessions use the newly selected revision.

Projects own publications and dataset bindings. Removing a binding does not
remove the producer's data. Active bindings and readers protect the referenced
snapshot from garbage collection. Project packages carry a logical dataset
requirement; the destination must explicitly bind it to an available publication.

`supabricks down` followed by `supabricks console` reopens the local product
without downloading Java or contacting a hosted catalog. For stopped backup and
moved-root restore, follow the installed recovery handbook. This local-owner
profile trusts the operating-system account; shared-user IAM and governed access
remain a separate workstream.
