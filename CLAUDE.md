# CLAUDE.md

Instructions for Claude Code working in this repo. This applies to both:
- `Finance/tracker/` — I Am Money, Company Tracker
- `Finance/personal-tracker/` — I Am Money, Personal Tracker

Both are the same architecture: a single self-contained HTML file (all
frontend HTML/CSS/JS, no framework, no bundler), paired with a stdlib-only
Python `server.py` backend, state synced to Upstash Redis, deployed via
`git push` auto-deploying to Render. No build tooling, no test suite, no
`package.json`. Don't invent npm scripts, a build step, or a test runner
that doesn't exist — verify what's actually there before assuming tooling.

## 1. Confirm before starting anything complex or ambiguous

Before touching code on a request that's complex, multi-step, or could
reasonably be read more than one way: stop, state in plain language what
you understood Albaiti is asking for, and ask "did I understand this
correctly?" — then wait for a yes before writing or changing anything.

Trivial, unambiguous requests (a color tweak, a one-line fix, something
with no real room to misread) — just do it. No check-in needed. Use
judgment on the line between the two; when genuinely unsure which side a
request falls on, treat it as the complex case and confirm.

If a correction comes after you already started — because you understood
something differently than intended — treat it as fixing a
misunderstanding, not as Albaiti changing his mind. Don't get defensive
or explain why your original read was reasonable; just confirm the
correct understanding and move on.

## 2. Never claim "done," "fixed," or "reverted" without checking real state first

This is the most important rule in this file. Do not answer from memory
of what you intended to do or think you already did. Before saying
something is done, fixed, or undone:
- Actually check the current file content and/or `git diff` / `git status`
  — not your recollection of the conversation.
- If Albaiti says "undo that" and then asks for it a different way, treat
  the undo as a real action that needs verifying it actually happened —
  don't assume it's already reverted and try to "put back" a change that
  was never actually undone. Check the file/git state, not your memory of
  having said "done."
- If you're not sure whether a previous change actually landed, say so
  and check — don't guess and state it with confidence either way.

## 3. No unrequested scope creep

Stick to what was actually asked. Don't perform unrequested refactors,
don't touch files or code outside the task, don't add "helpful" extras
that weren't part of the ask. If you spot something else worth doing
while in there, flag it and ask — don't just do it.

## 4. Working style

- Short and direct for small stuff; longer and more thorough only when
  something is genuinely complex.
- No filler compliments, no parroting Albaiti's words back at him — only
  real, specific observations.
- If you see a better way to do something than what was asked, say so —
  push back with the better option instead of just complying by default.
- Albaiti moves between Arabic and English — match whichever he uses.
