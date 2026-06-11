"""The datacrystal demo is literally a crystal cabinet.

Run it twice::

    uv run python examples/minerals/demo.py
    uv run python examples/minerals/demo.py   # finds the first run's data

First run: builds a small mineral collection (deterministic synthetic data —
real mineral names are facts; the vendored Wikidata CC0 snapshot arrives
later, see docs/design/KICKOFF.md §5) and commits it. Every run: queries via
bitmap indexes, follows lazy references, proves identity, appends to the
frozen provenance log.
"""

from __future__ import annotations

import random
from dataclasses import field
from pathlib import Path
from typing import Annotated

import datacrystal as dc

# --- the model --------------------------------------------------------------


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str, dc.Index]


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    formula: str | None
    crystal_system: Annotated[str | None, dc.Index]
    mohs: float | None
    type_locality: dc.Lazy[Locality] | None = None


@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    mineral: dc.Lazy[Mineral]
    quality: Annotated[str, dc.Index]  # museum / fine / cabinet / thumbnail
    mass_g: float
    acquired_year: Annotated[int, dc.Index]


@dc.entity(frozen=True)
class LogEntry:  # append-only provenance: dirty tracking never arms
    specimen_no: str
    kind: Annotated[str, dc.Index]
    note: str


@dc.entity
class Cabinet:
    minerals: list = field(default_factory=list)
    specimens: list = field(default_factory=list)
    log: list = field(default_factory=list)


# --- deterministic synthetic collection (seeded; real names are facts) ------

MINERALS = [
    ("Q43010", "quartz", "SiO2", "trigonal", 7.0, ("Q39", "Alps", "Switzerland")),
    ("Q193563", "azurite", "Cu3(CO3)2(OH)2", "monoclinic", 3.5, ("Q571997", "Tsumeb Mine", "Namibia")),
    ("Q190063", "malachite", "Cu2CO3(OH)2", "monoclinic", 3.8, ("Q571997", "Tsumeb Mine", "Namibia")),
    ("Q5283", "diamond", "C", "cubic", 10.0, ("Q258", "Kimberley", "South Africa")),
    ("Q134583", "pyrite", "FeS2", "cubic", 6.3, ("Q29", "Navajún", "Spain")),
    ("Q103480", "fluorite", "CaF2", "cubic", 4.0, ("Q21", "Rogerley Mine", "England")),
    ("Q127356", "topaz", "Al2SiO4(F,OH)2", "orthorhombic", 8.0, ("Q155", "Ouro Preto", "Brazil")),
    ("Q132734", "beryl", "Be3Al2Si6O18", "hexagonal", 7.8, ("Q33", "Karelia", "Finland")),
    ("Q25437", "corundum", "Al2O3", "trigonal", 9.0, ("Q837", "Mogok", "Myanmar")),
    ("Q131749", "opal", "SiO2·nH2O", "amorphous", 5.8, ("Q408", "Coober Pedy", "Australia")),
]
QUALITIES = ["museum", "fine", "cabinet", "thumbnail"]


def build_cabinet() -> Cabinet:
    rng = random.Random(0xDC)
    localities: dict[str, Locality] = {}
    minerals = []
    for qid, name, formula, system, mohs, (lqid, lname, country) in MINERALS:
        loc = localities.setdefault(lqid, Locality(qid=lqid, name=lname, country=country))
        minerals.append(Mineral(qid=qid, name=name, formula=formula, crystal_system=system,
                                mohs=mohs, type_locality=dc.Lazy.of(loc)))
    specimens, log = [], []
    for i in range(120):
        mineral = rng.choices(minerals, weights=range(len(minerals), 0, -1))[0]
        no = f"DC-{i:04d}"
        specimens.append(Specimen(
            specimen_no=no, mineral=dc.Lazy.of(mineral),
            quality=rng.choices(QUALITIES, weights=[1, 3, 6, 10])[0],
            mass_g=round(rng.lognormvariate(3.0, 1.2), 1),
            acquired_year=rng.randint(1995, 2026),
        ))
        log.append(LogEntry(specimen_no=no, kind="acquired", note=f"acquired {mineral.name}"))
    return Cabinet(minerals=minerals, specimens=specimens, log=log)


def main() -> None:
    store_dir = Path(__file__).parent / "cabinet.store"
    first_run = not store_dir.exists()
    with dc.Store.open(store_dir) as store:
        if store.root is None:
            store.root = build_cabinet()
            tid = store.commit()
            print(f"first run: crystallized the cabinet (commit tid={tid})")
        cabinet = store.root
        print(f"{'fresh' if first_run else 'restored'} cabinet: "
              f"{len(cabinet.minerals)} minerals, {len(cabinet.specimens)} specimens, "
              f"{len(cabinet.log)} log entries (watermark tid={store.last_tid})")

        # Bitmap query (single-class by design; KICKOFF §5). dc.fields() is
        # the type-checker-clean route; `Specimen.quality == ...` also works.
        S, M = dc.fields(Specimen), dc.fields(Mineral)
        hits = store.query((S.quality == "fine") & (S.mass_g >= 30.0))
        print(f"fine specimens >= 30 g: {len(hits)}")

        # Condition AST over the mineral facets
        hard_cubic = store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))
        print("hard cubic minerals:", sorted(m.name for m in hard_cubic))

        # Unique secondary key + lazy traversal
        azurite = store.get(Mineral, qid="Q193563")
        assert azurite is not None and azurite.type_locality is not None
        print(f"azurite type locality (lazy → loaded): "
              f"{azurite.type_locality.get().name}, {azurite.type_locality.get().country}")

        # Identity: every path to an entity yields the same live object
        tsumeb_minerals = [m for m in cabinet.minerals
                           if m.type_locality.get().qid == "Q571997"]
        assert tsumeb_minerals[0].type_locality.get() is tsumeb_minerals[1].type_locality.get()
        print("identity holds: azurite and malachite share one live Tsumeb object")

        # Frozen provenance log: appending is the only legal mutation.
        # The in-place append is tracked transparently — cabinet.log is a
        # PersistentList bound to the cabinet, no mark_dirty needed.
        cabinet.log.append(LogEntry(specimen_no="DC-0000", kind="inspected",
                                    note="demo run inspection"))
        try:
            cabinet.log[0].note = "rewrite history"
        except dc.FrozenEntityError:
            print("frozen log entry refused mutation (append-only provenance)")
        tid = store.commit()
        print(f"appended one log entry (commit tid={tid}) — rerun me: it will still be there")


if __name__ == "__main__":
    main()
