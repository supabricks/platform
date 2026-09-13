# Try an isolated notebook dependency

In an initialized project, start Supabricks and open its console:

```sh
supabricks up
supabricks console
```

Create/select a database branch. Open **Notebooks** and explicitly start a kernel.
In the environment panel, disable **Offline packages only**, add
`humanize==4.13.0`, wait, and choose **Use prepared environment**. Run:

```python
import humanize
print(humanize.__version__)
print(humanize.intcomma(1234567))
spark.sql("SELECT 42 AS answer").show()
```

Save the notebook. Commit its source and both environment declaration files.
Export a bundle using the [environment guide](../../docs/handbook/notebook-environments.md).
Copy the project source and bundle to a new directory, use `--project` with that
directory, explicitly import the bundle and select the newly prepared environment
in the console. Reopening the saved notebook does not run its cells. Execute them
to check the imported package and current selected analytical snapshot.

For a different machine, use the same supported OS target and compatible release.
Database data and saved analytical epochs require a separate platform backup;
copying notebook source and packages alone does not copy the database.
