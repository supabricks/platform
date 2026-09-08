import { expect } from "@playwright/test";
import { dirname, join } from "node:path";

export async function qualifyWorkspace({
  page,
  context,
  origin,
  cli,
  checks,
  screenshot,
}) {
  const record = (text) => {
    checks.push(text);
    console.log(JSON.stringify({ check: text }));
  };
  const nav = page.getByRole("combobox", { name: "Navigation branch" });
  const editor = page.getByRole("textbox", { name: "SQL statement" });
  const status = page.locator(".query-status strong");
  const write = page.getByRole("checkbox", { name: /Allow writes/ });
  async function operationDone() {
    await expect
      .poll(
        async () => {
          if (await page.locator(".operation-status strong").count())
            return page.locator(".operation-status strong").textContent();
          if (await page.locator(".database-workspace > .notice").count())
            return "request_failed";
          return "submitting";
        },
        { timeout: 120000 },
      )
      .toMatch(/^(succeeded|failed|superseded|request_failed)$/);
    expect(
      await page
        .locator(".operation-status strong")
        .textContent()
        .catch(() => null),
      await page.locator(".notice:visible").allTextContents(),
    ).toBe("succeeded");
  }
  async function run(
    sql,
    { writes = false, state = "succeeded", keyboard = false } = {},
  ) {
    await editor.fill(sql);
    await write.setChecked(writes);
    const old = (await page.locator(".query-status code").count())
      ? await page.locator(".query-status code").textContent()
      : null;
    if (keyboard) {
      await editor.focus();
      await page.keyboard.press("Control+Enter");
    } else
      await page.getByRole("button", { name: "Run SQL", exact: true }).click();
    if (old)
      await expect(page.locator(".query-status code")).not.toHaveText(old);
    await expect
      .poll(async () => status.textContent(), { timeout: 50000 })
      .toMatch(/^(succeeded|failed|cancelled)$/);
    expect(
      await status.textContent(),
      await page.locator(".sql-panel .notice").allTextContents(),
    ).toBe(state);
  }
  await page
    .getByRole("button", { name: "Database workspace", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Build on your data." }),
  ).toBeVisible();
  await nav.selectOption({ label: "main · running" });
  const main = await nav.inputValue();
  await page.getByRole("button", { name: "New SQL tab" }).click();
  await run(
    "CREATE TABLE console_numbers (id bigint PRIMARY KEY, amount numeric(30,10), note text)",
    { writes: true, keyboard: true },
  );
  await run(
    "INSERT INTO console_numbers VALUES (9007199254740993, 12345678901234567890.1234567890, NULL)",
    { writes: true },
  );
  await run("SELECT * FROM console_numbers");
  await expect(
    page.getByRole("button", {
      name: "id, row 1: 9007199254740993",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "amount, row 1: 12345678901234567890.1234567890",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "note, row 1: SQL NULL", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Query title", exact: true })
    .fill("Exact numbers");
  await page.getByRole("button", { name: "Save query", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Exact numbers", exact: true }),
  ).toBeVisible();
  record(
    "browser writes and reads real PostgreSQL data, preserves bigint/decimal text and SQL NULL, executes from keyboard and explicitly saves a query",
  );

  await page.getByText("Create or delete a branch", { exact: true }).click();
  await page
    .getByRole("combobox", { name: "Create kind" })
    .selectOption("branch");
  await page
    .getByRole("textbox", { name: "New branch name" })
    .fill("console-child");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await operationDone();
  await expect(nav.locator("option", { hasText: "console-child" })).toHaveCount(
    1,
  );
  const child = await nav
    .locator("option", { hasText: "console-child" })
    .getAttribute("value");
  await nav.selectOption(child);
  await expect(page.locator(".query-binding strong")).toHaveText(
    "PostgreSQL · main",
  );
  await page.getByRole("button", { name: "New SQL tab" }).click();
  await run("UPDATE console_numbers SET note='child only'", { writes: true });
  await run("SELECT note FROM console_numbers");
  await expect(
    page.getByRole("button", { name: "note, row 1: child only", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("tab", { name: "Exact numbers · main", exact: true })
    .click();
  await run("SELECT note FROM console_numbers");
  await expect(
    page.getByRole("button", { name: "note, row 1: SQL NULL", exact: true }),
  ).toBeVisible();
  expect((await cli("connect")).branch_id).toBe(main);
  record(
    "browser creates a child branch, modifies child data and proves unchanged parent; SQL tabs and CLI selection retain independent branch bindings",
  );

  await page
    .getByRole("button", { name: "Refresh tables", exact: true })
    .click();
  await expect(
    page.getByText("public.console_numbers", { exact: true }),
  ).toBeVisible({ timeout: 50000 });
  await page.getByText("public.console_numbers", { exact: true }).click();
  await expect(
    page.locator(".explorer").getByText("bigint", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Preview console_numbers" }).click();
  await expect(status).toHaveText("succeeded", { timeout: 50000 });
  await expect(
    page.getByRole("button", { name: "note, row 1: child only", exact: true }),
  ).toBeVisible();
  await run("SELECT generate_series(1, 1000) AS sequence", { state: "failed" });
  await expect(
    page.getByRole("alert").filter({ hasText: "SQL result limit exceeded" }),
  ).toBeVisible();
  await page.getByRole("spinbutton", { name: "Row limit" }).fill("1000");
  await run("SELECT generate_series(1, 1000) AS sequence");
  expect(
    await page.locator(".result-viewport tbody tr[aria-rowindex]").count(),
  ).toBeLessThan(30);
  await page.locator(".result-viewport").evaluate((e) => {
    e.scrollTop = e.scrollHeight;
  });
  await expect(
    page.getByRole("button", { name: "sequence, row 1000: 1000", exact: true }),
  ).toBeVisible();
  await run("SELECT repeat('x', 300000)", { state: "failed" });
  await expect(
    page.getByRole("alert").filter({ hasText: "SQL result limit exceeded" }),
  ).toBeVisible();
  await run("SELECT no_such_console_column", { state: "failed" });
  await expect(
    page.getByRole("alert").filter({ hasText: "42703" }),
  ).toBeVisible();
  await run("UPDATE console_numbers SET note='must not write'", {
    state: "failed",
  });
  await expect(
    page.getByRole("alert").filter({ hasText: "25006" }),
  ).toBeVisible();
  record(
    "catalog and preview use real branch data; virtual grid renders bounded rows, scrolls to row 1000, reports row/byte/SQL errors and rejects writes in read-only mode",
  );

  await editor.fill("SELECT pg_sleep(20)");
  await page.getByRole("button", { name: "Run SQL", exact: true }).click();
  await expect(status).toHaveText("running");
  await page
    .getByRole("tab", { name: "Exact numbers · main", exact: true })
    .click();
  await editor.fill("SELECT pg_sleep(2), 42 AS answer");
  await page.getByRole("button", { name: "Run SQL", exact: true }).click();
  await expect(status).toHaveText("running");
  await page
    .getByRole("tab", {
      name: "public.console_numbers · console-child",
      exact: true,
    })
    .click();
  await page.getByRole("button", { name: "Cancel query", exact: true }).click();
  await expect(status).toHaveText("cancelled", { timeout: 10000 });
  await page
    .getByRole("tab", { name: "Exact numbers · main", exact: true })
    .click();
  await expect(status).toHaveText("succeeded", { timeout: 10000 });
  await expect(
    page.getByRole("button", { name: "answer, row 1: 42", exact: true }),
  ).toBeVisible();
  record(
    "targeted browser cancellation terminates one query while another tab completes independently",
  );

  await page.getByRole("button", { name: "Reveal connection" }).click();
  await expect(page.locator(".connection-reveal code")).toContainText(
    "postgresql://",
  );
  await page.getByRole("button", { name: "Hide connection" }).click();
  await expect(page.locator(".connection-reveal")).toHaveCount(0);
  await expect
    .poll(async () => (await cli("status")).gateway.connections, {
      timeout: 3000,
    })
    .toBe(0);
  // A stale tab must not silently rebind after a CLI lifecycle change.
  await page
    .getByRole("tab", {
      name: "public.console_numbers · console-child",
      exact: true,
    })
    .click();
  await page
    .getByRole("button", { name: "Suspend branch", exact: true })
    .click();
  await operationDone();
  await expect(
    page.getByRole("alert").filter({ hasText: "This tab’s branch changed" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run SQL", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Resume branch", exact: true })
    .click();
  await operationDone();
  await page
    .getByRole("button", { name: "Rebind to navigation branch" })
    .click();
  await run("SELECT note FROM console_numbers");
  await expect(
    page.getByRole("button", { name: "note, row 1: child only", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Confirm branch deletion" })
    .fill("console-child");
  await page
    .getByRole("button", { name: "Delete branch", exact: true })
    .click();
  await operationDone();
  await expect(
    page.getByRole("alert").filter({ hasText: "This tab’s branch changed" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run SQL", exact: true }),
  ).toBeDisabled();
  record(
    "connection credentials require explicit reveal; suspend/resume and deletion surface durable completion and force stale tabs to rebind",
  );

  // Session/CSRF checks cover the newly writable endpoint, including non-allowlisted operations.
  const session = await context.request.get(origin + "/api/session", {
    headers: { "X-Supabricks-Console": "1" },
  });
  const { csrf } = await session.json();
  const headers = {
    Origin: origin,
    "X-Supabricks-Console": "1",
    "X-Supabricks-CSRF": csrf,
  };
  const denied = await context.request.post(origin + "/api/workspace", {
    headers: { Origin: origin, "X-Supabricks-Console": "1" },
    data: { action: "saved_list" },
  });
  expect(denied.status()).toBe(403);
  const arbitrary = await context.request.post(origin + "/api/workspace", {
    headers,
    data: { action: "select_branch", branch: main },
  });
  expect(arbitrary.status()).toBe(400);
  const cross = await context.request.post(origin + "/api/workspace", {
    headers: { ...headers, Origin: "https://evil.invalid" },
    data: { action: "saved_list" },
  });
  expect(cross.status()).toBe(403);
  const tooLarge = await context.request.post(origin + "/api/workspace", {
    headers,
    data: { action: "saved_list", padding: "x".repeat(61000) },
  });
  expect(tooLarge.status()).toBe(413);
  const overview = await (
    await context.request.get(origin + "/api/overview", {
      headers: { "X-Supabricks-Console": "1" },
    })
  ).json();
  const rootBranch = overview.branches.find((b) => b.id === main);
  const jobs = [];
  for (let i = 0; i < 4; i++) {
    const command = {
      action: "query",
      id: crypto.randomUUID(),
      target: { branch: main, revision: rootBranch.revision },
      sql: "SELECT pg_sleep(20)",
      read_only: true,
      max_rows: 200,
      timeout_ms: 30000,
    };
    const response = await context.request.post(origin + "/api/workspace", {
      headers,
      data: command,
    });
    expect(response.status()).toBe(200);
    jobs.push(command);
  }
  const duplicate = await context.request.post(origin + "/api/workspace", {
    headers,
    data: jobs[0],
  });
  expect(duplicate.status()).toBe(200);
  expect((await duplicate.json()).value.id).toBe(jobs[0].id);
  const changed = await context.request.post(origin + "/api/workspace", {
    headers,
    data: { ...jobs[0], sql: "SELECT 1" },
  });
  expect(changed.status()).toBe(409);
  const fifth = await context.request.post(origin + "/api/workspace", {
    headers,
    data: { ...jobs[0], id: crypto.randomUUID() },
  });
  expect(fifth.status()).toBe(409);
  let busy = false;
  try {
    await cli("sql", "--sql", "SELECT 1", "--branch", "main");
  } catch (e) {
    busy = String(e).includes("all four SQL workers");
  }
  expect(busy).toBe(true);
  const other = await context.browser().newContext();
  try {
    const link = await cli("console", "--no-open");
    const login = await other.request.post(origin + "/api/session", {
      headers: { Origin: origin, "X-Supabricks-Console": "1" },
      data: { token: new URL(link.url).hash.slice("#launch=".length) },
    });
    const otherCsrf = (await login.json()).csrf;
    const foreign = await other.request.post(origin + "/api/workspace", {
      headers: { ...headers, "X-Supabricks-CSRF": otherCsrf },
      data: { action: "cancel_query", id: jobs[0].id },
    });
    expect(foreign.status()).toBe(404);
  } finally {
    await other.close();
  }
  for (const job of jobs)
    await context.request.post(origin + "/api/workspace", {
      headers,
      data: { action: "cancel_query", id: job.id },
    });
  for (const job of jobs)
    await expect
      .poll(
        async () => {
          const response = await context.request.post(
            origin + "/api/workspace",
            { headers, data: { action: "query_status", id: job.id } },
          );
          return (await response.json()).value.state;
        },
        { timeout: 10000 },
      )
      .toBe("cancelled");
  record(
    "real SQL workers share a four-worker cap with synchronous CLI queries; duplicate handles do not start new work, changed requests conflict and another browser session cannot cancel them",
  );
  await page.reload();
  await page
    .getByRole("button", { name: "Database workspace", exact: true })
    .click();
  await expect(page.getByRole("tab")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Exact numbers", exact: true })
    .click();
  await expect(editor).toHaveValue("SELECT * FROM console_numbers");
  await expect(page.locator(".query-status")).toHaveCount(0);
  await expect(write).not.toBeChecked();
  await run("SELECT * FROM console_numbers");
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  if (screenshot)
    await page.screenshot({
      path: join(dirname(screenshot), "workspace.png"),
      fullPage: true,
    });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
  record(
    "workspace rejects missing CSRF, cross-origin, oversized and arbitrary API requests; reload restores only explicitly saved text with writes disabled and no query replay or browser storage",
  );
  await page.getByRole("link", { name: "Overview", exact: true }).click();
}

export async function verifySavedAfterRestart(page, checks) {
  await page
    .getByRole("button", { name: "Database workspace", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Exact numbers", exact: true })
    .click();
  await expect(
    page.getByRole("textbox", { name: "SQL statement" }),
  ).toHaveValue("SELECT * FROM console_numbers");
  await expect(page.locator(".query-status")).toHaveCount(0);
  checks.push(
    "full runtime restart retains saved query text and branch binding without replaying SQL",
  );
  await page.getByRole("link", { name: "Overview", exact: true }).click();
}
