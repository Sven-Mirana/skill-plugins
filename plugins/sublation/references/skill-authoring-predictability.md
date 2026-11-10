# Skill Authoring Predictability

This reference adapts `mattpocock/skills` `writing-great-skills` for Sublation authoring and review. It is not a wholesale import. The source method treats a skill as a way to make a stochastic agent behave predictably; Sublation uses that lens to decide whether a candidate makes skill behavior easier to invoke, audit, prune, and promote.

Source used:

- Local clone: `${USER_HOME}/Desktop/skill文档夹/mattpocock-skills`
- Commit: `7a83a3a`
- Entity source: `skills/productivity/writing-great-skills/SKILL.md`
- Disclosed glossary: `skills/productivity/writing-great-skills/GLOSSARY.md`
- License: MIT

## Core Lens

The target virtue is predictability: the agent should follow the same process for the same kind of task, even when the output varies. A Sublation candidate therefore improves a skill only when it makes process behavior more observable, repeatable, and reviewable without erasing useful user control.

Use this reference during:

- external skill absorption;
- route/index skill design;
- SKILL.md pruning;
- repeated failure-mode diagnosis;
- review of large candidate references before promotion.

## Load Tradeoff

Every skill split spends one of two loads:

- `context load`: model-visible description text that is always present and competes for attention;
- `cognitive load`: user memory required to know a user-invoked skill exists and when to call it.

Sublation rule: do not create a new model-invoked skill only because a concept is useful. Create or keep it model-invoked only when the agent must discover it on its own, or another skill must route to it. If the user alone chooses when to invoke it, make it user-invoked and preserve context.

When many user-invoked skills accumulate, prefer a router skill or index reference instead of forcing every branch into model context. This directly applies to large local skill sets such as legal, finder, crawler, and Loop Engineering lanes.

Review questions:

- Does this candidate reduce the load the user actually feels, or merely move text around?
- Is a new description paying for independent invocation, or just adding context noise?
- Would one router/index entry preserve discoverability with less context load?

## Information Ladder

Put information where the agent needs it:

1. in-skill steps: ordered actions that every run needs;
2. in-skill reference: compact rules the agent needs frequently;
3. disclosed reference: linked detail loaded only when a branch needs it;
4. external reference: shared material outside the skill body.

Sublation rule: keep `SKILL.md` focused on stable routing, hard boundaries, and minimum process steps. Push branch-specific doctrine, examples, large checklists, and historical patterns into references with clear context pointers.

The pointer wording matters more than the target filename. If a must-have reference is often missed, sharpen the pointer first; only inline it if pointer sharpening fails.

Review questions:

- Is the new reference branch-specific enough to be disclosed?
- Does each pointer say when to open the reference, not merely that it exists?
- Did the candidate scatter one concept across several files, or co-locate definition, rules, caveats, and review questions?

## Leading Words

A leading word is a compact concept that already has behavioral force in the model's prior knowledge. Good leading words reduce tokens and stabilize behavior because the agent can think with the concept during execution.

Sublation candidates should name recurring governance ideas with stable leading words, then reuse the word instead of restating the same paragraph. Existing Sublation examples include:

- `candidate layer`: experimental area that is not an active skill root;
- `formal write`: a production-path mutation requiring user authority;
- `observation window`: post-promotion monitoring, not completion;
- `value delta`: the positive increment required for real sublation;
- `USER_DECISION_REQUIRED`: automation stop point before promotion.

Review questions:

- Is the candidate repeating one idea in several paragraphs that could collapse into one named check?
- Is the chosen term already used by 用户 and the agents, or is it invented jargon that needs costly explanation?
- Does the leading word anchor a stricter behavior than the default? If not, it is probably a no-op.

## Completion Criteria

Every step should end with a checkable completion criterion. "Assess the candidate" is weak; "state changed paths, source provenance, boundary exclusions, audit result, and current approval state" is checkable.

Sublation rule: use completion criteria to prevent premature completion. If the agent sees later steps and starts rushing, first sharpen the criterion. Split the sequence only when the criterion cannot be made sharp and the rush is observed.

Review questions:

- Can another agent tell whether the step is done by reading disk evidence?
- Does the criterion force enough legwork, such as read-back, diff, hash, or fixture evidence?
- Is a reviewer `PASS` being treated as completion when user approval is still missing?

## Pruning Discipline

A skill should have one source of truth for each behavior. Additions are cheap in the moment and expensive later; every candidate should remove or avoid at least one layer of sediment when possible.

Use the no-op test sentence by sentence:

```text
Would this sentence change agent behavior compared with the default?
```

If the answer is no, delete it or replace it with a stronger leading word or sharper criterion. Do not keep no-op praise, process theater, or reminders that only repeat the system's normal behavior.

Review questions:

- Is this line live, unique, and behavior-changing?
- Is this a stale historical layer that should move to an archive or evidence file?
- Does the same rule already exist in `SKILL.md`, a reference, or the manifest schema?

## Failure Mode Map

Use this map when a skill or candidate repeatedly misbehaves.

| Failure mode | Sublation symptom | First response |
|---|---|---|
| Premature completion | reports "done" while review, audit, sync, or approval is missing | sharpen completion criterion; only split/hide later steps if rushing persists |
| Duplication | same authority rule lives in multiple references and drifts | choose one source of truth; replace repeats with pointers |
| Sediment | stale incident-specific text remains in the active path | move history to evidence/archive; keep only the durable rule |
| Sprawl | `SKILL.md` becomes too long even with live content | disclose branch-specific reference behind a clear pointer |
| No-op | text says what the agent already does by default | delete, or replace with a stronger leading word or checkable gate |

## Loop Engineering Mapping

In Loop Engineering reviews, apply the source method as follows:

- Load tradeoff -> decide whether a capability belongs in `find-skills`/index routing, a model-invoked skill, or a user-invoked specialist.
- Information ladder -> decide whether content belongs in `SKILL.md`, `references/`, `schemas/`, or only the candidate evidence.
- Leading words -> stabilize shared room vocabulary so Codex, Claude Code, Hermes, and 用户 mean the same operational state.
- Completion criteria -> make progress reports disk-verifiable before the coordinator briefs 用户.
- Pruning -> keep v3 automation from growing into a ceremonial checklist that says the same governance rule in five places.

## Promotion Guidance

This reference is safe to promote only as a spec patch. It does not authorize:

- formal writes before 用户 approval;
- automatic promotion;
- deletion or merge of donor skills;
- live scraping/download;
- credential/login/session handling;
- optimizer, iterative loop, sync pull, or load_skill.

Expected value delta:

- stronger authoring checks for future Sublation candidates;
- fewer no-op or duplicated governance paragraphs;
- clearer diagnosis when a skill fails by rushing, sprawl, sediment, or context/cognitive load mismatch;
- better route design for large skill families without broad context expansion.
