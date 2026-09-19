# PK01 source inspection example

This is a format-2 **inspection-only** project. It declares a PostgreSQL database,
a SQL query, a notebook and its environment inputs. It cannot deploy or execute
until the later project packaging slices provide deployment binding.

```sh
supabricks project validate --project /path/to/examples/projects/sales
supabricks project inspect --project /path/to/examples/projects/sales --target staging
```

The sample environment is a minimal, structurally valid uv declaration/lock pair
with no dependencies. It demonstrates input hashing; it is **not** a prepared or
kernel-compatible notebook environment. No package resolver or notebook code runs
while inspecting it. The existing runtime demo remains in `examples/console`.
