# Prompt evaluation scores (prompt_evaluator.md rubric)

Every skill prompt was run through `prompt_evaluator.md` via the live gateway
(`POST /v1/chat`, the evaluator as the system prompt, the prompt under test as
the user message). **All 11 prompts score 8/8 true** on the eight boolean
criteria, with a positive `overall_clarity` verdict.

| Prompt | explicit_reasoning | structured_output | tool_separation | conversation_loop | instructional_framing | internal_self_checks | reasoning_type_awareness | fallbacks |
|--------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| planner.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| browser.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| researcher.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| retriever.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| distiller.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| comparator.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| summariser.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| critic.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| formatter.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| coder.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| sandbox_executor.md | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

## How each criterion is satisfied across the board

Every prompt now carries the same nine-criteria scaffold, adapted to its role:

- **Explicit reasoning** — a "Think step by step (internally) before emitting
  JSON" block with numbered reasoning stages. Reasoning is kept internal so the
  single-JSON-object contract (`parse_skill_json`) still holds.
- **Structured output** — an explicit JSON output schema, "JSON only, no prose,
  no markdown fences."
- **Tool separation** — each prompt states its tool surface ("two MCP tools" /
  "you make no tool calls") and the OBSERVE → REASON → ACT split for tool users.
- **Conversation loop** — notes on how FAILURE / prior-attempt / critic-recovery
  context in INPUTS is consumed and fed forward across turns.
- **Instructional framing** — a concrete worked example (input → output) in every
  prompt.
- **Internal self-checks** — a "Self-check before output" checklist (grounding,
  completeness, scoping, the dropdown-fence rule for browser, etc.).
- **Reasoning-type awareness** — a `reasoning_type` field and/or a `[type]`
  prefix on `rationale` (lookup / extraction / comparison / arithmetic / logic …).
- **Fallbacks** — explicit "If uncertain / tool fails / evidence missing → do X"
  handling (`(not found)`, `fields:{}`, `gateway_blocked` route-around, etc.).
- **Overall clarity** — short, sectioned, role-specific; judged "excellent /
  exceptionally well-structured" by the evaluator.

## Notes on functional safety

Reasoning lives in the `rationale` / `reasoning_type` fields and in internal
("silent") thought — never as free prose before the JSON — so the orchestrator's
single-object JSON parser and every downstream field reader
(`nodes`, `successors`, `fields`, `rows`, `final_answer`, `verdict`) are
unchanged. Extra keys like `reasoning_type` are ignored by downstream consumers.

The judge model occasionally returns malformed JSON on a given call (observed
once for `formatter.md` on an NVIDIA route); re-running on a strict-JSON model
returns a clean 8/8. Scores above reflect the stable result.
