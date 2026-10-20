"""datacrystal[web] — a ``Decimal`` field crosses the Pydantic boundary exactly (#184)."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import datacrystal as dc
from datacrystal.web import entity_model, from_pydantic, to_pydantic


@dc.entity
class Invoice:
    number: Annotated[str, dc.Unique]
    net: Annotated[Decimal | None, dc.SortedIndex] = None


def test_live_entity_projects_its_decimal_unchanged(store) -> None:
    invoice = Invoice(number="a", net=Decimal("72.46"))
    store.root = invoice
    store.commit()

    dto = to_pydantic(invoice)

    assert dto.net == Decimal("72.46")
    assert type(dto.net) is Decimal


def test_snapshot_view_projects_its_decimal_unchanged(store) -> None:
    store.root = Invoice(number="a", net=Decimal("-0.30"))
    store.commit()

    with store.snapshot() as snap:
        (view,) = snap.query(dc.fields(Invoice).number == "a")
        dto = to_pydantic(view, face="public")

    assert dto.net == Decimal("-0.30")


def test_request_body_decimal_validates_from_text_and_rebuilds_the_entity() -> None:
    body = entity_model(Invoice, face="create").model_validate_json(
        '{"number": "a", "net": "0.1"}')

    entity = from_pydantic(body, Invoice)

    assert entity.net == Decimal("0.1")
    assert type(entity.net) is Decimal


def test_graphql_reflection_exposes_a_decimal_scalar() -> None:
    pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
    import strawberry
    from strawberry.tools import create_type

    from datacrystal.web import reflect_strawberry_type

    gql = reflect_strawberry_type(Invoice)
    def resolve_root() -> object:
        return None

    root: Any = strawberry.field(resolver=resolve_root, graphql_type=gql, name="root")
    sdl = strawberry.Schema(query=create_type("Query", [root])).as_str()

    assert "net: Decimal" in sdl
