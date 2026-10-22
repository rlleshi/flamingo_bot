# Widget integration

The deployed service root (`https://BOT_HOST/`) is a standalone version of the
chatbot. It uses the same custom element and API as embedded installations and
opens the assistant automatically. Use that page for direct sharing and smoke
testing; `/widget/flamingo-chat.js` is an integration asset rather than a
human-facing page.

The production container serves the custom element bundle and its optimized
avatar media from `/widget`. A host page needs one module script and one element:

```html
<script type="module" src="https://BOT_HOST/widget/flamingo-chat.js"></script>
<flamingo-chat></flamingo-chat>
```

When the bundle and API share an origin, `api-base` is optional. For a separate
API origin or a local preview, configure it explicitly:

```html
<flamingo-chat
  api-base="https://BOT_API_HOST"
  button-label="Ask Flamingo"
></flamingo-chat>
```

`asset-base` can point at a separately hosted media directory. By default it is
the `media/` directory beside the JavaScript bundle.

Add every real host origin to `FLAMINGO_ALLOWED_ORIGINS` without wildcarding.
The backend applies both CORS and a server-side origin guard, but that is not an
authentication mechanism; public-endpoint rate and concurrency limits still
apply.

Visitors share a metered service. Each one may ask 10 questions an hour, and the
service answers at most 100 questions a day in total, so an embedded widget can
refuse for a reason that has nothing to do with the visitor in front of it. The
widget distinguishes the cases already: a visitor who is out for the hour is told
to come back later, and one refused by the daily budget is told to come back
tomorrow rather than to retry. Operators tune both numbers from repository
variables; see `docs/runbooks/gcp-deployment.md`.

If the host uses Content Security Policy, allow `BOT_HOST` in `script-src`,
`connect-src`, and `media-src`. The host may need to add the module script's
origin to `default-src` as well when it does not declare those directives
separately. Keep the policy narrowly host-based; the widget does not require
`unsafe-inline` or `unsafe-eval`.

The widget uses a shadow root, works at phone width, traps focus while open,
closes with Escape, exposes streamed text through a live region, validates
source-link protocols, and renders the static poster when the visitor prefers
reduced motion. The video element is mounted only while the dialog is open.

An answer is rendered as paragraphs, bullet and numbered lists, bold, and
italic. That subset is fixed by the model prompt and by
`frontend/src/lib/richtext.ts`, which must be changed together. Parsing produces
a node tree that becomes real elements; no model or evidence text ever reaches
`innerHTML`. Anything outside the subset, a heading or a Markdown link for
example, stays on screen as the literal characters the model wrote, which is the
signal that the two sides have drifted apart.

## Conversation memory on the host page

The widget keeps the conversation in the page it is embedded in and sends the
recent turns in the `history` field of each `POST /v1/chat` body. Nothing is
written to `localStorage`, `sessionStorage`, or cookies, so the conversation
ends when the visitor reloads, navigates away, or uses the widget's new-chat
control. A host page needs no configuration or consent banner for this, and a
host that navigates between documents rather than routing in place will simply
start a new conversation on each page.

The service applies its own caps regardless of what a client sends. If an
operator needs to turn the feature off for a deployment, set
`FLAMINGO_HISTORY_TURNS=0`; the widget keeps displaying the transcript while the
backend answers each question independently again.

## Suggested questions

An empty chat shows four distinct questions in a 2×2 grid, randomly selected from nineteen
curated, source-tested topics. They disappear after the first question and
return after **New chat**. Clicking one submits it through the normal chat and quota path.
When optional aggregate feedback is enabled, a visible display and a click also
send the predefined suggestion IDs to the counter endpoint. The chip uses a short
label, while the full submitted question is visible in the conversation.

Event-specific starters include explicit dates in the submitted questions.

The host document's `<html lang>` selects English for `en`/`en-*` and Albanian
otherwise. Set that attribute on an embedded host page; the standalone page
already uses English. English suggestions explicitly request an English answer
because the current model sometimes follows the language of retrieved Albanian
sources instead of the visitor's question. Edit the bilingual bank in
`frontend/src/lib/suggestions.ts` only after checking the proposed questions
against the live corpus.

## Aggregate feedback and ratings

Four suggestion IDs are counted once when their grid is at least 99% visible
in an active tab, not on component updates. A click counts selection intent,
including when the subsequent answer fails. Completed, nonempty assistant
answers offer **Helpful / Not helpful** (localized in Albanian). Welcome,
failed, cancelled, and streaming answers cannot be rated. Each bubble permits
one rating in browser memory; refreshing or opening another browser is not
deduplicated because no persistent visitor identity is collected.

The counter request contains only a fixed kind and allowlisted suggestion IDs
or a binary rating. Ratings are overall totals, never linked to a question,
answer, conversation, or request ID. No freely typed questions are analyzed for
analytics. There are no analytics cookies, fingerprints, raw interaction records,
or third-party analytics service. Do Not Track / Global Privacy Control disable
this collection. Feedback failures do not block chat or trigger retries.

`/privacy` is a standalone bilingual privacy page linked both in the widget and
on its standalone landing page. It discloses suggestion counters, voluntary
ratings, authorized operator access, rolling retention, deletion limitations,
and the separate operational/security processing required to serve the bot.

Daily UTC aggregates live in `flamingo-quotas/widget_metrics`, never in the
knowledge corpus. A fixed expiry is set to UTC day-start plus 90 days, so later
reports do not extend older data. Expired buckets disappear from private reports
immediately; physical Firestore TTL deletion is asynchronous. Reports group the
remaining totals by week and include per-suggestion exposure, clicks, and
click-through rate. These are approximate counts, not unique people, causal
preferences, or tamper-proof votes.

Production collection defaults off until TTL and request-log exclusions are
configured; see [deployment setup](gcp-deployment.md#3b-aggregate-widget-feedback).
For local testing, set `FLAMINGO_WIDGET_METRICS_ENABLED=true`. An injected test
service bundle uses in-memory aggregates; the normal app uses the quota Firestore
database with the existing runtime identity.

Authorized operators can read the private report without a public dashboard:

```bash
uv run python scripts/report_widget_metrics.py
```

Do not commit report output or export expired/raw records. Disabling collection
does not delete unexpired totals; they age out through TTL. To remove all totals
early, an authorized operator can delete only the `widget_metrics` collection
in the quota database after confirming that exact target, without touching
`request_quotas` or the corpus.

## Public lifetime answer total

The compact counter under the composer shows an approximate lifetime number of
completed replies, localized to Albanian or English. All embeds and the
standalone page read the same production aggregate. It is not a count of unique
visitors, conversations, HTTP requests, widget loads, or individual model calls.
Follow-ups count once; replaying earlier conversation turns does not recount them.
A completed no-evidence reply counts too. Empty, failed, and cancelled streams
are not intentionally counted. A disconnect after generation/counting can still
prevent the final completion event from reaching the browser: generation, not a
proof of human receipt, is the completion boundary.

`GET /v1/answer-count` exposes only `answered` and `approximate`. The backend caches
reads for 45 seconds per process (including missing/unavailable records) and the
HTTP response for 15 seconds, so another instance's display can lag by about one
minute. Opening the widget fetches this value. A successful answer carries the
authoritative new total in its SSE `done.answer_count` payload; the browser does
not add one locally. An older cached response cannot roll the display back.
There is no polling, subscription, browser-side Firestore access, public write
endpoint, per-answer history, or extra model call.

The runtime atomically increments `flamingo-quotas/service_totals/answers` only
after a nonempty stream finishes. Production candidate-tag URLs are excluded;
manual checks on the canonical production URL count like any other completed
reply. Local development and injected test providers use memory and cannot alter
the production total. Optional suggestion/rating settings and DNT/GPC do not
control this anonymous server-side service total.

Storage retains one scalar plus baseline/initialization metadata for the life
of the service, without `expires_at`; the separate feedback collection still
expires after 90 days. Provider operations are bounded, writes disable SDK
retries, and a counter failure never turns a completed answer into a chat error.
An ambiguous write may have committed; it must not be retried blindly. The total
is intentionally approximate, not an exactly-once or billing-grade ledger.
Missing or invalid totals are hidden rather than fabricated as zero. The bilingual
privacy page discloses the purpose, public visibility, retention and controls.

See [one-time initialization and release controls](gcp-deployment.md#3c-lifetime-answer-total).

## Name and disclosure

The assistant is called **Diella - Flamingo Style**. The name deliberately
references the Albanian government's own AI program, which makes the disclosure
load-bearing rather than decorative: a visitor must not be able to mistake this
for the state service it comments on.

The visible Albanian disclosure identifies Diella 2.0 as a parody of the
government's Diella and an independent Flamingo Revolution project, not a state
service. It also explains that messages are sent for processing while the chat
history remains in the visitor's browser and is not stored on our server. Do
not remove it, move it below the fold, or embed the widget on a page that
presents it as official.

The welcome message is bilingual, Albanian first, which is also the first signal
to a visitor that they may write in their own language. Suggested-question chips
follow the host language; the other widget controls remain in English.

The name is long enough to wrap in the panel header. It is typeset to break
after the hyphen, and the header was verified at 1280, 390, and 320 pixels wide.
Keep the non-breaking space in the title if you edit that markup.
