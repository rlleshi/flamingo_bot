# Implementation status

- Baseline evidence date: 2026-08-23 (conversation memory added the same day)
- Retrieval/model update: 2026-10-18, verified locally; this update is not deployed.
- Retrieval 3.1.0 and prompt 1.9.0 (in-memory search, keyword search, broad-question
  retrieval searched inside the named period, summary records, no-private-names
  instruction, cited-only sources): 2026-11-18, not deployed. Given the same model
  outputs, retrieval 3.0.0 reproduced the evaluated candidate pools exactly for all 152
  evaluation questions. The 3.1.0 period filter keeps 128 of them identical (every
  narrow question and every broad one without a period); the 24 broad questions with a
  period no longer hold passages dated outside it (199 before). One end-to-end sample
  with live models conveyed 54% of essential facts on 116 questions and 71% on 36
  independently written ones, against 56% and 78% for the evaluated run and 35% and 44%
  for the current release; broad-question scores vary widely between samples. The
  search-snapshot write has run only against test fakes; its first real run is the
  next publication.
- Design: [`plans/2026-08-23-flamingo-rag-chatbot-design.md`](plans/2026-08-23-flamingo-rag-chatbot-design.md)
- Overall state: dedicated Firestore data plane, localhost application, and
  production Cloud Run release are verified end to end; host-site integration
  and owner content disposition remain

This document distinguishes executable local evidence from mutable cloud state.
It is not a production-readiness declaration.
The dated baseline below is historical; current retrieval verification is recorded
in the [evaluation runbook](runbooks/evaluation.md#local-verification-2026-10-18).

| Design point | Current evidence | Remaining gate |
| --- | --- | --- |
| 1. Objective and boundaries | Grounded prompt, widget disclosure, source-only refusal, and legal-status evaluation cases are implemented. A content-level agent review found the live rhetoric, affiliation disclosure, and legal-status handling consistent with the contract. | Owner human review and disposition before release; re-review after material source or prompt changes. |
| 2. Stack and boundaries | Python 3.12/FastAPI backend, direct Azure SDK adapters, live Frankfurt Firestore, Svelte custom element, Docker image, and Frankfurt Cloud Run service exist. No LangChain, LangGraph, or ChromaDB is present. | Integrate the deployed widget into staging copies of the approved host sites. |
| 3. Sources and provenance | Three local Git repositories are allowlisted. Parsers retain repository, path/record, revision, hash, URL, language, type, title, and legal metadata; known duplicates and presentation-only files are excluded. | Review future source-schema changes before publishing. |
| 4. Ingestion workflow | Publish activated `20260823T094238Z-002c3ce5269a` with 884 chunks. The pre-deployment dry-run found 44 files, 98 documents, zero failures/skips, zero additions/removals, and all 884 embeddings reusable, so no redundant generation was published. | Run the same reviewed workflow after future source updates. |
| 5. Parsing and chunking | Format-specific fixtures and deterministic semantic chunk tests pass. Current chunks average about 530 tokens, max 677, with none above the 700-token ceiling. Live retrieval evaluations passed over this corpus. | Review future parser/source-schema changes. |
| 6. Embeddings and diff | Azure `text-embedding-3-large`, 1,024 dimensions, batching, contract-version rebuilds, hash reuse, and duplicate-aware removals are implemented. The first publish embedded 850 chunks; adding the participation parser re-embedded only the 51 chunks it changed and reused the other 833. | Monitor subsequent incremental counts and Azure usage. |
| 7. Firestore publication | Firestore Native `flamingo-rag` is live in `europe-west3` with delete protection. Both flat 1,024-dimensional indexes are `READY`; the active generation passed count, source-coverage, and per-source vector checks. One pre-activation serialization failure is retained as a `failed` audit record and never changed the pointer. | Add a second retained valid generation before exercising live rollback. |
| 8. Retrieval and model roles | Every question receives a typed `gpt-6-luna` plan and, in parallel, a `gpt-6-luna` broad/narrow scope, both with no reasoning. Each instance loads the active generation into memory (from the packed snapshot, or from chunk documents for older generations) and searches it with exact vectors, BM25 keywords and bounded exact anchors, fused per record; one request uses one generation. Narrow questions: an independent `gpt-5.6-luna` reranker selects at most seven passages from at most 20 candidates. Broad questions: written queries, a follow-up search, Pulsi day notes and a month-by-month pool of 60, of which the reranker selects up to 30; a named period limits both searches before ranking (events overlapping it, reports up to two days after it, undated records), widening to its whole months when it holds fewer than five dated records. `gpt-6.1-sol` with low reasoning streams the answer (3,000 output tokens for broad questions). On 2026-11-18 the implementation reproduced the evaluated candidate pools exactly for all 152 evaluation questions given the same model outputs; with the period filter, 128 still do and the 24 with a named period changed as intended. | Deploy this runtime update separately; publish once so the active generation has a search snapshot. |
| 9. API operations | `/health`, `/status`, SSE `/v1/chat`, request bounds, origin guard, CORS, concurrency/rate limits, safe error events, hashed safety ID, timeouts, and non-content operational logs exist. Production chat requires an allowed Origin, and rate limiting uses the proxy-appended client address rather than the load-balancer address or an untrusted prefix. The service stores no conversation. | Monitor production telemetry and revisit edge enforcement with Cloud Armor if public usage warrants a load balancer. |
| 10. Widget and avatar | Framework-independent responsive custom element, citations, keyboard/focus behavior, live region, cancellation, reduced-motion poster, and authorized silent 10-second MP4/WebM loop are built. It replays tab-local conversation turns, writes nothing to browser storage, and offers a new-conversation control. Answers are rendered as real elements, never through `innerHTML`. The same widget also powers a responsive standalone page at the service root and opens there automatically. | Integrate into staging copies of the real host sites and repeat browser review there. |
| 11. Tests and evaluation | 95 Python tests and 19 widget tests pass; strict mypy, Ruff, Svelte check, frontend build, local production-image build, exact no-change source diff, the workflow candidate smoke, the full twelve-case local and production evaluations, and browser review at desktop and phone widths have passed. | Record the owner's human content disposition and repeat browser review after host-site integration. |
| 12. Security, cost, deploy, rollback | The complete infrastructure verifier passes billing linkage, Frankfurt Firestore/indexes, Artifact Registry, Cloud Run/public invoker, all three regional secrets, and both custom roles. Runtime, ingestion, and keyless GitHub deployment identities are separate and least-privilege; WIF is restricted to immutable repository/owner IDs, `main`, `production`, and the exact workflow. Cloud Run scales from zero to at most five instances. A project-filtered gross-usage budget alerts before credits hide consumption. The protected workflow proved zero-traffic candidate smoke and promotion while retaining the prior revision. | Review the three pre-existing GitHub admin accounts when the owner is ready; monitor GCP and Azure usage separately. |

## Verified commands

```bash
uv run pytest
uv run ruff check .
uv run mypy src scripts
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run build
uv run python -m flamingo_bot.ingest --dry-run --pretty
uv run python scripts/run_evals.py
uv run python scripts/smoke_azure.py
uv run --env-file .env python scripts/verify_gcp_contract.py --pretty
/usr/bin/docker build -t flamingo-bot:local .
```
