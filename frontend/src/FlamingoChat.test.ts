import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "./FlamingoChat.svelte";

beforeEach(() => {
  document.documentElement.lang = "en";
  vi.stubGlobal("matchMedia", () => ({
    matches: true,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  vi.stubGlobal("IntersectionObserver", class {
    constructor(private callback: IntersectionObserverCallback) {}
    observe(target: Element) {
      this.callback([
        { target, isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry,
      ], this as unknown as IntersectionObserver);
    }
    disconnect() {}
  });
});

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function openWidget(answerFails = false, metricsEnabled = true, incompleteAnswer = false) {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/v1/widget-config")) return new Response(JSON.stringify({ metrics_enabled: metricsEnabled }));
    if (url.endsWith("/v1/widget-metrics")) return new Response(null, { status: 204 });
    if (url.endsWith("/v1/chat")) {
      if (answerFails) return new Response('{"detail":"server_busy"}', { status: 429 });
      return new Response('event: delta\ndata: {"text":"A completed answer."}\n\n' +
        (incompleteAnswer ? "" : 'event: done\ndata: {}\n\n'));
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetcher);
  const element = document.createElement("flamingo-chat");
  element.setAttribute("api-base", "https://bot.example");
  document.body.append(element);
  await vi.waitFor(() => expect(element.shadowRoot?.querySelector(".launcher")).not.toBeNull());
  (element.shadowRoot!.querySelector(".launcher") as HTMLButtonElement).click();
  await vi.waitFor(() => expect(element.shadowRoot?.querySelectorAll(".suggestions button")).toHaveLength(4));
  const root = element.shadowRoot!;
  const reports = () => fetcher.mock.calls
    .filter(([url]) => url.endsWith("/v1/widget-metrics"))
    .map(([, options]) => JSON.parse(String(options?.body)));
  return { root, reports };
}

describe("widget suggestion and rating UI", () => {
  it("shows four hints, records one display, then accepts a single rating per completed answer", async () => {
    const { root, reports } = await openWidget();
    await vi.waitFor(() => expect(reports().filter((report) => report.kind === "shown")).toHaveLength(1));
    expect(root.querySelectorAll(".answer-rating button")).toHaveLength(0);
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelectorAll(".answer-rating button")).toHaveLength(2));
    expect(root.querySelector(".suggestions")).toBeNull();
    const buttons = [...root.querySelectorAll<HTMLButtonElement>(".answer-rating button")];
    buttons[0].click();
    // A second synchronous activation is guarded before the DOM disables buttons.
    buttons[1].click();
    await vi.waitFor(() => expect(reports().filter((report) => report.kind === "rated")).toHaveLength(1));
    expect(reports().find((report) => report.kind === "rated")).toEqual({ kind: "rated", rating: "helpful" });
    expect(reports().filter((report) => report.kind === "clicked")).toHaveLength(1);
    expect(reports().filter((report) => report.kind === "shown")).toHaveLength(1);
    expect(buttons.every((button) => button.disabled)).toBe(true);
    expect(root.querySelector('a[href="https://bot.example/privacy"]')).not.toBeNull();
  });

  it("never offers ratings for failed answers", async () => {
    const { root } = await openWidget(true);
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".error")).not.toBeNull());
    expect(root.querySelectorAll(".answer-rating button")).toHaveLength(0);
  });

  it("keeps four suggestions and chat working when collection is disabled", async () => {
    const { root, reports } = await openWidget(false, false);
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".messages")?.textContent).toContain("A completed answer."));
    expect(root.querySelectorAll(".answer-rating button")).toHaveLength(0);
    expect(reports()).toEqual([]);
  });

  it("counts each newly opened empty grid but not unrelated UI updates", async () => {
    const { root, reports } = await openWidget();
    await vi.waitFor(() => expect(reports()).toHaveLength(1));
    (root.querySelector(".close") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".launcher")).not.toBeNull());
    (root.querySelector(".launcher") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(reports().filter((report) => report.kind === "shown")).toHaveLength(2));
  });

  it("does not offer a rating if a stream ends without its completion event", async () => {
    const { root } = await openWidget(false, true, true);
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".messages")?.textContent).toContain("A completed answer."));
    expect(root.querySelectorAll(".answer-rating button")).toHaveLength(0);
  });
});
