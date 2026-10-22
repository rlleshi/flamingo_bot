import { describe, expect, it, vi } from "vitest";

import { fetchAnswerCount, mergeAnswerCount } from "./answerCount";

describe("public lifetime answer total", () => {
  it("fetches only an anonymous scalar using the configured API base", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ answered: 370, approximate: true })));
    expect(await fetchAnswerCount("https://bot.example/", fetcher)).toEqual({ answered: 370, approximate: true });
    expect(fetcher).toHaveBeenCalledOnce();
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe("https://bot.example/v1/answer-count");
    expect(options.credentials).toBe("omit");
    expect(options.referrerPolicy).toBe("no-referrer");
    expect(options.body).toBeUndefined();
  });

  it("never overwrites a fresh SSE total with an older cached fetch", () => {
    const fresh = { answered: 372, approximate: true };
    expect(mergeAnswerCount(fresh, { answered: 370, approximate: true })).toEqual(fresh);
    expect(mergeAnswerCount(fresh, { answered: 373, approximate: true })).toEqual({ answered: 373, approximate: true });
  });

  it.each([
    null, {}, { answered: null, approximate: true }, { answered: -1, approximate: true },
    { answered: 1.5, approximate: true }, { answered: "100", approximate: true },
    { answered: Number.MAX_SAFE_INTEGER + 1, approximate: true },
    { answered: 100 }, { answered: 100, approximate: "yes" },
  ])("ignores invalid, missing, or unsafe totals: %j", (value) => {
    expect(mergeAnswerCount(null, value)).toBeNull();
    expect(mergeAnswerCount({ answered: 370, approximate: true }, value)?.answered).toBe(370);
  });

  it("accepts a legitimate zero, not a fabricated fallback", () => {
    expect(mergeAnswerCount(null, { answered: 0, approximate: true })?.answered).toBe(0);
  });

  it("hides unavailable or malformed responses without retrying", async () => {
    for (const response of [new Response("error", { status: 503 }), new Response("bad json"),
      new Response('{"answered":null,"approximate":true}')]) {
      const fetcher = vi.fn().mockResolvedValue(response);
      expect(await fetchAnswerCount("https://bot.example", fetcher)).toBeNull();
      expect(fetcher).toHaveBeenCalledOnce();
    }
    const fetcher = vi.fn().mockRejectedValue(new Error("offline"));
    expect(await fetchAnswerCount("https://bot.example", fetcher)).toBeNull();
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
