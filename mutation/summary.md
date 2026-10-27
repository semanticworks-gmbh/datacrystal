# Mutation run (#193)

Mutants of `src/datacrystal/` at 8f8d1e0, the code #192 cleaned and the tests #190 rewrote, mutmut 3.8.0, 8 CPUs.
It replaces the v0.10.0 baseline (#189); that run's numbers stay in the last column of the table, from this file's history.
The survivors in the functions #192 changed, with how each was triaged: `mutation/survivors.md`.

## Run

- Wall clock: about 13 h in two sittings (2026-10-26 11:42 to 21:37, 2026-10-27 07:59 to 11:10), slices of at most 9 minutes, resumed from mutmut's per-mutant results.
- The first 9 h ran the mutants in name order, so the functions every test reaches (`Store.__init__`, `_load_state`, `close`, about 30 s of tests per mutant) held most slices; sorted by the test time of their function, cheapest first, the last 4,166 took 4 h.
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required on macOS: the fork server aborts on `import pyarrow` without it.
- `process_isolation = "forkserver"` is required: the tests leave classes in the global entity registry.
- `mutation/mutmut_order.py` sorts the tests of every run by file, so a mutant is not killed by that order.
- `tests/fitness/test_contract_engine_free.py::test_contract_package_imports_no_engine` is deselected: mutmut's trampoline import is a rejected engine import in `contract/`.
- Each mutant leaves a pytest temp dir with SQLite stores, so clear `$TMPDIR/pytest-of-$USER` between slices.
- A laptop sleep stops the run; mutmut's deadlines are wall-clock, so pause it by killing it and resume, never sleep through a slice.

## Not counted as killed

- survived: 2,876, in every module but five.
- timeout: 35.
- segfault: 239, exit -9: the SIGKILL mutmut sends one second after its CPU-time limit, so in effect a timeout (mostly `_store`, `_storage.sqlite`, `_indexes`, `_records`, `_storage.lock`).
- no tests: 95, no test reaches the function (`_snapshot` 19, `_async` 17, `_lazy` 16, `_conditions` 14).
- not checked: 4, still running after two 9-minute slices each, so they hang: `Store._register_graph` 16, `Store._discover_new_graphs` 1/3/5; the same two functions hung at v0.10.0.
- No stability rerun this time; at v0.10.0, 1 of 300 rerun kills flipped.

## Per module

| module | mutants | killed | survived | timeout | segfault | no tests | not checked | killed at v0.10.0 |
|---|---|---|---|---|---|---|---|---|
| datacrystal | 11 | 10 | 1 | 0 | 0 | 0 | 0 | 10 of 11 |
| datacrystal._actors | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 8 of 8 |
| datacrystal._async | 76 | 41 | 18 | 0 | 0 | 17 | 0 | 39 of 76 |
| datacrystal._conditions | 372 | 303 | 52 | 1 | 2 | 14 | 0 | 303 of 378 |
| datacrystal._containers | 176 | 101 | 54 | 0 | 18 | 3 | 0 | 91 of 169 |
| datacrystal._entity | 665 | 491 | 170 | 0 | 1 | 3 | 0 | 494 of 668 |
| datacrystal._errors | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 4 of 4 |
| datacrystal._follower | 570 | 385 | 185 | 0 | 0 | 0 | 0 | 375 of 565 |
| datacrystal._ids | 18 | 15 | 0 | 0 | 0 | 3 | 0 | 15 of 18 |
| datacrystal._index_cache | 74 | 64 | 8 | 0 | 2 | 0 | 0 | 47 of 58 |
| datacrystal._indexes | 896 | 762 | 94 | 4 | 35 | 1 | 0 | 721 of 859 |
| datacrystal._lazy | 190 | 132 | 42 | 0 | 0 | 16 | 0 | 135 of 190 |
| datacrystal._permissions | 150 | 109 | 41 | 0 | 0 | 0 | 0 | 104 of 140 |
| datacrystal._pipeline | 44 | 44 | 0 | 0 | 0 | 0 | 0 | 44 of 44 |
| datacrystal._records | 226 | 164 | 31 | 0 | 24 | 7 | 0 | 159 of 221 |
| datacrystal._redacted | 76 | 50 | 26 | 0 | 0 | 0 | 0 | 50 of 76 |
| datacrystal._registry | 7 | 7 | 0 | 0 | 0 | 0 | 0 | 7 of 7 |
| datacrystal._snapshot | 952 | 710 | 223 | 0 | 0 | 19 | 0 | 676 of 915 |
| datacrystal._state | 32 | 18 | 5 | 0 | 9 | 0 | 0 | 18 of 32 |
| datacrystal._storage.lock | 162 | 113 | 27 | 0 | 22 | 0 | 0 | 88 of 140 |
| datacrystal._storage.memory | 107 | 93 | 8 | 0 | 5 | 1 | 0 | 93 of 107 |
| datacrystal._storage.sqlite | 516 | 353 | 112 | 1 | 46 | 4 | 0 | 350 of 514 |
| datacrystal._store | 3518 | 2717 | 692 | 27 | 73 | 5 | 4 | 2456 of 3257 |
| datacrystal.arrow | 1062 | 783 | 278 | 0 | 0 | 1 | 0 | 784 of 1072 |
| datacrystal.contract.applier | 112 | 88 | 22 | 0 | 2 | 0 | 0 | 83 of 103 |
| datacrystal.deltalog | 359 | 263 | 96 | 0 | 0 | 0 | 0 | 266 of 367 |
| datacrystal.fts | 887 | 664 | 221 | 2 | 0 | 0 | 0 | 625 of 852 |
| datacrystal.testing | 596 | 412 | 184 | 0 | 0 | 0 | 0 | 439 of 624 |
| datacrystal.web | 12 | 0 | 11 | 0 | 0 | 1 | 0 | 0 of 12 |
| datacrystal.web._app | 169 | 144 | 25 | 0 | 0 | 0 | 0 | 141 of 169 |
| datacrystal.web._federation | 330 | 224 | 106 | 0 | 0 | 0 | 0 | 208 of 314 |
| datacrystal.web._pydantic | 334 | 238 | 96 | 0 | 0 | 0 | 0 | 228 of 327 |
| datacrystal.web._reflect | 81 | 62 | 19 | 0 | 0 | 0 | 0 | 62 of 81 |
| datacrystal.web._strawberry | 170 | 141 | 29 | 0 | 0 | 0 | 0 | 141 of 170 |
| **total** | **12962** | **9713** | **2876** | **35** | **239** | **95** | **4** | **9264 of 12589** |

## Score

- Strict, killed / checked: 9,713 / 12,958 = 75.0 % (v0.10.0: 73.6 %).
- Timeout and the SIGKILL counted as detected (Stryker's count): 9,987 / 12,958 = 77.1 % (v0.10.0: 76.1 %).
- Mutants a test reaches only: 9,987 / 12,863 = 77.6 % (v0.10.0: 76.9 %).
- The score is a thermometer, never a gate: it compares only with itself (same mutmut, config and code); equivalent mutants among the survivors push the true value up.
- The working unit is a survivor in a changed function: `scripts/mutation_check.py` lists them before a push.

## The functions #192 changed

The 307 functions #192 (PR #205) rewrote hold 6,767 mutants: 4,998 killed, 1,631 survived, 121 timeout or SIGKILL, 16 without a test, 1 hanging.
The survivors split by what the mutant changes, sorted by its diff (a heuristic: a story that reads the function may move one):

| kind | survivors | triage |
|---|---|---|
| a `cast()` type string, nothing at runtime | 143 | accepted, no test, no pragma |
| the wording of a message or reason | 430 | accepted, no test, no pragma |
| a key or name string | 128 | a test, per module in `survivors.md` |
| a value, operator, argument or number | 930 | a test, per module in `survivors.md` |
