// Product qualification: interact with the packaged editor, never inject an execution client.
import { expect } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

export async function qualifyNotebooks(page, { project, cli, checks }) {
  await page.getByRole("button", { name: "Notebooks", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Start kernel", exact: true }),
  ).toBeVisible();
  await page.getByLabel("Notebook branch").selectOption({ label: "main" });
  const code = page.locator(".jp-CodeCell .cm-content").first();
  await code.fill(
    "total = spark.sql('SELECT sum(amount) AS total FROM public.orders').first().total\nprint('TOTAL', total)",
  );
  await page.getByRole("button", { name: "Start kernel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 120000 });
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "TOTAL 19.75",
    { timeout: 60000 },
  );
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  checks.push("product_editor_real_Sail_query");
  await cli(
    "sql",
    "--branch",
    "main",
    "--write",
    "--sql",
    "INSERT INTO public.orders VALUES(3,1.00)",
  );
  await page
    .getByRole("button", { name: "Refresh snapshot", exact: true })
    .click();
  await expect(page.getByText(/^Snapshot refresh: published/)).toBeVisible({
    timeout: 120000,
  });
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "TOTAL 19.75",
    { timeout: 30000 },
  );
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  page.once("dialog", (d) => d.accept());
  await page
    .getByRole("button", { name: "Restart kernel", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 120000 });
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "TOTAL 19.75",
    { timeout: 30000 },
  );
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  page.once("dialog", (d) => d.accept());
  await page
    .getByRole("button", { name: "Start on latest snapshot", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 120000 });
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "TOTAL 20.75",
    { timeout: 30000 },
  );
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  checks.push("product_refresh_isolation_restart_pinned_and_explicit_latest");
  await code.fill(
    "import time\nprint('FIRST', flush=True)\ntime.sleep(0.1)\nprint('SECOND', flush=True)\nvalue_for_next_cell=7",
  );
  await page
    .getByRole("button", { name: "Add Python cell", exact: true })
    .click();
  await page
    .locator(".jp-CodeCell .cm-content")
    .nth(1)
    .fill("print('NEXT', value_for_next_cell + 1)");
  await page.getByRole("button", { name: "Run all", exact: true }).click();
  await expect(page.locator(".jp-CodeCell").nth(1)).toContainText("NEXT 8", {
    timeout: 30000,
  });
  await expect(page.locator(".jp-OutputArea").first()).toContainText("FIRST");
  await expect(page.locator(".jp-OutputArea").first()).toContainText("SECOND");
  await expect(
    page.getByRole("button", { name: "Run all", exact: true }),
  ).toBeEnabled();
  checks.push("product_run_all_serial_and_stream_chunks_append");
  page.once("dialog", (d) => d.accept("orders.ipynb"));
  await page
    .getByRole("button", { name: "Save notebook", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "orders.ipynb", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Saved locally", { exact: true })).toBeVisible();
  const saved = JSON.parse(
    await readFile(path.join(project, "notebooks/orders.ipynb"), "utf8"),
  );
  expect(
    saved.cells.every(
      (c) =>
        c.id &&
        (c.cell_type !== "code" ||
          (Array.isArray(c.outputs) &&
            c.outputs.length === 0 &&
            c.execution_count === null)),
    ),
  ).toBe(true);
  await code.fill("print('SECOND SAVE')");
  let releaseSave;
  const saveGate = new Promise((resolve) => {
    releaseSave = resolve;
  });
  const delaySave = async (route) => {
    if (route.request().postDataJSON()?.action === "save") await saveGate;
    await route.continue();
  };
  await page.route("**/api/notebooks/contents", delaySave);
  await page
    .getByRole("button", { name: "Save notebook", exact: true })
    .click();
  await expect(page.locator(".supabricks-notebook")).toHaveJSProperty(
    "inert",
    true,
  );
  await page.keyboard.insertText("EDIT DURING SAVE");
  await expect(code).toContainText("print('SECOND SAVE')");
  await expect(code).not.toContainText("EDIT DURING SAVE");
  releaseSave();
  await expect(page.getByText("Saved locally", { exact: true })).toBeVisible();
  await page.unroute("**/api/notebooks/contents", delaySave);
  expect(
    await readFile(path.join(project, "notebooks/orders.ipynb"), "utf8"),
  ).toContain("SECOND SAVE");
  checks.push("product_standard_document_and_repeated_conditional_save");
  page.once("dialog", (d) => d.accept("renamed.ipynb"));
  await page
    .getByRole("button", { name: "Rename notebook", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "renamed.ipynb", exact: true }),
  ).toBeVisible();
  page.once("dialog", (d) => d.accept("orders.ipynb"));
  await page
    .getByRole("button", { name: "Rename notebook", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "orders.ipynb", exact: true }),
  ).toBeVisible();
  checks.push("product_conditional_rename");
  const disk = await readFile(
    path.join(project, "notebooks/orders.ipynb"),
    "utf8",
  );
  await code.fill("print('UNSAVED MUST SURVIVE')");
  await writeFile(path.join(project, "notebooks/orders.ipynb"), disk + "\n");
  await page
    .getByRole("button", { name: "Save notebook", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("changed on disk");
  await expect(code).toContainText("UNSAVED MUST SURVIVE");
  page.once("dialog", (d) => d.dismiss());
  await page
    .getByRole("button", { name: "＋ New notebook", exact: true })
    .click();
  await expect(code).toContainText("UNSAVED MUST SURVIVE");
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download notebook", exact: true })
    .click();
  expect(await (await downloadPromise).failure()).toBeNull();
  checks.push("product_conflict_preserves_dirty_edits_and_download");
  page.once("dialog", (d) => d.accept("orders.ipynb"));
  await page.getByRole("button", { name: "Save a copy", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("already exists");
  expect(
    await readFile(path.join(project, "notebooks/orders.ipynb"), "utf8"),
  ).toBe(disk + "\n");
  await page.getByRole("button", { name: "Stop kernel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Start kernel", exact: true }),
  ).toBeVisible({ timeout: 30000 });
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "orders.ipynb", exact: true }).click();
  await expect(code).toContainText("SECOND SAVE");
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Start kernel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 120000 });
  await code.fill("print('value_for_next_cell' in globals())");
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText("False", {
    timeout: 30000,
  });
  checks.push("product_reopen_and_explicit_restart_without_replay");
  await code.fill("import time\ntime.sleep(60)");
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Interrupt", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 20000 });
  checks.push("product_interrupt_observed");
  await code.fill(
    "from IPython.display import display, HTML, Image\nimport base64\ndisplay(HTML('<b>SAFE_TABLE</b><script>window.notebookAttack=1</script><img src=\"/api/forbidden-output\" onerror=\"window.notebookAttack=1\">'))\ndisplay(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4WQAAAAASUVORK5CYII='),format='png'))",
  );
  const outputRequests = [];
  const observe = (request) => {
    if (request.url().includes("/api/forbidden-output"))
      outputRequests.push(request.url());
  };
  page.on("request", observe);
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "SAFE_TABLE",
    { timeout: 30000 },
  );
  await expect(
    page
      .locator(".jp-OutputArea")
      .first()
      .locator('img[src^="data:image/png"]'),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  expect(await page.evaluate(() => window.notebookAttack)).toBeUndefined();
  expect(outputRequests).toEqual([]);
  page.off("request", observe);
  await page.getByLabel("Include outputs when saving or downloading").check();
  page.once("dialog", (d) => d.accept("outputs.ipynb"));
  await page.getByRole("button", { name: "Save a copy", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "outputs.ipynb", exact: true }),
  ).toBeVisible();
  const withOutputs = JSON.parse(
    await readFile(path.join(project, "notebooks/outputs.ipynb"), "utf8"),
  );
  expect(withOutputs.cells[0].outputs.some((o) => o.data?.["image/png"])).toBe(
    true,
  );
  expect(
    withOutputs.cells[0].metadata.supabricks_outputs.epoch_id,
  ).toBeTruthy();
  checks.push("product_opt_in_output_persistence_with_cell_provenance");
  await page
    .getByRole("button", { name: "Add Markdown cell", exact: true })
    .click();
  await page
    .locator(".jp-MarkdownCell .cm-content")
    .last()
    .fill("# Notebook markdown");
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Notebook markdown", exact: true }),
  ).toBeVisible();
  checks.push("product_markdown_html_sanitization_and_png_output");
  await code.fill(
    "import time\nprint('BEFORE DISCONNECT', flush=True)\ntime.sleep(3)\ndisconnect_count = globals().get('disconnect_count', 0) + 1",
  );
  await code.click();
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "BEFORE DISCONNECT",
    { timeout: 30000 },
  );
  // Reload during execution closes the connection. Reopening and attaching must not replay it.
  let submissions = 0;
  page.on("websocket", (socket) => socket.on("framesent", () => submissions++));
  page.once("dialog", (d) => d.accept());
  await page.reload();
  await page.getByRole("button", { name: "Notebooks", exact: true }).click();
  await page.getByRole("button", { name: "orders.ipynb", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeDisabled();
  expect(submissions).toBe(0);
  await page.getByRole("button", { name: /^Attach main/ }).click();
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  expect(submissions).toBe(0);
  await expect(page.getByRole("status")).toContainText("Kernel: ready", {
    timeout: 30000,
  });
  await code.fill(
    "print('RECONNECTED', 'value_for_next_cell' in globals(), disconnect_count)",
  );
  await page.getByRole("button", { name: "Run cell", exact: true }).click();
  await expect(page.locator(".jp-OutputArea").first()).toContainText(
    "RECONNECTED False 1",
    { timeout: 30000 },
  );
  await expect(
    page.getByRole("button", { name: "Run cell", exact: true }),
  ).toBeEnabled();
  checks.push(
    "product_reload_during_execution_and_explicit_attach_without_replay",
  );
  await page.getByRole("button", { name: "Stop kernel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Start kernel", exact: true }),
  ).toBeVisible({ timeout: 30000 });
}
