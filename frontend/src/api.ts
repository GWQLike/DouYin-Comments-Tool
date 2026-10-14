import type { Filters } from './types';

export async function responseError(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') return body.detail;
  } catch { /* A failed local service may return plain text. */ }
  return `请求未完成（${response.status}），请稍后重试。`;
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options?.headers },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('无法连接本机服务。请检查启动窗口是否仍在运行。');
  }
  if (!response.ok) throw new Error(await responseError(response));
  return response.json() as Promise<T>;
}

export function filterQuery(filters: Filters, page?: number): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (String(value).trim()) query.set(key, String(value).trim());
  }
  if (page !== undefined) {
    query.set('page', String(page));
    query.set('page_size', '50');
  }
  return query.toString();
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作未完成，请稍后重试。';
}
