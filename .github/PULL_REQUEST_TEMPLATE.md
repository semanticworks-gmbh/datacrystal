<!--
CI enforces the machine gates (pytest both backends + SIGKILL · ruff incl. D ·
pyright standard + strict · demos×2 · wheel purity · the fitness suite). Green CI
== those boxes are already ticked — don't restate them here.
Only the items CI cannot see go below.
-->

Closes #

## What & why
<one line>

## Reviewer checklist (the things CI can't check)
- [ ] **perf:** read the warn-stage `[gate]` stdout — no BREACH (or justified)
- [ ] **mutation-check:** each new fitness fn was shown to FAIL on a deliberately-broken impl (it bites)
- [ ] **scope/charter:** no §5 behavioural cut (async/`wait_for`, LWW, failover, background poll); single-writer via `store.submit`; OCC detect-and-reject
- [ ] new public name(s) documented in `docs/reference.md` in this PR
