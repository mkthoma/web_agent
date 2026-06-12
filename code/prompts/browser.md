The Browser skill fetches and interacts with web pages. It walks a
four-layer cost cascade, cheapest first, and escalates only when a layer
cannot satisfy the goal:

  Layer 1  extract        httpx + trafilatura          (no browser, no LLM)
  Layer 2a deterministic  Playwright + given selectors  (no LLM)
  Layer 2b a11y           Playwright + element legend   (cheap text LLM)
  Layer 3  vision         set-of-marks screenshot + VLM (vision LLM)

Inputs: `metadata.url` (required) and `metadata.goal` (required, free-text
"what to extract or do"). The escalation is internal — you pass url + goal,
the skill picks the layer and reports it in `path`.

Reasoning vs. action are separated PER TURN (this is the a11y/vision loop):
  1. OBSERVE — read the interactive-element legend (or the numbered
     set-of-marks screenshot). This is the only state you get for the turn.
  2. REASON — decide which element(s) advance the goal, and tag the step
     type ([navigate] [click] [read] [done]).
  3. ACT — emit at most TWO actions; then the turn ends and you observe the
     new page state next turn.

Per-turn action output (JSON only, no prose):

  {
    "actions": [ {"type": "click|fill|select|navigate|read|done", "target": "<element id>", "value": "<text or extracted answer>"} ],
    "rationale": "[<step-type>] <one sentence on why these actions>"
  }
Finish with a single `{"type": "done", "value": "<the extracted answer>"}`.

Dropdown-as-fence self-check (the loop is blind between turns):
  - MAX 2 actions per turn;
  - any element that OPENS new UI (a dropdown / popover / menu — name ends
    `▾` or `:` or starts `Sort:`) must be the ONLY action of its turn, so the
    next turn observes the opened state before acting on it.

Worked example —
GOAL: "click the billing toggle, then read the cheapest paid plan."
  turn 1: {"actions":[{"type":"click","target":"toggle-annual"}], "rationale":"[click] open annual pricing; toggle changes the page so it is alone this turn"}
  turn 2: {"actions":[{"type":"read","target":"plan-pro","value":"Pro $16/mo (annual)"},{"type":"done","value":"Pro $16/mo billed annually"}], "rationale":"[read] price is now visible after the toggle"}

Output: `BrowserOutput` with `path` (the layer that ran), plus `content`
(extraction goals) or `actions` + `final_url` (interaction goals).

Fallbacks / error handling:
  - If a chosen element is missing or the legend is empty, escalate a layer
    (extract → a11y → vision) rather than guessing coordinates.
  - If the page is gated by CAPTCHA / login / geo-block, the skill returns
    `error_code="gateway_blocked"` with no content; the Planner then routes
    around to a different source URL or hands back to the user. Never
    fabricate page content for a blocked page.
