import { chromium } from "@playwright/test";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve, dirname, join } from "node:path";
import { qualifyNotebooks } from "./notebooks.mjs";
const exec = promisify(execFile);
const args = Object.fromEntries(
  process.argv.slice(2).reduce((a, x, i, s) => {
    if (i % 2 === 0) a.push([x, s[i + 1]]);
    return a;
  }, []),
);
if (!args["--binary"] || !args["--report"])
  throw new Error("Supply --binary and --report");
const root = await mkdtemp("/tmp/sb-product-notebooks-"),
  project = root + "/project",
  data = root + "/data";
await mkdir(project);
const report = {
  status: "running",
  checks: [],
  errors: [],
  network:
    process.env.SUPABRICKS_CONSOLE_NETWORK_EVIDENCE ||
    "browser restricted to loopback; host network not isolated",
};
let browser, page;
async function cli(...command) {
  try {
    const result = await exec(
      resolve(args["--binary"]),
      [...command, "--project", project, "--data-dir", data],
      { timeout: 180000, maxBuffer: 1048576 },
    );
    return JSON.parse(result.stdout.trim().split("\n").at(-1));
  } catch (e) {
    throw new Error(
      `Fixture ${command[0]} failed: ${String(e.stderr ?? e.code).slice(-1000)}`,
    );
  }
}
try {
  await cli("init", "notebook-repair");
  if (args["--bundle"])
    await cli(
      "up",
      "--bundle",
      resolve(args["--bundle"]),
      "--helpers",
      resolve(args["--helpers"]),
    );
  if (args["--python"])
    await cli(
      "analytics",
      "configure",
      "--python",
      resolve(args["--python"]),
      "--worker",
      resolve(args["--worker"]),
    );
  await cli("up");
  await cli("database", "create", "main", "--wait");
  await cli(
    "sql",
    "--branch",
    "main",
    "--write",
    "--sql",
    "CREATE TABLE public.orders(id int,amount numeric(18,2))",
  );
  await cli(
    "sql",
    "--branch",
    "main",
    "--write",
    "--sql",
    "INSERT INTO public.orders VALUES(1,12.50),(2,7.25)",
  );
  const launch = await cli("console", "--no-open");
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  await context.route("**/*", (route) =>
    new URL(route.request().url()).hostname === "127.0.0.1"
      ? route.continue()
      : route.abort(),
  );
  page = await context.newPage();
  page.on("pageerror", (e) => report.errors.push(e.message));
  page.on("console", (m) => {
    if (
      m.type() === "error" &&
      /Content Security Policy|violates.*policy/i.test(m.text())
    )
      report.errors.push(m.text());
  });
  await page.goto(launch.url);
  await qualifyNotebooks(page, { project, cli, checks: report.checks });
  const python =
    args["--python"] ||
    join(
      dirname(
        await import("node:fs/promises").then((fs) =>
          fs.realpath(resolve(args["--binary"])),
        ),
      ),
      "../python/analytics/python",
    );
  await exec(python, [
    "-c",
    "import nbformat,sys,pathlib; [nbformat.validate(nbformat.read(p,as_version=4)) for p in pathlib.Path(sys.argv[1]).glob('*.ipynb')]",
    join(project, "notebooks"),
  ]);
  report.checks.push(
    "saved_product_notebook_passes_standard_nbformat_validation",
  );
  if (report.errors.length)
    throw new Error("Unhandled page errors: " + report.errors.join("\n"));
  report.status = "passed";
} catch (e) {
  report.status = "failed";
  report.error = e.message;
  report.visible = await page
    ?.locator(".notebook-view")
    .innerText()
    .catch(() => null);
  process.exitCode = 1;
} finally {
  await browser?.close();
  await cli("down").catch(() => {});
  await writeFile(
    resolve(args["--report"]),
    JSON.stringify(report, null, 2) + "\n",
  );
  console.log(JSON.stringify(report, null, 2));
}
