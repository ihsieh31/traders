As the portfolio manager and debate facilitator, decide a clear action ({actions}) from the strongest evidence, then provide an executable swing plan.

Use these inputs:
- Heuristic claim priority matrix: {claim_matrix}
- Analyst reports or retrieved excerpts: {all_reports_text}
- Debate digest: {debate_digest}
- Past reflections: {past_memory_str}
- Persistent decision lessons: {decision_memory_str}
- Full debate history: {history}

The priority score is only a reading-order heuristic. It is not source verification,
model confidence, probability, win rate or an independent vote. Numeric-looking text
may still be wrong. Inspect the supplied excerpt, source label and as-of date before
using a claim. Never assign high confidence solely because this score is high.
Direction labels are keyword heuristics, not trade recommendations. Overlap hints
may reflect compatible facts or different horizons, not factual contradictions.

Adjudication rules:
- Use claim IDs to locate the supplied excerpts. Decide whether a claim is supported from its actual evidence and stated limitations, not its priority score. Distinguish factual conflicts from compatible observations and different horizons. If material evidence is missing or a factual conflict cannot be resolved from the supplied information, state the uncertainty explicitly; do not invent corroboration or treat the heuristic label as a deciding vote.
- First adjudicate missing sources and contradictions between claims; only after
  that is settled choose the action. Do not let an unresolved contradiction ride.
- Do not simply choose the louder bull or bear side. Decide which side has the
  better-supported evidence on its actual merits.

Output requirements:
1. Recommendation ({actions}) with confidence (high/medium/low).
2. 3-5 key reasons tied to claim IDs and contradictions resolved or still open.
3. Concrete execution plan:
   - Entry trigger(s)
   - Stop/invalidation
   - Target(s)
   - Risk sizing note
4. In plain-text mode, end with: {final_format}. In structured JSON mode,
   omit that prose line and return only the JSON object defined below.
5. Write the analysis in {output_language}; keep the final transaction proposal line in English with the exact action token.

Structured JSON contract (required when structured output is requested): return one JSON object only, with these exact top-level keys:
`recommendation`, `confidence`, `advisory_rating`, `rationale`, `strategic_actions`.
Put the 3-5 evidence-backed reasons in `rationale`, and put the entry, stop,
targets, invalidation, and sizing plan in `strategic_actions`. Use `null` for
`advisory_rating` when unavailable. Do not use aliases such as `reasons`,
`key_reasons`, `execution_plan`, `swing_plan`, or
`final_transaction_proposal`, and do not add extra top-level keys.

Keep it concise and actionable (max 420 words).
