import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile, writeFile } from "node:fs/promises";
const exec = promisify(execFile);
const args = process.argv.slice(2),
  report = args[args.indexOf("--report") + 1];
const reports = {};
try {
  for (const [name, script] of [
    ["runtime", "frontend/runtime.mjs"],
    ["product", "../../../console/scripts/qualify-notebooks.mjs"],
  ]) {
    const childReport = report.replace(/\.json$/, `-${name}.json`);
    const childArgs = [...args];
    childArgs[childArgs.indexOf("--report") + 1] = childReport;
    try {
      await exec(
        process.execPath,
        [new URL(script, import.meta.url).pathname, ...childArgs],
        { timeout: 1500000, maxBuffer: 1048576 },
      );
    } finally {
      reports[name] = JSON.parse(await readFile(childReport, "utf8"));
    }
  }
} catch (e) {
  process.exitCode = 1;
  console.error(
    "Notebook qualification failed; inspect runtime and product reports.",
  );
} finally {
  await writeFile(
    report,
    JSON.stringify(
      { status: process.exitCode ? "failed" : "passed", ...reports },
      null,
      2,
    ) + "\n",
  );
}
