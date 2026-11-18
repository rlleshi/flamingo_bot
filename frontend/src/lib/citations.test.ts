import { describe, expect, it } from "vitest";

import { citedSourceIds, citedSources } from "./citations";

const sources = Array.from({ length: 30 }, (_, index) => ({ id: `S${index + 1}` }));

describe("citedSourceIds", () => {
  it("reads single markers and keeps S1 and S12 apart", () => {
    expect([...citedSourceIds("Police used tear gas [S12]. A rally followed [S1].")]).toEqual([
      "S12",
      "S1",
    ]);
  });

  it("reads lists and ranges inside one bracket", () => {
    expect([...citedSourceIds("Figures differ [S2, S5; S7] and [S3–S5] and [S9-S10].")].sort()).toEqual(
      ["S10", "S2", "S3", "S4", "S5", "S7", "S9"],
    );
  });

  it("ignores brackets that are not markers and absurd ranges", () => {
    expect([...citedSourceIds("[Shënim] [S] [SS3] [ABS4] text [S2–S900]")]).toEqual(["S2"]);
  });

  it("finds nothing in an answer without markers", () => {
    expect(citedSourceIds("I could not find enough relevant evidence.").size).toBe(0);
  });
});

describe("citedSources", () => {
  it("keeps only cited sources, in the service's order", () => {
    const shown = citedSources(sources, "Overview [S14] … earlier [S3] and [S14].");
    expect(shown.map((source) => source.id)).toEqual(["S3", "S14"]);
  });

  it("shows a source only once its marker is complete while streaming", () => {
    expect(citedSources(sources, "Police used tear gas [S1").map((s) => s.id)).toEqual([]);
    expect(citedSources(sources, "Police used tear gas [S12]").map((s) => s.id)).toEqual(["S12"]);
  });
});
