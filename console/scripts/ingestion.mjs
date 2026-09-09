import { expect } from "@playwright/test";
import { readFile, writeFile, open } from "node:fs/promises";
import { join, dirname } from "node:path";
import { createConnection } from "node:net";
export async function qualifyIngestion({
  page,
  context,
  browser,
  origin,
  cli,
  checks,
  workspace,
  launch,
  screenshot,
}) {
  const record = (text) => {
    checks.push(text);
    console.log(JSON.stringify({ check: text }));
  };
  const session = await (
    await context.request.get(origin + "/api/session", {
      headers: { "X-Supabricks-Console": "1" },
    })
  ).json();
  const headers = {
    Origin: origin,
    "X-Supabricks-Console": "1",
    "X-Supabricks-CSRF": session.csrf,
  };
  async function raw(command, client = context.request, custom = headers) {
    return client.post(origin + "/api/workspace", {
      headers: custom,
      data: { action: "ingest", command },
    });
  }
  async function api(command) {
    const r = await raw(command);
    const v = await r.json();
    expect(r.ok(), JSON.stringify(v)).toBe(true);
    return v.value;
  }
  expect(
    (
      await raw({ action: "begin", name: "too-large.csv", bytes: 104857601 })
    ).status(),
  ).toBe(400);
  expect(
    (
      await raw({
        action: "begin",
        name: "x.csv",
        bytes: 10,
        path: "/etc/passwd",
      })
    ).status(),
  ).toBe(400);
  const slot = await api({
    action: "begin",
    name: "../../display-only.csv",
    bytes: 8,
  });
  const other = await browser.newContext();
  try {
    const ticket = await launch();
    const auth = await other.request.post(origin + "/api/session", {
      headers: { Origin: origin, "X-Supabricks-Console": "1" },
      data: { token: new URL(ticket.url).hash.slice(8) },
    });
    const otherSession = await auth.json();
    expect(auth.ok()).toBe(true);
    expect(
      (
        await raw({ action: "source", source: slot.source.id }, other.request, {
          ...headers,
          "X-Supabricks-CSRF": otherSession.csrf,
        })
      ).status(),
    ).toBe(409);
    const upload = await other.request.post(
      origin + "/api/upload/" + slot.source.id,
      {
        headers: {
          ...headers,
          "X-Supabricks-CSRF": otherSession.csrf,
          "Content-Type": "application/octet-stream",
        },
        data: Buffer.from("a,b\n1,2\n"),
      },
    );
    expect(upload.status()).toBe(409);
    expect(
      (await api({ action: "source", source: slot.source.id })).received,
    ).toBe(0);
  } finally {
    await other.close();
  }
  expect(
    (
      await context.request.post(origin + "/api/upload/" + slot.source.id, {
        headers: {
          ...headers,
          "X-Supabricks-CSRF": "wrong",
          "Content-Type": "application/octet-stream",
        },
        data: Buffer.from("a,b\n1,2\n"),
      })
    ).status(),
  ).toBe(403);
  await api({ action: "dispose", source: slot.source.id });
  record(
    "browser upload rejects oversize, host paths, wrong CSRF and another session/source binding; display names cannot select storage paths",
  );

  // A raw socket closes after a partial body; server must close its daemon slot.
  const partial = await api({
    action: "begin",
    name: "aborted.csv",
    bytes: 100,
  });
  const cookie = (await context.cookies(origin))
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");
  await new Promise((resolve, reject) => {
    const socket = createConnection(
      { host: "127.0.0.1", port: Number(new URL(origin).port) },
      () => {
        socket.write(
          `POST /api/upload/${partial.source.id} HTTP/1.1\r\nHost: ${new URL(origin).host}\r\nOrigin: ${origin}\r\nCookie: ${cookie}\r\nX-Supabricks-Console: 1\r\nX-Supabricks-CSRF: ${session.csrf}\r\nContent-Type: application/octet-stream\r\nContent-Length: 100\r\n\r\na,b\n`,
        );
        setTimeout(() => {
          socket.destroy();
          resolve();
        }, 100);
      },
    );
    socket.on("error", reject);
  });
  await expect
    .poll(
      async () =>
        (await cli("ingest", "source", partial.source.id)).source.state,
      { timeout: 35000 },
    )
    .toBe("disposed");
  record(
    "aborted streaming upload disposes its partial source without a staged payload",
  );

  await page.goto(origin);
  await page
    .getByRole("button", { name: "Database workspace", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Navigation branch" })
    .selectOption({ label: "main · running" });
  await page.getByRole("button", { name: "Import file", exact: true }).click();
  const fixture = await readFile(
    new URL("../../examples/console/orders.csv", import.meta.url),
  );
  const original = join(workspace, "device-orders.csv");
  await writeFile(original, fixture);
  const picker = page.getByLabel("Choose CSV or TSV");
  await picker.focus();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.keyboard.press("Enter");
  const chooser = await chooserPromise;
  await chooser.setFiles(original);
  await expect(page.getByRole("table", { name: "Column mapping" })).toBeVisible(
    { timeout: 30000 },
  );
  await page.getByLabel("Column 3 type").selectOption("decimal");
  await page.getByLabel("Column 4 type").selectOption("boolean");
  await page
    .getByLabel("New table name", { exact: true })
    .fill("browser_orders");
  await page.getByText("Preview 4 sample rows", { exact: true }).click();
  await expect(
    page.getByRole("table", { name: "Import sample" }),
  ).toContainText("0001");
  const staged = (await api({ action: "sources" })).find(
    (s) => s.source.display_name === "device-orders.csv",
  );
  let preview = await api({ action: "source", source: staged.source.id });
  await api({
    action: "inspect",
    source: staged.source.id,
    mapping: preview.status.inspection.mapping,
  });
  await expect
    .poll(
      async () =>
        (await api({ action: "source", source: staged.source.id })).status
          .inspection,
      { timeout: 30000 },
    )
    .not.toBeNull();
  const overview = await (
    await context.request.get(origin + "/api/overview", { headers })
  ).json();
  const branch = overview.branches.find((b) => b.name === "main");
  const load = {
    version: 1,
    project_id: overview.project.id,
    branch_id: branch.id,
    branch_revision: branch.revision,
    source_id: staged.source.id,
    source_sha256: preview.status.source.sha256,
    mapping: preview.status.inspection.mapping,
    schema: "public",
    table: "stale_preview_should_not_exist",
  };
  expect(
    (
      await raw({
        action: "load",
        key: crypto.randomUUID(),
        preview: preview.preview,
        load,
      })
    ).status(),
  ).toBe(409);
  record(
    "keyboard file chooser streams original CSV unchanged; superseded preview approval is rejected",
  );
  // Reinspection intentionally resets edited types and requires fresh approval.
  await expect(page.getByLabel("Column 3 type")).toHaveValue("text");
  await page.getByLabel("Column 3 type").selectOption("decimal");
  await page.getByLabel("Column 4 type").selectOption("boolean");
  await page
    .getByLabel("I approve these columns and this destination.")
    .check();
  await page
    .getByRole("button", { name: "Create table and import", exact: true })
    .click();
  await expect(
    page.locator(".import-job").filter({ hasText: "browser_orders" }),
  ).toContainText("4 committed rows", { timeout: 30000 });
  await expect(page.getByRole("table", { name: "SQL results" })).toContainText(
    "0001",
    { timeout: 15000 },
  );
  expect(await readFile(original)).toEqual(fixture);
  const jobs = await api({ action: "list" });
  const job = jobs.find((j) => j.load.table === "browser_orders");
  await page.reload();
  await page
    .getByRole("button", { name: "Database workspace", exact: true })
    .click();
  await expect(
    page.locator(".import-job").filter({ hasText: job.id }),
  ).toContainText("4 committed rows");
  expect(
    (await api({ action: "list" })).filter(
      (j) => j.load.table === "browser_orders",
    ),
  ).toHaveLength(1);
  expect((await cli("ingest", "status", job.id)).committed_rows).toBe(4);
  record(
    "browser approved decimal/boolean mapping commits exactly four rows, opens the new table and reload reconnects to the same CLI-visible job",
  );

  await cli("branch", "create", "import-demo", "--from", "main", "--wait");
  await cli(
    "sql",
    "--branch",
    "import-demo",
    "--sql",
    "UPDATE browser_orders SET amount=amount*0.9 WHERE paid",
    "--write",
  );
  expect(
    (
      await cli(
        "sql",
        "--branch",
        "main",
        "--sql",
        "SELECT amount FROM browser_orders WHERE order_id='0001'",
      )
    ).rows,
  ).toEqual([["19.95"]]);
  expect(
    (
      await cli(
        "sql",
        "--branch",
        "import-demo",
        "--sql",
        "SELECT amount FROM browser_orders WHERE order_id='0001'",
      )
    ).rows,
  ).toEqual([["17.96"]]);
  await cli("branch", "delete", "import-demo", "--wait");
  record(
    "synthetic CSV demo branches the imported table, mutates the child and proves the parent and device file are unchanged",
  );

  // Enough rows to catch the owned loader before it can finish, without a test hook.
  const large = join(workspace, "streamed.csv");
  const file = await open(large, "w", 0o600);
  try {
    await file.write("id,note\n");
    const block = Buffer.from(("1," + "x".repeat(60) + "\n").repeat(16384));
    for (let i = 0; i < 80; i++) await file.write(block);
    await file.sync();
  } finally {
    await file.close();
  }
  await page.getByRole("button", { name: "Import file", exact: true }).click();
  await page.getByLabel("Choose CSV or TSV").setInputFiles(large);
  await expect(page.getByRole("table", { name: "Column mapping" })).toBeVisible(
    { timeout: 120000 },
  );
  await page.getByLabel("Import branch").selectOption(branch.id);
  await page
    .getByLabel("New table name", { exact: true })
    .fill("browser_reload_cancel");
  await page
    .getByLabel("I approve these columns and this destination.")
    .check();
  await page
    .getByRole("button", { name: "Create table and import", exact: true })
    .click();
  let held;
  try {
    await expect
      .poll(
        async () => {
          const list = await api({ action: "list" });
          const j = list.find((j) => j.load.table === "browser_reload_cancel");
          if (!j || j.state !== "loading") return false;
          const status = await cli("status");
          const processRecord = status.runtime.processes.find(
            (p) =>
              p.role === `ingest-${j.id}` && p.generation === status.generation,
          );
          if (!processRecord) return false;
          held = processRecord.pid;
          process.kill(-held, "SIGSTOP");
          return true;
        },
        { timeout: 15000, intervals: [20, 30, 50] },
      )
      .toBe(true);
    await page.reload();
    await page
      .getByRole("button", { name: "Database workspace", exact: true })
      .click();
    const ongoing = page
      .locator(".import-job")
      .filter({ hasText: "browser_reload_cancel" });
    await expect(ongoing).toContainText("loading");
    await expect(ongoing).toContainText("Awaiting commit confirmation");
    await ongoing
      .getByRole("button", { name: "Cancel import", exact: true })
      .click();
    process.kill(-held, "SIGCONT");
    held = null;
    await expect(ongoing).toContainText("cancelled", { timeout: 30000 });
    expect(
      (
        await cli(
          "sql",
          "--branch",
          "main",
          "--sql",
          "SELECT to_regclass('browser_reload_cancel')",
        )
      ).rows,
    ).toEqual([[null]]);
    expect(
      (await api({ action: "list" })).filter(
        (j) => j.load.table === "browser_reload_cancel",
      ),
    ).toHaveLength(1);
  } finally {
    if (held)
      try {
        process.kill(-held, "SIGCONT");
      } catch (e) {
        if (e.code !== "ESRCH") throw e;
      }
  }
  record(
    "large file streams through bounded upload; browser reload during owned COPY reconnects to one job and cancellation leaves no partial table",
  );

  // A retained failed source can be explicitly retried; no automatic replay.
  await page.getByRole("button", { name: "Import file", exact: true }).click();
  const drag = await page.evaluateHandle(() => {
    const d = new DataTransfer();
    d.items.add(
      new File(["amount\nnot-an-integer\n"], "bad.csv", { type: "text/csv" }),
    );
    return d;
  });
  await page
    .locator(".import-drop")
    .dispatchEvent("drop", { dataTransfer: drag });
  await drag.dispose();
  await expect(page.getByRole("table", { name: "Column mapping" })).toBeVisible(
    { timeout: 30000 },
  );
  await page.getByLabel("Column 1 type").selectOption("integer");
  await page.getByLabel("New table name", { exact: true }).fill("browser_bad");
  await page.getByLabel("Import branch").selectOption(branch.id);
  await page
    .getByLabel("I approve these columns and this destination.")
    .check();
  await page
    .getByRole("button", { name: "Create table and import", exact: true })
    .click();
  const failed = page.locator(".import-job").filter({ hasText: "browser_bad" });
  await expect(failed).toContainText("failed", { timeout: 30000 });
  await expect(failed).toContainText("attempt 1");
  await failed.getByRole("button", { name: "Retry retained source" }).click();
  await expect(failed).toContainText("attempt 2", { timeout: 30000 });
  await expect(failed).toContainText("failed", { timeout: 30000 });
  await page
    .getByRole("button", { name: "Dispose source", exact: true })
    .click();
  const bad = (await api({ action: "list" })).find(
    (j) => j.load.table === "browser_bad",
  );
  expect((await cli("ingest", "source", bad.load.source_id)).source.state).toBe(
    "disposed",
  );
  record(
    "drag/drop uses the same real upload service; invalid conversion rolls back, explicit retry preserves job identity, and retained source disposal works",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({
    path: screenshot
      ? join(dirname(screenshot), "importer.png")
      : join(workspace, "importer.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "Overview", exact: true }).click();
}
