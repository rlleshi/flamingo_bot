import { afterEach, describe, expect, it, vi } from "vitest";

import { observeSuggestionDisplay, WidgetMetrics } from "./metrics";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("aggregate widget feedback", () => {
  it("sends only predefined counters without cookies or referrers", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response('{"metrics_enabled":true}'))
      .mockResolvedValue(new Response(null, { status: 204 }));
    const metrics = new WidgetMetrics("https://bot.example/", fetcher);
    await metrics.record({ kind: "shown", suggestion_ids: ["movement", "diaspora", "pulsi", "dossier"] });
    await metrics.record({ kind: "clicked", suggestion_ids: ["movement"] });
    await metrics.record({ kind: "rated", rating: "helpful" });
    expect(fetcher).toHaveBeenCalledTimes(4);
    for (const [url, options] of fetcher.mock.calls) {
      expect(url).not.toContain("?");
      expect(options.credentials).toBe("omit");
      expect(options.referrerPolicy).toBe("no-referrer");
    }
    const [, options] = fetcher.mock.calls[3];
    expect(JSON.parse(options.body)).toEqual({ kind: "rated", rating: "helpful" });
    expect(options.body).not.toMatch(/answer_id|request_id|session|visitor|question|timestamp/);
  });

  it("does not send counters when disabled", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"metrics_enabled":false}'));
    const metrics = new WidgetMetrics("https://bot.example", fetcher);
    await metrics.record({ kind: "rated", rating: "helpful" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each(["doNotTrack", "globalPrivacyControl"])("respects %s without any network call", async (key) => {
    vi.stubGlobal("navigator", { [key]: key === "doNotTrack" ? "1" : true });
    const fetcher = vi.fn();
    const metrics = new WidgetMetrics("https://bot.example", fetcher);
    await metrics.record({ kind: "rated", rating: "helpful" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not throw or retry failed feedback", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response('{"metrics_enabled":true}'))
      .mockRejectedValue(new Error("offline"));
    const metrics = new WidgetMetrics("https://bot.example", fetcher);
    await expect(metrics.record({ kind: "rated", rating: "not_helpful" })).resolves.toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("does not send feedback after config initialization fails", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("offline"));
    const metrics = new WidgetMetrics("https://bot.example", fetcher);
    await expect(metrics.record({ kind: "rated", rating: "helpful" })).resolves.toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("counts a visible suggestion display once and cleans up observers", () => {
    let notify: IntersectionObserverCallback = () => {};
    const disconnect = vi.fn();
    vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: IntersectionObserverCallback) { notify = callback; }
      observe = vi.fn();
      disconnect = disconnect;
    });
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const report = vi.fn();
    const dispose = observeSuggestionDisplay(document.createElement("div"), report);
    const entry = { isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry;
    notify([entry], {} as IntersectionObserver);
    notify([entry], {} as IntersectionObserver);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(report).toHaveBeenCalledTimes(1);
    dispose();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("waits for both intersection and an active tab before reporting", () => {
    let notify: IntersectionObserverCallback = () => {};
    vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: IntersectionObserverCallback) { notify = callback; }
      observe() {}
      disconnect() {}
    });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const report = vi.fn();
    const dispose = observeSuggestionDisplay(document.createElement("div"), report);
    notify([{ isIntersecting: true, intersectionRatio: 0.1 } as IntersectionObserverEntry], {} as IntersectionObserver);
    expect(report).not.toHaveBeenCalled();
    notify([{ isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry], {} as IntersectionObserver);
    expect(report).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(report).toHaveBeenCalledOnce();
    dispose();
  });
});
