import type { ReviewDetail, ReviewHistoryItem } from "../types/review";


export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}


const DEV_TENANT_IDS: Record<string, string> = {
  "tenant-a": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "law-firm": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
};

export function currentActorTenant(): string {
  const storedTenant =
    window.localStorage.getItem("actor_tenant") || "tenant-a";
  return DEV_TENANT_IDS[storedTenant] ?? storedTenant;
}


function authHeaders(): Record<string, string> {
  const role = window.localStorage.getItem("actor_role");
  if (!role) {
    return {};
  }
  return {
    "X-Actor-User": window.localStorage.getItem("actor_user") || "user-a",
    "X-Actor-Tenant": currentActorTenant(),
    "X-Actor-Role": role,
  };
}


async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return body.detail;
    }
  } catch {
    // The fallback still carries the HTTP status when the body is not JSON.
  }
  return fallback;
}


export async function getReview(jobId: string): Promise<ReviewDetail> {
  const response = await fetch(`/api/reviews/${encodeURIComponent(jobId)}`, {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!response.ok) {
    throw new ApiError(`review request failed: ${response.status}`, response.status);
  }
  return (await response.json()) as ReviewDetail;
}

export async function listReviewHistory(): Promise<ReviewHistoryItem[]> {
  const response = await fetch("/api/reviews", {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!response.ok) {
    throw new ApiError(
      `review history failed: ${response.status}`,
      response.status,
    );
  }
  return (await response.json()) as ReviewHistoryItem[];
}

export async function uploadContract(file: File): Promise<{ id: string }> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/contracts", {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (!response.ok) {
    throw new Error(`upload failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function retryReview(jobId: string): Promise<{ id: string }> {
  const response = await fetch(
    `/api/reviews/${encodeURIComponent(jobId)}/retry`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new ApiError(
      await readErrorDetail(response, `review retry failed: ${response.status}`),
      response.status,
    );
  }
  return (await response.json()) as { id: string };
}

export async function rerunReview(jobId: string): Promise<{ id: string }> {
  const response = await fetch(
    `/api/reviews/${encodeURIComponent(jobId)}/rerun`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new ApiError(
      await readErrorDetail(response, `review rerun failed: ${response.status}`),
      response.status,
    );
  }
  return (await response.json()) as { id: string };
}

export async function getContractFile(jobId: string): Promise<Blob> {
  const response = await fetch(
    `/api/reviews/${encodeURIComponent(jobId)}/file`,
    { headers: { ...authHeaders() } },
  );
  if (!response.ok) {
    throw new Error(`contract file failed: ${response.status}`);
  }
  return response.blob();
}

export interface KnowledgeDocument {
  id: string
  title: string
  scope: "public" | "firm" | "tenant_private"
  version: string
  effective_date?: string
  status: "active" | "inactive" | "deleted"
}

export interface KnowledgeMetadata {
  title?: string
  version?: string
  issuing_authority?: string
  effective_date?: string
  source_type?: string
}

export interface TenantRecord {
  id: string
  slug: string
  name: string
  status: string
}

export async function listKnowledge(
  scope?: string,
  status?: string,
): Promise<KnowledgeDocument[]> {
  const params = new URLSearchParams();
  if (scope) params.set("scope", scope);
  if (status) params.set("status", status);
  const query = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`/api/admin/knowledge${query}`, {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!response.ok) {
    throw new Error(`knowledge list failed: ${response.status}`);
  }
  return (await response.json()) as KnowledgeDocument[];
}

export async function extractKnowledgeMetadata(
  file: File,
): Promise<KnowledgeMetadata> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/admin/knowledge/metadata", {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (!response.ok) {
    throw new Error(
      `knowledge metadata extraction failed: ${response.status}`,
    );
  }
  return (await response.json()) as KnowledgeMetadata;
}

export async function uploadKnowledge(
  form: FormData,
): Promise<{ id: string }> {
  const response = await fetch("/api/admin/knowledge", {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (!response.ok) {
    throw new Error(`knowledge upload failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function deactivateKnowledge(
  id: string,
): Promise<{ id: string }> {
  const response = await fetch(
    `/api/admin/knowledge/${encodeURIComponent(id)}/deactivate`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new Error(`knowledge deactivate failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function activateKnowledge(
  id: string,
): Promise<{ id: string }> {
  const response = await fetch(
    `/api/admin/knowledge/${encodeURIComponent(id)}/activate`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new Error(`knowledge activate failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function archiveKnowledge(
  id: string,
): Promise<{ id: string }> {
  const response = await fetch(
    `/api/admin/knowledge/${encodeURIComponent(id)}/archive`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new Error(`knowledge archive failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function restoreKnowledge(
  id: string,
): Promise<{ id: string }> {
  const response = await fetch(
    `/api/admin/knowledge/${encodeURIComponent(id)}/restore`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) {
    throw new Error(`knowledge restore failed: ${response.status}`);
  }
  return (await response.json()) as { id: string };
}

export async function listTenants(): Promise<TenantRecord[]> {
  const response = await fetch("/api/admin/tenants", {
    headers: { Accept: "application/json", ...authHeaders() },
  });
  if (!response.ok) {
    throw new Error(`tenant list failed: ${response.status}`);
  }
  return (await response.json()) as TenantRecord[];
}

export async function createTenant(payload: {
  slug: string
  name: string
}): Promise<TenantRecord> {
  const response = await fetch("/api/admin/tenants", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`tenant create failed: ${response.status}`);
  }
  return (await response.json()) as TenantRecord;
}
