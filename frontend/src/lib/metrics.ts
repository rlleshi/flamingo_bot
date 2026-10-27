/** Optional, best-effort totals. Never send message text or a visitor identifier. */
export type AnswerRating = "helpful" | "not_helpful";
export type WidgetMetric =
  | { kind: "shown"; suggestion_ids: string[] }
  | { kind: "clicked"; suggestion_ids: [string] }
  | { kind: "rated"; rating: AnswerRating };

export class WidgetMetrics {
  private apiBase: string;
  private fetcher: typeof fetch;
  private ready: Promise<boolean> | null = null;

  // Native browser fetch must be called with the global object as its receiver.
  constructor(apiBase: string, fetcher: typeof fetch = (...args) => globalThis.fetch(...args)) {
    this.apiBase = apiBase.replace(/\/+$/, "");
    this.fetcher = fetcher;
  }

  initialize(): Promise<boolean> {
    if (this.ready) return this.ready;
    // Respect browser-level privacy preferences without adding an opt-out cookie.
    const privacy = navigator as Navigator & { globalPrivacyControl?: boolean };
    if (privacy.doNotTrack === "1" || privacy.globalPrivacyControl === true) {
      this.ready = Promise.resolve(false);
      return this.ready;
    }
    this.ready = this.fetcher(`${this.apiBase}/v1/widget-config`, {
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(2500),
    }).then(async (response) => {
      if (!response.ok) return false;
      const config: unknown = await response.json();
      return typeof config === "object" && config !== null &&
        "metrics_enabled" in config && config.metrics_enabled === true;
    }).catch(() => false);
    return this.ready;
  }

  async record(metric: WidgetMetric): Promise<void> {
    if (!await this.initialize()) return;
    try {
      await this.fetcher(`${this.apiBase}/v1/widget-metrics`, {
        method: "POST",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(metric),
        keepalive: true,
        signal: AbortSignal.timeout(2500),
      });
    } catch {
      // No retries or diagnostics containing interaction data. Chat still works.
    }
  }
}

/** Count one visible display, not component updates, hidden tabs, or mounting. */
export function observeSuggestionDisplay(element: HTMLElement, report: () => void): () => void {
  if (typeof IntersectionObserver === "undefined") return () => {};
  let visible = false;
  let reported = false;
  const maybeReport = () => {
    if (reported || !visible || document.visibilityState !== "visible") return;
    reported = true;
    report();
  };
  const observer = new IntersectionObserver((entries) => {
    visible = entries.some((entry) => entry.isIntersecting && entry.intersectionRatio >= 0.99);
    maybeReport();
  }, { threshold: [0.99] });
  observer.observe(element);
  document.addEventListener("visibilitychange", maybeReport);
  return () => {
    observer.disconnect();
    document.removeEventListener("visibilitychange", maybeReport);
  };
}
