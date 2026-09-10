export interface Branch {
  id: string;
  name: string;
  parent_id: string | null;
  desired_state: string;
  revision: number;
  observed_revision: number;
  is_default: boolean;
  expired: boolean;
}
export interface Overview {
  api_version: 1;
  project: { id: string; name: string };
  worktree: string;
  data_dir: string;
  branches: Branch[];
  runtime: {
    ready: boolean;
    engine_enabled: boolean;
    generation: number;
    postgres_major: number;
    needs_attention: boolean;
  };
  capabilities: { overview: boolean; sql: boolean; ingestion: boolean };
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
let csrf = "";
async function request(path: string, method = "GET", body?: object) {
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.timeout(6000),
      headers: {
        "X-Supabricks-Console": "1",
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(csrf ? { "X-Supabricks-CSRF": csrf } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      "The local runtime is unavailable. Run supabricks doctor, then reopen the console.",
      503,
    );
  }
  let value;
  try {
    value = await response.json();
  } catch {
    throw new ApiError(
      "Unexpected console response. Reopen with supabricks console.",
      503,
    );
  }
  if (!response.ok)
    throw new ApiError(
      value.error?.message ?? "The request failed.",
      response.status,
    );
  if (value.api_version !== 1)
    throw new ApiError(
      "This console and runtime are different versions. Reopen with supabricks console.",
      409,
    );
  return value;
}
export async function authenticate(token: string | null) {
  const value = await request(
    "session",
    token ? "POST" : "GET",
    token ? { token } : undefined,
  );
  csrf = value.csrf;
}
export async function overview(): Promise<Overview> {
  const value = await request("overview");
  if (
    !value.capabilities?.overview ||
    !Array.isArray(value.branches) ||
    !value.project?.id
  ) {
    throw new ApiError(
      "The runtime does not support this overview. Reopen with supabricks console.",
      409,
    );
  }
  return value;
}
export async function logout() {
  await request("logout", "POST");
  csrf = "";
}

export type NotebookFile = { path: string };
export type NotebookDocument = {
  cells: { cell_type: "code" | "markdown" | "raw"; source: string; metadata?: object; [key: string]: unknown }[];
  metadata: object;
  nbformat: 4;
  nbformat_minor: number;
};
export async function notebookFiles(command: object): Promise<Record<string, unknown>> {
  return (await request("notebooks/contents", "POST", command)).value;
}

export type Target = { branch: string; revision: number };
export type SqlResult = {
  branch_id: string;
  columns: { name: string; type: string; oid: number }[];
  rows: (string | null)[][];
  affected_rows: number;
  read_only: boolean;
};
export type QueryHandle = {
  id: string;
  generation: number;
  target: Target;
  state: "running" | "cancelling" | "succeeded" | "failed" | "cancelled";
  read_only: boolean;
  elapsed_ms: number;
  result?: SqlResult;
  error?: { message: string };
};
export type Operation = {
  id: string;
  branch_id: string;
  status: "pending" | "succeeded" | "superseded" | "failed";
  next_step: number;
  steps: string[];
  error: unknown;
};
export type Saved = {
  id: string;
  revision: number;
  target: Target;
  title: string;
  sql?: string;
};
export type WorkspaceCommand =
  | { action: "create_database"; name: string; key: string }
  | { action: "create_branch"; name: string; target: Target; key: string }
  | {
      action: "set_state";
      target: Target;
      desired: "running" | "suspended";
      key: string;
    }
  | { action: "delete_branch"; target: Target; key: string }
  | { action: "operation"; id: string }
  | { action: "connect"; target: Target }
  | {
      action: "query";
      id: string;
      target: Target;
      sql: string;
      read_only: boolean;
      max_rows: number;
      timeout_ms: number;
    }
  | { action: "catalog"; id: string; target: Target }
  | {
      action: "preview";
      id: string;
      target: Target;
      schema: string;
      table: string;
    }
  | { action: "query_status" | "cancel_query"; id: string }
  | { action: "saved_list" }
  | { action: "saved_get"; id: string }
  | {
      action: "saved_put";
      id: string;
      expected_revision: number;
      target: Target;
      title: string;
      sql: string;
    }
  | { action: "saved_delete"; id: string; expected_revision: number };
export async function workspace<T>(command: WorkspaceCommand): Promise<T> {
  return (await request("workspace", "POST", command)).value;
}

export type Mapping = {
  version: 1;
  format: "csv";
  delimiter: string;
  header: boolean;
  null_strings: string[];
  columns: {
    input: string;
    name: string;
    data_type: { kind: string; precision?: number; scale?: number };
    nullable: boolean;
  }[];
};
export type ImportSource = {
  id: string;
  display_name: string;
  state: string;
  sha256: string | null;
  bytes: number;
  expires_at_ms: number;
};
export type SourceStatus = {
  status: {
    source: ImportSource;
    inspection: { mapping: Mapping; rows: (string | null)[][] } | null;
    error: string | null;
  };
  received: number;
  expected: number;
  preview: number;
};
export type ImportLoad = {
  version: 1;
  project_id: string;
  branch_id: string;
  branch_revision: number;
  source_id: string;
  source_sha256: string;
  mapping: Mapping;
  schema: string;
  table: string;
};
export type ImportJob = {
  id: string;
  load: ImportLoad;
  state: string;
  attempt: number;
  parsed_rows: number;
  copied_rows: number;
  committed_rows: number | null;
  retryable: boolean;
  source_released: boolean;
  error?: string;
  source?: ImportSource;
};
export async function ingest<T>(command: object): Promise<T> {
  return (await request("workspace", "POST", { action: "ingest", command }))
    .value;
}
export function upload(
  source: string,
  file: File,
  progress: (bytes: number) => void,
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload/${source}`);
    xhr.setRequestHeader("X-Supabricks-Console", "1");
    xhr.setRequestHeader("X-Supabricks-CSRF", csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 600000;
    xhr.upload.onprogress = (e) => progress(e.loaded);
    const abort = () => xhr.abort();
    signal.addEventListener("abort", abort, { once: true });
    xhr.onloadend = () => {
      signal.removeEventListener("abort", abort);
      if (xhr.status === 200) resolve();
      else
        reject(
          new Error(
            "Upload interrupted, rejected or out of disk space. Select the file again.",
          ),
        );
    };
    if (signal.aborted) {
      reject(new Error("Upload cancelled"));
      return;
    }
    // The browser streams the File directly; no arrayBuffer/text/base64 copy.
    xhr.send(file);
  });
}
