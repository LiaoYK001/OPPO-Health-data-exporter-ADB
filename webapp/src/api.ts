export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function apiErrorMessage(payload: unknown, status: number): string {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return `请求失败（${status}）`;
  }
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== "object") return String(item);
        const record = item as { msg?: unknown; loc?: unknown };
        const message =
          typeof record.msg === "string" ? record.msg : JSON.stringify(item);
        const location = Array.isArray(record.loc)
          ? record.loc.slice(1).join(".")
          : "";
        return location ? `${location}：${message}` : message;
      })
      .filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  if (detail && typeof detail === "object" && "msg" in detail) {
    const message = (detail as { msg?: unknown }).msg;
    if (typeof message === "string") return message;
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return `请求失败（${status}）`;
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
  });
  const type = response.headers.get("content-type") ?? "";
  const payload = type.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = apiErrorMessage(payload, response.status);
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

export function query(
  path: string,
  params: Record<string, string | number | undefined>,
): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  return `${path}?${search.toString()}`;
}
