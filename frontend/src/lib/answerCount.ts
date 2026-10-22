/** Public aggregate only: no browser writes, identities, or subscriptions. */
export type AnswerCount = { answered: number; approximate: boolean };

export function mergeAnswerCount(current: AnswerCount | null, value: unknown): AnswerCount | null {
  if (typeof value !== "object" || value === null || !("answered" in value) ||
      typeof value.answered !== "number" || !Number.isSafeInteger(value.answered) || value.answered < 0 ||
      !("approximate" in value) || typeof value.approximate !== "boolean") return current;
  // An initial cached fetch can finish after a fresh total arrives in SSE.
  if (current && current.answered > value.answered) return current;
  return { answered: value.answered, approximate: value.approximate };
}

export async function fetchAnswerCount(
  apiBase: string,
  fetcher: typeof fetch = fetch,
): Promise<AnswerCount | null> {
  try {
    const response = await fetcher(`${apiBase.replace(/\/+$/, "")}/v1/answer-count`, {
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(2500),
    });
    return response.ok ? mergeAnswerCount(null, await response.json()) : null;
  } catch {
    // Optional statistic: no error bubble, retries, fake zero, or blocked chat.
    return null;
  }
}
