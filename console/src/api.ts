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
