// Real browser + native runtime. Every mutation is confined to a new /tmp root.
import { chromium, expect } from "@playwright/test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, mkdir, writeFile, rm, readFile } from "node:fs/promises";
import { join, resolve, dirname } from "node:path";
import { createServer } from "node:net";

const exec = promisify(execFile);
const options = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, args) => {
    if (i % 2 === 0) pairs.push([arg, args[i + 1]]);
    return pairs;
  }, []),
);
if (!options["--binary"] || !options["--report"])
  throw new Error(
    "Supply --binary and --report, optionally --bundle and --helpers",
  );
const binary = resolve(options["--binary"]);
const workspace = await mkdtemp("/tmp/sb-c01-");
const data = join(workspace, "data"),
  project = join(workspace, "app ' with spaces");
await mkdir(project, { mode: 0o700 });
const env = { ...process.env, SUPABRICKS_DATA_DIR: data };
for (const key of Object.keys(env))
  if (/^(PG|AWS_|PC_|OTEL_)/.test(key) || /_PROXY$/i.test(key)) delete env[key];
const checks = [],
  external = [],
  errors = [];
let browser;
const occupied = createServer();
await new Promise((resolve) => {
  occupied.once("error", resolve);
  occupied.listen(5432, "127.0.0.1", resolve);
});
const report = {
  status: "running",
  checks,
  limits: [
    "Chromium browser qualification; not Safari or Firefox qualification",
  ],
  network_qualification:
    process.env.SUPABRICKS_CONSOLE_NETWORK_EVIDENCE ??
    "page requests restricted to loopback; host network not isolated",
};
async function cli(...args) {
  try {
    const scoped =
      args[0] === "installation" && args[1] === "verify"
        ? args
        : [...args, "--project", project];
    const result = await exec(binary, scoped, {
      env,
      timeout: 120000,
      maxBuffer: 2 * 1024 * 1024,
    });
    return JSON.parse(result.stdout);
  } catch (e) {
    // Never include command arguments or stdout: launch URLs and connect contain secrets.
    throw new Error(
      `CLI ${args[0]} failed: ${String(e.stderr ?? e.code ?? "unknown").slice(-1500)}`,
    );
  }
}
async function launch() {
  return cli("console", "--no-open");
}
try {
  await cli("init", "console-demo");
  if (options["--bundle"])
    await cli(
      "up",
      "--bundle",
      resolve(options["--bundle"]),
      "--helpers",
      resolve(options["--helpers"]),
    );
  // Installed mode deliberately starts the runtime through console, without a preceding up.
  const first = await launch();
  const origin = new URL(first.url).origin;
  browser = await chromium.launch({
    headless: true,
    args: ["--disable-background-networking", "--disable-component-update"],
  });
  report.browser = browser.version();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1") {
      external.push(url.origin);
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(first.url);
  await expect(
    page.getByRole("heading", { name: "Your first branch starts here." }),
  ).toBeVisible();
  await expect(page.getByText("Runtime ready", { exact: true })).toBeVisible();
  expect(page.url()).toBe(origin + "/");
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  checks.push(
    `${options["--bundle"] ? "source" : "installed"} console starts/reconnects runtime, opens real empty project, consumes fragment and stores no browser data`,
  );
  const replay = await context.request.post(origin + "/api/session", {
    headers: { Origin: origin, "X-Supabricks-Console": "1" },
    data: { token: new URL(first.url).hash.slice("#launch=".length) },
  });
  expect(replay.status()).toBe(401);
  const createCommand = await page.locator(".command code").textContent();
  expect(createCommand).toContain("--project");
  expect(createCommand).toContain("--data-dir");
  await exec("/bin/bash", ["-c", createCommand], {
    env: { ...env, PATH: `${dirname(binary)}:${env.PATH}` },
    cwd: "/tmp",
    timeout: 120000,
  });
  checks.push(
    "empty-project command creates the correct database from another directory, including quoted project paths and a custom data root",
  );
  await cli("branch", "use", "main");
  await cli("branch", "create", "experiment", "--from", "main", "--wait");
  await expect(
    page.getByRole("button", { name: "experiment", exact: true }),
  ).toBeVisible({ timeout: 15000 });
  const before = await cli("connect");
  await page.getByRole("button", { name: "experiment", exact: true }).click();
  await expect(
    page.getByText("Selection is local to this console."),
  ).toBeVisible();
  expect((await cli("connect")).uri).toBe(before.uri);
  await page.getByRole("textbox", { name: "Find a branch" }).fill("experiment");
  await expect(
    page.getByRole("button", { name: "main", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("textbox", { name: "Find a branch" }).fill("");
  await expect(
    page.getByRole("button", { name: "main", exact: true }),
  ).toBeVisible();
  checks.push(
    "real root and child branches render and filter; browser selection preserves CLI worktree selection",
  );
  await page.reload();
  await expect(
    page.getByRole("button", { name: "experiment", exact: true }),
  ).toBeVisible();
  const repeat = await launch();
  expect(new URL(repeat.url).origin).toBe(origin);
  await page.goto(repeat.url);
  await expect(
    page.getByRole("button", { name: "main", exact: true }),
  ).toBeVisible();
  checks.push(
    "reload uses authenticated session; repeated CLI launch reuses the bound bridge with a fresh one-use ticket",
  );
  if (options["--screenshot"]) {
    await mkdir(dirname(resolve(options["--screenshot"])), { recursive: true });
    await page.screenshot({
      path: resolve(options["--screenshot"]),
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Refresh", exact: false }).focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("button", { name: "main", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 960 });
  checks.push(
    "narrow viewport remains usable and refresh works from the keyboard",
  );
  const status = await cli("status");
  const processRecord = status.runtime.processes.find((p) =>
    p.role.startsWith("console-"),
  );
  const supervisor = status.runtime.processes.find(
    (p) => p.role === "supervisor",
  );
  if (!processRecord || processRecord.generation !== status.generation)
    throw new Error("Missing owned console process");
  process.kill(processRecord.pid, "SIGKILL");
  await expect(page.getByRole("alert")).toBeVisible({ timeout: 15000 });
  const reopened = await launch();
  await page.goto(reopened.url);
  await expect(
    page.getByRole("button", { name: "main", exact: true }),
  ).toBeVisible();
  expect(
    (await cli("status")).runtime.processes.find((p) => p.role === "supervisor")
      .pid,
  ).toBe(supervisor.pid);
  checks.push(
    "console SIGKILL is fenced and reopened without restarting the storage supervisor",
  );
  await cli("down");
  const restarted = await launch();
  await page.goto(restarted.url);
  await expect(
    page.getByRole("button", { name: "experiment", exact: true }),
  ).toBeVisible();
  checks.push(
    "whole-cell shutdown and console-driven restart retain branches and issue fresh browser sessions",
  );
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("alert")).toContainText("Session closed");
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("session expired");
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
  checks.push(
    "sign-out revokes session; application loads no external resources and emits no browser errors",
  );
  if (!options["--bundle"]) {
    await cli("installation", "verify");
    const manifest = await readFile(
      join(
        dirname(
          await import("node:fs/promises").then((m) => m.realpath(binary)),
        ),
        "../release.json",
      ),
    );
    report.release_version = JSON.parse(manifest).version;
    report.release_identity = (await import("node:crypto"))
      .createHash("sha256")
      .update(manifest)
      .digest("hex");
    checks.push(
      "browser use leaves exact installed release inventory unchanged",
    );
  }
  report.status = "passed";
} catch (e) {
  report.status = "failed";
  // Assertion diagnostics may contain launch URLs. Redact fragment credentials.
  report.error = String(e.message)
    .replace(/#launch=[a-f0-9]+/g, "#launch=REDACTED")
    .replace(/postgres(?:ql)?:\/\/[^\s]+/g, "postgresql://REDACTED")
    .slice(-3000);
  process.exitCode = 1;
} finally {
  await browser?.close();
  occupied.close();
  try {
    await cli("down");
  } catch {
    report.cleanup = "runtime cleanup needs inspection";
    report.status = "failed";
    process.exitCode = 1;
  }
  await mkdir(dirname(resolve(options["--report"])), { recursive: true });
  await writeFile(
    resolve(options["--report"]),
    JSON.stringify(report, null, 2) + "\n",
  );
  console.log(JSON.stringify(report));
  if (report.status === "passed" && !report.cleanup)
    await rm(workspace, { recursive: true });
}
