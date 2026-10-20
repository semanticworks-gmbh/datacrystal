# Mutation baseline (#189)

Mutants of `src/datacrystal/` at v0.10.0 (86d6b21) against the v0.10.0 tests, mutmut 3.8.0, 8 CPUs.
Killed names: `baseline-killed.txt`; check with `scripts/mutation_check.py`.
Expires with #192, deleted by #193.

## Run

- Wall clock: about 4 h (17:34 to 21:33), 26 slices of at most 9 minutes, resumed from mutmut's per-mutant results.
- Rate: 10 mutants/s on the fast functions, 0.06/s on the last ones (functions every test touches, such as `Store.__init__`, `_load_state`).
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required on macOS: the fork server aborts on `import pyarrow` without it.
- `process_isolation = "forkserver"` is required: the tests leave classes in the global entity registry, and `test_fts.py::test_registry_derived_config_indexes_fulltext_entities` fails after `test_r11_lazy_only.py`.
- `mutation/mutmut_order.py` sorts the tests of every run by file, so a mutant is not killed by that order.
- `tests/fitness/test_contract_engine_free.py::test_contract_package_imports_no_engine` is deselected: mutmut's trampoline import is a rejected engine import in `contract/`.
- Each mutant leaves a pytest temp dir with SQLite stores; several GB per slice filled the disk once, so clear `$TMPDIR/pytest-of-$USER` between slices.
- The disk-full slice was redone: the 235 killed or crashed results with the highest estimated test time were reset and re-run.

## Not counted as killed

- survived: 2875.
- timeout: 23.
- segfault: 285, exit -9, the SIGKILL mutmut sends at the wall limit under load (mostly `_storage/sqlite`, `_store`, `_indexes`, `_records`).
- no tests: 136, no test reaches the function (all of `contract.vectors._generate`, `_async` 17, `_lazy` 16, `_snapshot` 19).
- not checked: 6, still running after 9 minutes each, so they hang: `Store._register_graph` 52, `Store._discover_new_graphs` 1/3/5, `IndexManager.ensure` 1/2.
- Nothing is left out by scope: all modules ran.

## Stability

300 killed mutants (tests under 15 s) were re-run: 299 killed again, 1 survived (`ClassIndexes._unindex` mutmut_10, now excluded).
About 0.3 % of the kills, roughly 30 of 9264, may be flaky.

## Per module

| module | mutants | killed | survived | timeout | segfault | no tests | suspicious | not checked |
|---|---|---|---|---|---|---|---|---|
| datacrystal | 11 | 10 | 1 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._actors | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._async | 76 | 39 | 18 | 2 | 0 | 17 | 0 | 0 |
| datacrystal._conditions | 378 | 303 | 55 | 1 | 5 | 14 | 0 | 0 |
| datacrystal._containers | 169 | 91 | 64 | 0 | 11 | 3 | 0 | 0 |
| datacrystal._entity | 668 | 494 | 160 | 11 | 0 | 3 | 0 | 0 |
| datacrystal._errors | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._follower | 565 | 375 | 190 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._ids | 18 | 15 | 0 | 0 | 0 | 3 | 0 | 0 |
| datacrystal._index_cache | 58 | 47 | 4 | 0 | 7 | 0 | 0 | 0 |
| datacrystal._indexes | 859 | 721 | 92 | 1 | 42 | 1 | 0 | 2 |
| datacrystal._lazy | 190 | 135 | 39 | 0 | 0 | 16 | 0 | 0 |
| datacrystal._permissions | 140 | 104 | 36 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._pipeline | 44 | 44 | 0 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._records | 221 | 159 | 28 | 0 | 27 | 7 | 0 | 0 |
| datacrystal._redacted | 76 | 50 | 26 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._registry | 7 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| datacrystal._snapshot | 915 | 676 | 220 | 0 | 0 | 19 | 0 | 0 |
| datacrystal._state | 32 | 18 | 10 | 0 | 4 | 0 | 0 | 0 |
| datacrystal._storage.lock | 140 | 88 | 29 | 0 | 23 | 0 | 0 | 0 |
| datacrystal._storage.memory | 107 | 93 | 9 | 0 | 4 | 1 | 0 | 0 |
| datacrystal._storage.sqlite | 514 | 350 | 107 | 0 | 53 | 4 | 0 | 0 |
| datacrystal._store | 3257 | 2456 | 678 | 5 | 109 | 5 | 0 | 4 |
| datacrystal.arrow | 1072 | 784 | 287 | 0 | 0 | 1 | 0 | 0 |
| datacrystal.contract.applier | 103 | 83 | 20 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.contract.vectors._generate | 41 | 0 | 0 | 0 | 0 | 41 | 0 | 0 |
| datacrystal.deltalog | 367 | 266 | 101 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.fts | 852 | 625 | 225 | 2 | 0 | 0 | 0 | 0 |
| datacrystal.testing | 624 | 439 | 185 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.web | 12 | 0 | 11 | 0 | 0 | 1 | 0 | 0 |
| datacrystal.web._app | 169 | 141 | 28 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.web._federation | 314 | 208 | 106 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.web._pydantic | 327 | 228 | 98 | 1 | 0 | 0 | 0 | 0 |
| datacrystal.web._reflect | 81 | 62 | 19 | 0 | 0 | 0 | 0 | 0 |
| datacrystal.web._strawberry | 170 | 141 | 29 | 0 | 0 | 0 | 0 | 0 |
| **total** | **12589** | **9264** | **2875** | **23** | **285** | **136** | **0** | **6** |
