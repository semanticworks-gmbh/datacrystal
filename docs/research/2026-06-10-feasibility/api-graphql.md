# Finding: api-graphql

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

Strawberry (v0.316.0 clone, current to 2026-06-09) is a thin layer over stdlib dataclasses: @strawberry.type literally runs dataclasses.dataclass(kw_only=True)(cls) and attaches a __strawberry_definition__ registry object; at execution time plain fields are read with getattr (configurable per-schema via StrawberryConfig.default_resolver), and graphql-core never type-checks source objects for non-interface types. I verified empirically that (a) an existing dataclass passes through strawberry.type unchanged (same class object, original __init__ preserved), (b) __setattr__ hooks, descriptors, and duck-typed/raw-pydantic source objects all work, (c) a custom default_resolver may return a DataLoader future for a plain field and graphql-core awaits it — giving batched lazy-ref resolution with zero per-field resolvers, and (d) ONE plain dataclass with dual Annotated metadata (pydantic.Field + strawberry.field) simultaneously serves pydantic TypeAdapter validation/JSON and a strawberry schema. Pydantic BaseModel classes cannot be passed to strawberry.type directly; strawberry.experimental.pydantic.type generates a second dataclass via make_dataclass, though raw pydantic instances can be returned from resolvers. The FastAPI integration is a GraphQLRouter whose context_getter is a normal FastAPI dependency — the natural injection point for the store and per-request DataLoaders. Conclusion: the "one class definition" story is fully achievable with a dataclass-first @pyr.entity decorator that instruments attributes after dataclass processing and installs a store-aware default_resolver.

## Key findings

### @strawberry.type is dataclasses.dataclass + a sidecar definition object

In strawberry/types/object_type.py, _wrap_dataclass() is exactly `dataclasses.dataclass(kw_only=True)(cls)`; _process_type() then sets cls.__strawberry_definition__ = StrawberryObjectDefinition(name, fields, interfaces, ...). The class object returned is the SAME class passed in. Fields are harvested from dataclasses.fields(cls) in types/type_resolver.py:_get_fields. Verified: passing an already-@dataclass class through strawberry.type keeps its original __init__ (dataclasses' _set_new_attribute skips methods already in __dict__, so the kw_only re-wrap is a no-op for existing dataclasses); only classes where strawberry generates the dataclass get kw-only __init__.

### Runtime field resolution is plain getattr on arbitrary objects — descriptors/__setattr__/duck typing all tolerated

StrawberryField.default_resolver = getattr (types/field.py:81); get_result() calls default_resolver(source, python_name). In schema/schema_converter.py:from_object, is_type_of is None unless the type implements interfaces, so graphql-core never verifies the source object's class for plain object types. Empirically verified: a class with a __setattr__ hook, a descriptor-valued dataclass field (descriptor fires on schema read), a non-dataclass duck-typed object, and a raw pydantic BaseModel instance all resolve correctly as field sources. This means pyrsistance's instrumentation (dirty-tracking __set__, lazy-swizzling __get__) is invisible to strawberry.

### StrawberryConfig.default_resolver is the killer integration hook — verified it can return DataLoader futures

schema/config.py: StrawberryConfig(default_resolver: Callable[[Any, str], object] = getattr) replaces the resolver for ALL basic (resolver-less) fields schema-wide (assigned in from_resolver, schema_converter.py:692). Verified empirically: a default_resolver that detects an unloaded Ref sentinel and returns loader.load(oid) (an asyncio Future) works — graphql-core awaits awaitables returned from sync resolvers, and two sibling refs were batched into ONE load_fn call. This gives pyrsistance transparent, batched lazy-reference resolution in GraphQL with zero per-field resolver code.

### DataLoader: event-loop-tick batching, future cache, prime/clear API

strawberry/dataloader.py (306 lines, self-contained, no graphql dependency): DataLoader(load_fn: async [keys]->[values|Exception], max_batch_size, cache=True, cache_key_fn, cache_map). load() returns a Future; the batch dispatch is scheduled via loop.call_soon, so everything requested in the same event-loop tick lands in one batch. Per-key errors are exceptions in the result list. prime()/prime_many() inject known values (useful to pre-seed from pyrsistance's identity map); clear()/clear_all() for invalidation. Async-only — no sync API.

### experimental.pydantic generates a SECOND class; raw model instances still usable as sources

strawberry/experimental/pydantic/object_type.py: @strawberry.experimental.pydantic.type(model=User, all_fields=True) builds a NEW dataclass via dataclasses.make_dataclass from the pydantic model's fields (strawberry.auto markers or all_fields), wires from_pydantic/to_pydantic converters, sets model._strawberry_type = cls and generates is_type_of accepting isinstance(obj, (cls, model)) so raw pydantic instances work in interfaces/unions too. Docs (docs/integrations/pydantic.md) mark it 'experimental: true'. A pydantic BaseModel cannot be passed to strawberry.type directly (metaclass clash with dataclass wrapping). Verified: a resolver typed UserType returning a raw User pydantic instance resolves fine.

### Verified: ONE dataclass + dual Annotated metadata serves pydantic AND strawberry AND leaves room for persistence markers

Test: @strawberry.type over @dataclasses.dataclass with field `name: Annotated[str, pydantic.Field(min_length=2), strawberry.field(description=...)]`. pydantic.TypeAdapter(Person) validates/serializes (rejects short names, dumps JSON); strawberry schema renders the description and executes; both libraries ignore each other's Annotated metadata. Strawberry explicitly supports StrawberryField inside Annotated (types/type_resolver.py:_get_field_from_annotated). pyrsistance can add its own Annotated markers (PyrLazy(), PyrIndex(), Transient()) the same way — three consumers, one annotation.

### Private fields and ID/relay machinery

strawberry.Private[T] = Annotated[T, StrawberryPrivate()] (types/private.py) keeps a dataclass field out of the schema — ideal for persistence-internal state; combining Private with strawberry.field raises PrivateStrawberryFieldError. relay (strawberry/relay/types.py): GlobalID = base64('TypeName:node_id') exposed as id: ID!; Node interface + NodeID[T] annotation marks the id attribute (hidden from schema); types implement classmethod resolve_nodes(node_ids, required) — a registry-level batch hook pyrsistance can implement once against the store for `node(id:)` lookup. strawberry.ID is a NewType over str. StrawberryConfig.relay_use_legacy_global_id toggles GlobalID vs plain ID scalar.

### FastAPI integration: GraphQLRouter with DI-friendly context_getter

strawberry/fastapi/router.py: class GraphQLRouter(AsyncBaseHTTPView, APIRouter); usage app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix='/graphql'). context_getter is a genuine FastAPI dependency (Depends-chain works), returning a dict or BaseContext subclass; default context carries request/response/background_tasks. WebSocket subscriptions (graphql-transport-ws + legacy), GraphiQL IDE, queries via GET supported. This is where pyrsistance injects the store handle plus fresh per-request DataLoaders.

### Impedance mismatches catalogued

(1) strawberry.field() as assignment default IS a dataclasses.Field subclass so plain dataclass accepts it, but pydantic TypeAdapter would misread it — the Annotated form avoids this entirely. (2) Sync resolvers (incl. getattr-fired lazy descriptor loads) run inline on the event loop — blocking disk I/O blocks the loop; strawberry has no threadpool offload; fix via default_resolver returning awaitables, eager prefetch, or anyio.to_thread inside loaders. (3) kw_only=True applies only to strawberry-generated dataclasses; pre-decorated dataclasses keep positional init — decorator order matters (@strawberry.type above @dataclass). (4) GraphQL requires separate input types — output classes can't be reused for mutations; need generated strawberry.input clones or experimental.pydantic.input. (5) default_resolver is per-schema, not per-type — fine if pyrsistance owns schema construction. (6) ID worlds: GraphQL ID(str)/GlobalID(base64) vs EclipseStore-style int64 OIDs vs pydantic ints — needs one canonical mapping (OID -> GlobalID via Node interface). (7) DataLoader caches futures per loader instance — pyrsistance already has an identity map (the live graph), so per-request loaders should set cache=False or prime from the identity map to avoid double caching.

### Proposed @pyr.entity design (single decorator, derive everything)

@pyr.entity: (1) apply dataclasses.dataclass if not already one; (2) AFTER dataclass processing, replace field attributes with instrumented descriptors (SQLAlchemy-style instrumentation) for dirty tracking + lazy swizzle — safe because strawberry reads via getattr at execution time and harvests fields from __dataclass_fields__, not instance attrs; (3) register the class + field metadata (from Annotated markers: pyr.Lazy[T]=Annotated[T,PyrLazy()], pyr.Transient[T] mapped also to strawberry.Private, pyr.Id[T] mapped to relay NodeID) in an EntityRegistry; (4) lazily derive views on demand: strawberry.type(cls) pass-through for GraphQL, cached pydantic.TypeAdapter(cls) for REST/JSON, generated strawberry.input clone for mutations; (5) pyr.graphql.schema(query=...) builds strawberry.Schema with StrawberryConfig(default_resolver=store_resolver) returning DataLoader futures for unloaded refs, and a registry-wide Node.resolve_nodes against the store; (6) pyr.fastapi.router(store) wraps GraphQLRouter with context_getter injecting store + request-scoped loaders. Net result: user writes one dataclass; persistence, GraphQL (incl. batched lazy refs and global node lookup), and pydantic/REST all derive from it.

## Implications for the Python port

Build dataclass-first, not pydantic-first: a plain dataclass is the only representation all three consumers accept natively (strawberry.type passes it through unmodified; pydantic v2 TypeAdapter validates/serializes dataclasses including Annotated constraints; FastAPI accepts dataclasses as response models). Pydantic BaseModel users get a second-class path: auto-generate the strawberry type via the experimental.pydantic machinery (all_fields=True) or, better, reimplement that small generator in pyrsistance to avoid depending on a module marked experimental. REPLICATE: (1) strawberry's Annotated-metadata convention — define pyr.Lazy/pyr.Transient/pyr.Id as Annotated aliases so one annotation carries persistence + GraphQL + validation semantics and each library ignores the others' markers; (2) the DataLoader pattern as the storage read-batching primitive (it is 300 lines, dependency-free — vendor or reimplement it so the persistence core has no strawberry dependency, and have the GraphQL layer reuse it); (3) SQLAlchemy-style post-dataclass attribute instrumentation for dirty tracking and lazy swizzling — verified invisible to strawberry. KEY MECHANISM to exploit: StrawberryConfig(default_resolver=...) returning DataLoader futures for unloaded refs gives transparent batched lazy loading across the whole schema with no per-field resolvers — this should be the centerpiece of the GraphQL story. SIMPLIFY: do not require users to write resolver classes, second 'Type' classes, or from_/to_ converters; one @pyr.entity decorator + derived views. DO DIFFERENTLY: (a) keep the persistence core synchronous (EclipseStore-style lazy.get() blocking) but expose an async facade for GraphQL/FastAPI — never let getattr-triggered disk reads run inline on the event loop in production paths; route them through the default_resolver/awaitable hook or prefetch; (b) unify IDs early: int64 OID in storage, exposed as relay GlobalID('Type:oid') in GraphQL and plain int in REST, with one registry-level resolve_nodes implementation against the store; (c) auto-generate GraphQL input types and pydantic create/update models from the same field metadata, excluding Lazy/Transient/Id fields by default. Pin design assumptions to strawberry >=0.300 semantics (kw_only dataclass wrap, default_resolver config, Annotated StrawberryField support all present in 0.316.0).

## Sources

- /Users/sh/pyrsistance/resources/strawberry/strawberry/types/object_type.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/types/field.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/types/type_resolver.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/types/private.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/schema/config.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/schema/schema_converter.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/dataloader.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/experimental/pydantic/object_type.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/fastapi/router.py
- /Users/sh/pyrsistance/resources/strawberry/strawberry/relay/types.py
- /Users/sh/pyrsistance/resources/strawberry/docs/integrations/pydantic.md
- /Users/sh/pyrsistance/resources/strawberry/docs/integrations/fastapi.md
- /Users/sh/pyrsistance/resources/strawberry/docs/guides/dataloaders.md
- /Users/sh/pyrsistance/resources/strawberry/pyproject.toml
- empirical tests run via `uv run --with strawberry-graphql --with pydantic` (Python 3.13, strawberry 0.316.x): dataclass pass-through, __setattr__/descriptor tolerance, duck-typed and raw-pydantic sources, default_resolver returning DataLoader futures, dual-Annotated dataclass with TypeAdapter + Schema
