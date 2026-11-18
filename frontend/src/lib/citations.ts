/**
 * Which sources an answer actually cites.
 *
 * The service streams every passage it gave the answer model as a citation before the
 * answer text: up to seven for a narrow question, up to thirty for a broad one. The widget
 * lists only the sources the text cites, so a long answer is not followed by a long list of
 * sources it never used. An answer that cites nothing shows no list.
 *
 * Markers are the model's own "[S3]", occasionally "[S2, S5]" or a range "[S2–S4]". A marker
 * can arrive split across stream chunks, so callers recompute from the whole text as it grows.
 */

const BRACKETED = /\[([^[\]\n]{1,80})\]/g;
const MARKER = /\bS(\d{1,3})(?:\s*[–-]\s*S?(\d{1,3}))?\b/g;
/** A wider "range" is more likely a typo than a citation of every source in it. */
const MAX_RANGE = 40;

export function citedSourceIds(text: string): Set<string> {
  const ids = new Set<string>();
  for (const group of text.matchAll(BRACKETED)) {
    for (const marker of group[1].matchAll(MARKER)) {
      const start = Number(marker[1]);
      const end = marker[2] === undefined ? start : Number(marker[2]);
      const last = end >= start && end - start <= MAX_RANGE ? end : start;
      for (let number = start; number <= last; number += 1) ids.add(`S${number}`);
    }
  }
  return ids;
}

export function citedSources<T extends { id: string }>(citations: readonly T[], text: string): T[] {
  const ids = citedSourceIds(text);
  return citations.filter((citation) => ids.has(citation.id));
}
