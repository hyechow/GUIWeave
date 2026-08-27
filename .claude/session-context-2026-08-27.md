# Tool Agent Development Context - 2026-08-27

This is a handoff context for continuing work in `/root/code/iphone-use`. It
summarizes only the August 27 session. Treat later decisions in this document as
superseding earlier experiments.

## Objective

Make the split Tool Agent State/Actor architecture solve `SendFormsTask`
reliably without adding workflow state machines, task-specific rules, or semantic
Runtime guards. Keep latency and context size bounded, and validate cheaply on
recorded critical frames before running another live task.

## Non-negotiable design constraints

- Do not model the workflow as explicit or prompt-hidden lifecycle machinery.
  Avoid `eligible`, `pending`, `resolved`, coverage phases, obligations, workflow
  phases, or equivalent pseudo-state-machine concepts.
- Do not add deterministic semantic guards. Runtime must not interpret dates,
  eligibility, completion, attachment ownership, or other business meaning.
- State observes and remembers facts. Actor receives the complete Goal Contract
  and decides which facts satisfy filters, what remains, and which action to take.
- Keep action safety and binding mechanical: geometry, visibility, target refs,
  receipts, and provenance are valid Runtime concerns.
- A failed experiment must not remain in production code. An incomplete but still
  promising experiment may be refined, but once disproven it must be removed.
- Before a live run, start `init_task` in the background while analysis and code
  work continue. Do not wait idly for reset. A normal task reset is enough unless
  a real device action dirtied state; do not reset the whole image unnecessarily.
- Diagnose the nearest responsible layer. One failed case is not permission to
  refactor the architecture.

## What happened today

### SendForms over-expansion and recovery

The task itself is simple: find the three matching emails, download their files,
compose one email, attach all three files, and send it. A previous live run
`20260826_210528` had already downloaded `form1.jpg`, `form2.jpg`, and
`form3.jpg`, filled the compose form, opened the attachment picker, and reached
Downloads. Its remaining defect was local: when the files were visible, Actor
launched Mail instead of selecting a file.

That local defect was incorrectly expanded into admission, exclusion, coverage
reconciliation, receipt reconciliation, target lifecycle, and several Runtime
guards. The worktree grew by roughly 1,300 lines and regressed the earlier email
collection stage.

The code was restored to the `20260826_210528` behavior. The overgrown version
was preserved as:

```text
stash@{0}: wip: overgrown sendforms state experiments after 20260826_210528
```

At that recovery point, 23 Actor snapshots and 21 State append snapshots matched
the saved run, and the Tool Agent regression reported `394 passed, 1 deselected`.

### Findings from subsequent live/debug work

- The Downloads picker originally lost its causal surface path. State flattened
  `Compose -> Add Attachment picker -> Downloads` into a generic Downloads page,
  so Actor treated it as a standalone Files screen. An owner-aware surface path
  fixed the recorded decision 3/3 without a new schema or state machine.
- A later live failed earlier because the action binding contract incorrectly
  required target and property refs to be both present or both absent. Opening an
  email is naturally `target + null`: it is bound to that target but only reveals
  the control that can establish a property. This should be represented by the
  existing two independent refs, not by a new phase or `reveal_action` type.
- Another early failure was not missing perception. State already had Alice,
  Bob, Carl, Dave, and Echo with dates, but Actor interacted with Alice and then
  reopened it. Actor was not reliably applying the Goal Contract predicate before
  choosing the first visible row.
- A Runtime date guard made this worse by comparing visual `Oct 3` directly with
  an ISO timestamp. That semantic guard was invalid and was removed. Do not
  restore it or add similar semantic guards.
- State and Perception already run in parallel. Critical LLM latency is
  `max(state, perception) + policy`, not their sum. Timing output was updated to
  display this explicitly.
- Typed State output repeatedly hit `max_tokens=900`. A compact tuple wire reduced
  a dense 16-event frame from 742-968 tokens to about 409 tokens, but the larger
  conclusion was that the fixed semantic schema itself encourages field and
  reducer growth.

## Latest architectural decision

Replace the fixed semantic property model with a minimal machine envelope and one
open, target-oriented Markdown memory document.

```text
State
  -> observes current facts
  -> edits target-oriented Markdown memory

Runtime
  -> applies exact text edits
  -> retains frame id, surface, target registry, current visibility, binding,
     receipts, and provenance
  -> does not parse Markdown or infer business meaning

Actor
  -> reads full Goal Contract + latest Markdown memory + screenshot
  -> evaluates filters, remaining differences, completion, and next action
```

The machine envelope stays small. Semantic relationships remain free-form:

```markdown
### email_carl_field_trip_form

- Sender: Carl
- Received date: Oct 3
- Subject: Field trip form
- Attachments:
  - form1.jpg
    - Downloaded to local storage

### email_echo_field_trip

- Sender: Echo
- Received date: Oct 20
- Subject: Field trip
```

This expresses email-to-attachment ownership naturally without adding
`parent_ref`, `status`, `coverage`, or lifecycle fields.

### Memory update contract

Memory is a current Markdown snapshot, not an ever-growing event dump. State uses
an `edit_state_memory` tool:

- initialization creates the document;
- later frames submit exact `old_lines -> new_lines` replacements;
- Runtime mechanically joins lines and applies a unique replacement;
- Runtime records frame/receipt provenance outside the document;
- Actor reads only the latest document, not the complete edit history.

Line arrays replaced raw `old_text/new_text` because the tested Qwen endpoint
discarded Markdown newlines inside string tool arguments. This is a transport
fix, not a semantic schema.

State memory rules:

- organize durable facts under stable `### <target_ref>` headings;
- preserve natural nested relationships;
- write only observed or receipt-conclusive results;
- never store visibility/clickability in durable Markdown;
- never write eligibility, progress, pending/resolved, coverage, terminal status,
  recommendations, coordinates, or next actions;
- State receives only open `observation_focus` / `fact_interests`, not the full
  filter predicates; Actor alone receives and applies the Goal Contract.

The Actor action contract should require `state_target_ref`, with value
`string | null`. A visible target action uses its exact ref. Pure navigation or an
untracked control uses `null`.

## Evidence for the Markdown direction

- Manually reorganizing frame 7 memory as target-oriented Markdown made Actor
  choose Echo 3/3 instead of reopening Carl.
- After clarifying that Markdown heading IDs are exact action refs, Actor returned
  the correct Echo `state_target_ref` 3/3.
- The manually organized context fell from 1,883 to 1,019 characters, about 46%.
- Automatic State replay over frames 5 -> 6 -> 7 produced stable refs, natural
  email/attachment relationships, and date facts for all five visible emails.
- `old_lines/new_lines` preserved real Markdown structure and allowed local edits.
- Measured State calls during probes ranged roughly from 4.6 to 18.1 seconds; the
  upper end is endpoint variance and is not yet an architectural latency result.
- The standard endpoint could not be used for final replay because its token-plan
  weekly quota returned 429. DashScope was used for the same-contract probe.

## Current implementation status

The Markdown/edit design has already been wired into the uncommitted production
worktree. Changes include contracts, State trace projection/editing, Runtime,
Actor/State prompts, replay migration, and tests. The intended replacement removes
the property reducer, predicate pairs, Runtime completion gating, and semantic
submit-control checks rather than running old and new systems in parallel.

Observed intermediate validation:

- basic contract set reached 70/70;
- later focused suites reached 133/134 and 123/124, with the single failures being
  stale test expectations that were patched;
- a final full focused rerun has not yet been completed after the latest edits;
- no live run has validated the Markdown implementation;
- no commit has been made for today's Markdown work.

The last probe was interrupted while correcting its receipt target reference. The
probe had generated `attachment_form1_jpg_download`, but injected a receipt for
the old hard-coded `attachment_form1_jpg`. State correctly ignored a receipt bound
to a nonexistent ref. This is a probe wiring bug, not evidence against production.

The temporary probe is:

```text
tmp_scripts/probe_markdown_state_edit.py
```

No probe process is currently running.

## Worktree cautions

- Branch: `codex/toolagent-loop-regression`
- HEAD/upstream: `541c560a`, already pushed before today's uncommitted experiments.
- The worktree is dirty across Tool Agent production, replay, fixtures, tests,
  `bin/mobileworld`, and unrelated local directories such as `.claude/` and
  `injection/`.
- Do not reset or discard unrelated user changes.
- Do not apply `stash@{0}` over the current tree unless explicitly asked; it is the
  deliberately overgrown SendForms experiment retained only for recovery/reference.

## Exact next steps

1. Fix the probe so the simulated receipt copies the actual target ref produced in
   frame 5. Do not fall back to a guessed email ref if the attachment is represented
   only as nested Markdown; bind the receipt according to the real production action
   envelope produced for that frame.
2. Rerun frames 5 -> 6 -> 7 through the current production prompt and
   `edit_state_memory` contract. Verify:
   - Carl keeps one stable ref across detail/list views;
   - the successful download becomes a durable result fact exactly once;
   - all five email dates are retained;
   - Actor chooses Echo 3/3 and returns its exact target ref.
3. Run the focused Tool Agent tests and `git diff --check`. Resolve only genuine
   Markdown-contract regressions; do not add compatibility state machines.
4. Replay a terminal case and confirm Actor, not Runtime, owns completion judgment.
5. Review the diff and delete all replaced typed property/predicate/completion paths.
   Ensure there is one production path, not a compatibility dual track.
6. Only after the critical replays pass, start `init_task` in the background and run
   one bounded SendForms live test. Analyze the first divergence before changing code.
7. If the live experiment disproves the Markdown/edit direction, remove its changes
   rather than layering another mechanism on top.

## Definition of success for the next handoff

- frame 7 selects Echo consistently after Carl's download receipt;
- picker remains owned by Compose and selects the visible attachment;
- State memory stays factual, compact, and free of lifecycle language;
- Runtime contains no task-semantic predicate or completion logic;
- focused tests pass and the production diff is smaller than the replaced typed
  implementation;
- one SendForms live run reaches attachment selection and send without repeated
  email/download actions or meaningless turns.
