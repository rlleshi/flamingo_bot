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

async function openWidget(answerFails = false, metricsEnabled = true, incompleteAnswer = false,
  count: number | null = 370, countUnavailable = false) {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/v1/widget-config")) return new Response(JSON.stringify({ metrics_enabled: metricsEnabled }));
    if (url.endsWith("/v1/widget-metrics")) return new Response(null, { status: 204 });
    if (url.endsWith("/v1/answer-count")) return new Response(JSON.stringify({ answered: count, approximate: true }),
      { status: countUnavailable ? 503 : 200 });
    if (url.endsWith("/v1/chat")) {
      if (answerFails) return new Response('{"detail":"server_busy"}', { status: 429 });
      return new Response('event: delta\ndata: {"text":"A completed answer."}\n\n' +
        (incompleteAnswer ? "" : 'event: done\ndata: {"answer_count":{"answered":371,"approximate":true}}\n\n'));
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
  return { root, reports, fetcher };
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

describe("lifetime answer count UI", () => {
  it("loads on open and uses the authoritative completed-answer total without polling", async () => {
    const { root, fetcher } = await openWidget();
    await vi.waitFor(() => expect(root.querySelector(".answer-count")?.textContent).toContain("370"));
    expect(root.querySelector(".answer-count")?.textContent?.replace(/\s+/g, " ").trim())
      .toBe("Diella 2.0 has answered ≈370 questions so far.");
    expect(root.querySelector(".answer-count strong")?.textContent?.trim()).toBe("≈370");
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".answer-count")?.textContent).toContain("371"));
    expect(root.querySelector(".answer-count strong")?.textContent?.trim()).toBe("≈371");
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/v1/answer-count"))).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/v1/chat"))).toHaveLength(1);
  });

  it("localizes the compact label and keeps it independent of optional feedback", async () => {
    document.documentElement.lang = "sq";
    const { root, reports } = await openWidget(false, false);
    await vi.waitFor(() => expect(root.querySelector(".answer-count")?.textContent?.replace(/\s+/g, " ").trim())
      .toBe("Diella 2.0 ka dhënë ≈370 përgjigje deri tani."));
    expect(root.querySelector(".answer-count strong")?.textContent?.trim()).toBe("≈370");
    expect(root.querySelector(".answer-count")?.getAttribute("title")).toContain("Jo vizitorë unikë");
    expect(reports()).toEqual([]);
  });

  it("does not locally increment on a failed or incomplete answer", async () => {
    for (const [fails, incomplete] of [[true, false], [false, true]]) {
      const { root } = await openWidget(fails, true, incomplete);
      await vi.waitFor(() => expect(root.querySelector(".answer-count")?.textContent).toContain("370"));
      (root.querySelector(".suggestions button") as HTMLButtonElement).click();
      // The textarea is disabled only while the stream is active (the send
      // button also stays disabled for empty input, so it is not a completion signal).
      await vi.waitFor(() => expect(root.querySelector(".suggestions")).toBeNull());
      await vi.waitFor(() => expect(root.querySelector("textarea")?.hasAttribute("disabled")).toBe(false));
      expect(root.querySelector(".answer-count")?.textContent).not.toContain("371");
      document.body.replaceChildren();
    }
  });

  it("hides a missing/unavailable total while keeping chat usable", async () => {
    const { root } = await openWidget(false, false, false, null, true);
    expect(root.querySelector(".answer-count")).toBeNull();
    (root.querySelector(".suggestions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".messages")?.textContent).toContain("A completed answer."));
    await vi.waitFor(() => expect(root.querySelector(".answer-count")?.textContent).toContain("371"));
    expect(root.querySelector(".error")).toBeNull();
  });
});

describe("Flamingo speech-bubble launcher and pink halo", () => {
  function finishAnimation(target: Element, name = "svelte-test-launcher-bob", pseudoElement = "") {
    const event = new Event("animationend", { bubbles: true });
    Object.defineProperties(event, {
      animationName: { value: name },
      pseudoElement: { value: pseudoElement },
    });
    target.dispatchEvent(event);
  }

  async function mountLauncher(reducedMotion: boolean) {
    const changeListeners: ((event: { matches: boolean }) => void)[] = [];
    const removeMotionListener = vi.fn();
    vi.stubGlobal("matchMedia", () => ({
      matches: reducedMotion,
      addEventListener: (_: string, listener: (event: { matches: boolean }) => void) => {
        changeListeners.push(listener);
      },
      removeEventListener: removeMotionListener,
    }));
    // Opening the preview is local-only, with optional feedback disabled.
    const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(
      url.endsWith("/v1/widget-config") ? { metrics_enabled: false } : { answered: null, approximate: true },
    )));
    vi.stubGlobal("fetch", fetcher);
    const element = document.createElement("flamingo-chat");
    element.setAttribute("button-label", "Pyet Flamingon");
    element.setAttribute("api-base", "https://bot.example");
    // Match Diaspora's red theme override. The halo/logo retain their own pink.
    element.style.setProperty("--flamingo-accent", "#b91c1c");
    document.body.append(element);
    await vi.waitFor(() => expect(element.shadowRoot?.querySelector(".launcher")).not.toBeNull());
    const root = element.shadowRoot!;
    const launcher = root.querySelector<HTMLButtonElement>(".launcher")!;
    return { element, root, launcher, changeListeners, removeMotionListener, fetcher };
  }

  it("uses the project vector mark, preserves its accessible host label and makes no initial API call", async () => {
    const { launcher, fetcher } = await mountLauncher(true);
    expect(launcher.getAttribute("aria-label")).toBe("Pyet Flamingon");
    expect(launcher.getAttribute("aria-haspopup")).toBe("dialog");
    expect(launcher.type).toBe("button");
    const mark = launcher.querySelector<HTMLElement>(".mark")!;
    expect(mark.getAttribute("aria-hidden")).toBe("true");
    expect(mark.textContent).toBe("");
    expect(mark.style.backgroundImage, mark.outerHTML).toMatch(/^url\(/);
    expect(launcher.querySelector(".launcher-label")?.textContent).toBe("Pyet Flamingon");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("finishes its bounded attention animation and can replay on deliberate hover or keyboard focus", async () => {
    const { launcher } = await mountLauncher(false);
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
    finishAnimation(launcher);
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(false));
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
    finishAnimation(launcher, "launcher-bob");
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(false));
    launcher.focus();
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
  });

  it("does not let the shorter sheen, pseudo-elements, or children end the two bobs early", async () => {
    const { launcher } = await mountLauncher(false);
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
    finishAnimation(launcher, "svelte-test-launcher-sheen", "::after");
    finishAnimation(launcher.querySelector(".mark")!);
    finishAnimation(launcher, "launcher-bob", "::before");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(launcher.classList.contains("attention")).toBe(true);
    finishAnimation(launcher);
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(false));
  });

  it("never starts motion for reduced motion and responds when the preference changes", async () => {
    const { launcher, changeListeners } = await mountLauncher(true);
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    launcher.focus();
    expect(launcher.classList.contains("attention")).toBe(false);
    changeListeners[0]({ matches: false });
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
    changeListeners[0]({ matches: true });
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(false));
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    expect(launcher.classList.contains("attention")).toBe(false);
  });

  it("stops in a hidden tab and does not automatically restart on unrelated updates", async () => {
    const { launcher } = await mountLauncher(false);
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(false));
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    expect(launcher.classList.contains("attention")).toBe(false);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(launcher.classList.contains("attention")).toBe(false);
    launcher.dispatchEvent(new MouseEvent("mouseenter"));
    await vi.waitFor(() => expect(launcher.classList.contains("attention")).toBe(true));
  });

  it("keeps opening/closing and focus restoration working, and removes listeners on unmount", async () => {
    const removeListener = vi.spyOn(document, "removeEventListener");
    const { element, root, launcher, removeMotionListener } = await mountLauncher(false);
    launcher.click();
    await vi.waitFor(() => expect(root.querySelector("dialog")).not.toBeNull());
    expect(root.querySelector(".launcher")).toBeNull();
    (root.querySelector(".close") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.activeElement).toBe(root.querySelector(".launcher")));
    expect(root.querySelector(".launcher")?.classList.contains("attention")).toBe(false);
    element.remove();
    await vi.waitFor(() => expect(removeMotionListener).toHaveBeenCalledOnce());
    expect(removeListener.mock.calls.some(([name]) => name === "visibilitychange")).toBe(true);
  });
});
