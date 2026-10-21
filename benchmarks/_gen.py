"""The deterministic mineral-cabinet generator (KICKOFF §6).

One generator, one domain: the vendored Wikidata backbone arrives later
(KICKOFF §5 fallback — mineral names are facts, CC0 regardless), so the
backbone here is an embedded vocabulary; scale comes from the synthetic
Specimen/CatalogEvent layer. Topology requirements ported from KICKOFF:

* Specimens sample Zipf-over-minerals (s=1.1): the head minerals are the
  "mega mineral" quartz-class hubs — worst-case bitmap density.
* ``acquired_from`` provenance chains of depth ≤ 6, with ~0.1% reference
  cycles (specimen trades) keeping cyclic GC honest.
* The canonical ~1%-selectivity predicate is single-class by design:
  ``(Specimen.quality == "A") & (Specimen.mass_g >= 100.0)``.
* Events: 1 ``acquired`` + Poisson(0.2) extras; ``CatalogEvent`` is
  ``frozen=True`` (append-only throughput; dirty tracking never arms).

Determinism: one ``random.Random(0xDC)``; timestamps are ``epoch + seq``;
no wall-clock, uuid or set iteration anywhere.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable
from typing import Annotated, Any

import datacrystal as dc

# ~48 real species (name, crystal system, Mohs) — facts, CC0 regardless of
# source; padded procedurally to the KICKOFF ~200-mineral vocabulary.
_REAL = [
    ("quartz", "trigonal", 7.0), ("calcite", "trigonal", 3.0),
    ("azurite", "monoclinic", 3.8), ("malachite", "monoclinic", 3.8),
    ("pyrite", "cubic", 6.3), ("galena", "cubic", 2.6),
    ("fluorite", "cubic", 4.0), ("halite", "cubic", 2.5),
    ("gypsum", "monoclinic", 2.0), ("baryte", "orthorhombic", 3.0),
    ("topaz", "orthorhombic", 8.0), ("corundum", "trigonal", 9.0),
    ("hematite", "trigonal", 5.5), ("magnetite", "cubic", 6.0),
    ("sphalerite", "cubic", 3.8), ("chalcopyrite", "tetragonal", 3.5),
    ("cassiterite", "tetragonal", 6.5), ("rutile", "tetragonal", 6.0),
    ("zircon", "tetragonal", 7.5), ("beryl", "hexagonal", 7.8),
    ("apatite", "hexagonal", 5.0), ("vanadinite", "hexagonal", 3.0),
    ("pyromorphite", "hexagonal", 3.8), ("mimetite", "hexagonal", 3.8),
    ("wulfenite", "tetragonal", 2.9), ("crocoite", "monoclinic", 2.8),
    ("cerussite", "orthorhombic", 3.3), ("anglesite", "orthorhombic", 2.8),
    ("smithsonite", "trigonal", 4.5), ("rhodochrosite", "trigonal", 3.8),
    ("siderite", "trigonal", 3.9), ("dolomite", "trigonal", 3.8),
    ("aragonite", "orthorhombic", 3.8), ("olivenite", "orthorhombic", 3.0),
    ("adamite", "orthorhombic", 3.5), ("austinite", "orthorhombic", 4.0),
    ("dioptase", "trigonal", 5.0), ("atacamite", "orthorhombic", 3.3),
    ("cuprite", "cubic", 3.8), ("tenorite", "monoclinic", 3.8),
    ("bornite", "orthorhombic", 3.0), ("covellite", "hexagonal", 1.8),
    ("enargite", "orthorhombic", 3.0), ("tennantite", "cubic", 3.8),
    ("tetrahedrite", "cubic", 3.8), ("proustite", "trigonal", 2.3),
    ("acanthite", "monoclinic", 2.3), ("stephanite", "orthorhombic", 2.3),
]
_SYSTEMS = ("triclinic", "monoclinic", "orthorhombic", "tetragonal",
            "trigonal", "hexagonal", "cubic", "amorphous")
_COUNTRIES = ("Namibia", "Germany", "Mexico", "Australia", "USA", "Chile",
              "Morocco", "China", "Russia", "Peru", "Bolivia", "Greece")
_QUALITIES = ("A", "B", "C", "D")
_EPOCH = dt.datetime(2000, 1, 1)


@dc.entity
class Country:
    iso: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str, dc.Index]


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    mineral: dc.Lazy[Mineral]
    quality: Annotated[str, dc.Index]
    mass_g: float
    acquired_from: dc.Lazy[Specimen] | None = None


@dc.entity(protected=True)
class CuratedSpecimen:
    """Specimen's protected twin (ADR-008, W2-9): exactly the same five
    fields, so protected-vs-plain benchmarks compare ONLY the fence cost.
    Stores built with ``protected=True`` must be opened with a non-anonymous
    ``principal=`` — the R6 fail-closed refusal rejects anonymous creation.
    """

    specimen_no: Annotated[str, dc.Unique]
    mineral: dc.Lazy[Mineral]
    quality: Annotated[str, dc.Index]
    mass_g: float
    acquired_from: dc.Lazy[CuratedSpecimen] | None = None


@dc.entity(frozen=True)
class CatalogEvent:
    seq: Annotated[int, dc.Unique]
    specimen_no: str          # app-maintained backlink (engine incoming() is v1)
    kind: Annotated[str, dc.Index]
    at: dt.datetime = _EPOCH


def vocabulary(size: int = 200) -> list[tuple[str, str, float]]:
    vocab = list(_REAL)
    rng = random.Random(0xDC ^ size)
    while len(vocab) < size:
        i = len(vocab)
        vocab.append((
            f"synthetite-{i:03d}",
            _SYSTEMS[rng.randrange(len(_SYSTEMS))],
            round(1.0 + rng.random() * 8.5, 1),
        ))
    return vocab[:size]


_ZIPF_EXPONENT = 1.1
_LOCALITY_COUNT = 40
_CHAIN_PROBABILITY = 0.30  # share of specimens acquired from an earlier specimen
_MAX_CHAIN_DEPTH = 6
_RECENT_WINDOW = 64  # chain sources: a bounded window of the latest specimens
_CYCLE_PROBABILITY = 0.001
# Cumulative upper bounds of qualities A, B, C (the rest is D).
_QUALITY_BOUNDS = (0.05, 0.20, 0.60)
# Poisson(0.2) extra events: P(0)=.819, P(1)=.164 (cumulative .983), else 2.
_NO_EXTRA_EVENTS_BELOW = 0.819
_ONE_EXTRA_EVENT_BELOW = 0.983


def _quality_for(draw: float) -> str:
    """Map a uniform draw to a quality grade by the cumulative bounds."""
    for grade, bound in zip(_QUALITIES, _QUALITY_BOUNDS, strict=False):
        if draw < bound:
            return grade
    return _QUALITIES[-1]


def _extra_event_count(draw: float) -> int:
    """Map a uniform draw to the Poisson(0.2) number of loan/return events."""
    if draw < _NO_EXTRA_EVENTS_BELOW:
        return 0
    return 1 if draw < _ONE_EXTRA_EVENT_BELOW else 2


def _reference_layers() -> tuple[list[Country], list[Locality], list[Mineral]]:
    """Build the small backbone: countries, localities, the mineral vocabulary."""
    countries = [Country(iso=name[:2].upper() + str(i), name=name)
                 for i, name in enumerate(_COUNTRIES)]
    localities = [
        Locality(qid=f"QL{i:04d}", name=f"locality {i}",
                 country=_COUNTRIES[i % len(_COUNTRIES)])
        for i in range(_LOCALITY_COUNT)
    ]
    minerals = [
        Mineral(qid=f"QM{i:04d}", name=name, crystal_system=system, mohs=mohs,
                type_locality=dc.Lazy.of(localities[i % len(localities)]))
        for i, (name, system, mohs) in enumerate(vocabulary())
    ]
    return countries, localities, minerals


def _zipf_picker(minerals: list[Mineral], rng: random.Random) -> Callable[[], Mineral]:
    """Return a sampler over ``minerals`` with Zipf weights: the head is the quartz-class hub."""
    weights = [1.0 / (i + 1) ** _ZIPF_EXPONENT for i in range(len(minerals))]
    total_w = sum(weights)
    cumulative: list[float] = []
    acc = 0.0
    for w in weights:
        acc += w / total_w
        cumulative.append(acc)

    def pick_mineral() -> Mineral:
        u = rng.random()
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cumulative[mid] < u:
                lo = mid + 1
            else:
                hi = mid
        return minerals[lo]

    return pick_mineral


_AnySpecimen = Specimen | CuratedSpecimen


class _SpecimenLayer:
    """Generates specimens with their trade chains and events, one rng draw order throughout."""

    def __init__(
        self,
        rng: random.Random,
        specimen_cls: type[Specimen | CuratedSpecimen],
        pick_mineral: Callable[[], Mineral],
    ) -> None:
        self.rng = rng
        self.specimen_cls = specimen_cls
        self.pick_mineral = pick_mineral
        self.seq = 0
        self.chain_depth: dict[str, int] = {}
        self.recent: list[_AnySpecimen] = []   # chain sources (bounded window); Specimen or twin
        self.cycle_candidates: list[_AnySpecimen] = []

    def _acquisition(self) -> tuple[dc.Lazy[Any] | None, int]:
        """Pick a chain source among the recent specimens, as ``(acquired_from, depth)``."""
        rng = self.rng
        if self.recent and rng.random() < _CHAIN_PROBABILITY:
            source = self.recent[rng.randrange(len(self.recent))]
            depth = self.chain_depth[source.specimen_no] + 1
            if depth <= _MAX_CHAIN_DEPTH:
                return dc.Lazy.of(source), depth
        return None, 0

    def _remember(self, specimen: _AnySpecimen) -> None:
        """Make ``specimen`` a chain source and, rarely, a cycle candidate."""
        rng = self.rng
        if len(self.recent) < _RECENT_WINDOW:
            self.recent.append(specimen)
        else:
            self.recent[rng.randrange(_RECENT_WINDOW)] = specimen
        if rng.random() < _CYCLE_PROBABILITY:
            self.cycle_candidates.append(specimen)

    def specimen(self, index: int) -> _AnySpecimen:
        """Create the specimen numbered ``index``."""
        rng = self.rng
        no = f"S-{index:07d}"
        acquired_from, depth = self._acquisition()
        specimen = self.specimen_cls(
            specimen_no=no,
            mineral=dc.Lazy.of(self.pick_mineral()),
            quality=_quality_for(rng.random()),
            mass_g=round(rng.lognormvariate(3.7, 1.0), 2),
            acquired_from=acquired_from,
        )
        self.chain_depth[no] = depth
        self._remember(specimen)
        return specimen

    def _event(self, specimen_no: str, kind: str) -> CatalogEvent:
        self.seq += 1
        return CatalogEvent(seq=self.seq, specimen_no=specimen_no, kind=kind,
                            at=_EPOCH + dt.timedelta(seconds=self.seq))

    def events(self, specimen_no: str) -> list[CatalogEvent]:
        """Create the ``acquired`` event plus the Poisson extras for one specimen."""
        events = [self._event(specimen_no, "acquired")]
        for k in range(_extra_event_count(self.rng.random())):
            events.append(self._event(specimen_no, "loaned" if k == 0 else "returned"))
        return events

    def close_trade_loops(self) -> None:
        """Close ~0.1% trade loops: link a chained source back into the specimen."""
        for specimen in self.cycle_candidates:
            if specimen.acquired_from is not None:
                source = specimen.acquired_from.get()
                if source.acquired_from is None:
                    link: dc.Lazy[Any] = dc.Lazy.of(specimen)
                    source.acquired_from = link


def build(store: dc.Store, *, specimens: int, seed: int = 0xDC,
          batch: int = 10_000, protected: bool = False) -> dict[str, int]:
    """Populate ``store`` with the scaled cabinet; returns object counts.
    Commits in batches so P1 capture stays bounded; idempotent only on an
    empty store (it assigns the root)."""
    rng = random.Random(seed)
    # protected=True swaps ONLY the specimen layer's class (additive knob,
    # W2-9): same rng sequence, Zipf hubs, chain/cycle topology and events —
    # the corpus differs solely in the four injected _dc_ columns.
    specimen_cls = CuratedSpecimen if protected else Specimen
    countries, localities, minerals = _reference_layers()
    layer = _SpecimenLayer(rng, specimen_cls, _zipf_picker(minerals, rng))

    store.root = {"countries": countries, "localities": localities,
                  "minerals": minerals}
    store.commit()

    all_events = 0
    for start in range(0, specimens, batch):
        block: list[_AnySpecimen] = []
        events: list[CatalogEvent] = []
        for i in range(start, min(start + batch, specimens)):
            specimen = layer.specimen(i)
            block.append(specimen)
            events.extend(layer.events(specimen.specimen_no))
        for entity in block:
            store.store(entity)
        for event in events:
            store.store(event)
        all_events += len(events)
        store.commit()
    layer.close_trade_loops()
    store.commit()
    return {
        "countries": len(countries), "localities": len(localities),
        "minerals": len(minerals), "specimens": specimens,
        "events": all_events, "cycles": len(layer.cycle_candidates),
    }
