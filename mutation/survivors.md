# Survivors in the functions #192 changed

The mutants of #193's full run (`mutation/summary.md`) that no test noticed, in the 307 functions #192 (PR #205) rewrote, by the function that holds them.
A story per section writes the tests that kill the *behaviour* survivors; rerun one function with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES uv run mutmut run 'datacrystal.<module>.x_<function>__mutmut_*'` (a method is `xǁ<Class>ǁ<method>`).

- **behaviour:** the mutant changes a value, an operator, an argument or a key or name string, and the code then does something else.
- **accepted:** the mutant changes only a `cast()` type string, which does nothing at runtime, or a message's wording, which no test pins (decision in #193); no test and no pragma.

## _store

294 behaviour survivors, 169 accepted.

| function | behaviour | accepted |
|---|---|---|
| `_store.Store._fingerprint_payload` | 21 | 0 |
| `_store.Store._deref_item` | 16 | 3 |
| `_store._wanted_oids` | 16 | 3 |
| `_store.Store.pluck` | 15 | 1 |
| `_store.Store._materialize_graph` | 14 | 5 |
| `_store.Store._load_oid_deref` | 13 | 4 |
| `_store._find_escapee` | 13 | 8 |
| `_store.Store._harvest_live_refs` | 11 | 9 |
| `_store.Store._resolve` | 10 | 5 |
| `_store.Store._fill_entity` | 8 | 0 |
| `_store.Store._get_many_by_key` | 8 | 2 |
| `_store.Store._gate_new_record` | 7 | 5 |
| `_store.Store.get_many` | 7 | 1 |
| `_store._equivalent` | 7 | 0 |
| `_store.Store.__init__` | 6 | 0 |
| `_store.Store.open` | 6 | 0 |
| `_store._publish` | 6 | 5 |
| `_store.Store._check_write_gate` | 5 | 0 |
| `_store.Store._resolve_registry_actor` | 5 | 18 |
| `_store.Store.follower` | 5 | 0 |
| `_store.Store.query` | 5 | 0 |
| `_store._gate_label_changes` | 5 | 4 |
| `_store.Store._check_registered` | 4 | 0 |
| `_store.Store._prior_labels` | 4 | 2 |
| `_store.Store._walk_value` | 4 | 4 |
| `_store.Store.mark_dirty` | 4 | 3 |
| `_store.Store.open_blob` | 4 | 5 |
| `_store.Store.upsert` | 4 | 6 |
| `_store._natural_key_field` | 4 | 3 |
| `_store._raw_value` | 4 | 10 |
| `_store._ref_target_oid` | 4 | 3 |
| `_store.Store._delete_by_key` | 3 | 4 |
| `_store.Store.incoming` | 3 | 0 |
| `_store.Store._delete_instance` | 2 | 2 |
| `_store.Store._delete_oid` | 2 | 1 |
| `_store.Store._deref_cached_protected` | 2 | 3 |
| `_store.Store._load_oid` | 2 | 0 |
| `_store.Store._oid_for_encode` | 2 | 0 |
| `_store.Store._ordered_window` | 2 | 0 |
| `_store.Store._p1_capture` | 2 | 0 |
| `_store.Store._readable_registered` | 2 | 0 |
| `_store.Store._readable_unregistered` | 2 | 0 |
| `_store.Store._sink_blob` | 2 | 0 |
| `_store._deny_write` | 2 | 2 |
| `_store._forbid_redacted` | 2 | 2 |
| `_store._gate_actor_memberships` | 2 | 3 |
| `_store.Store._assert_lease_held` | 1 | 3 |
| `_store.Store._find_by_key` | 1 | 11 |
| `_store.Store._get_by_key_unchecked` | 1 | 2 |
| `_store.Store._load_root_holder` | 1 | 3 |
| `_store.Store._on_first_write` | 1 | 3 |
| `_store.Store._payload_digest` | 1 | 0 |
| `_store.Store._query_iter_stream` | 1 | 0 |
| `_store.Store._queue_entity` | 1 | 3 |
| `_store.Store._read_delta_priors` | 1 | 2 |
| `_store.Store._stamp_perm_labels` | 1 | 0 |
| `_store.Store._twin_if_unreadable` | 1 | 0 |
| `_store.Store._validate_before_tid` | 1 | 0 |
| `_store._RawView.__getattr__` | 1 | 0 |
| `_store._gate_delete` | 1 | 0 |
| `_store._gate_persisted_record` | 1 | 1 |
| `_store._require_storable` | 1 | 4 |
| `_store._require_unique` | 1 | 0 |
| `_store.Store._adopt_unregistered` | 0 | 3 |
| `_store.Store.delete` | 0 | 4 |
| `_store.Store.get` | 0 | 3 |
| `_store._require_persisted_fields` | 0 | 1 |

## arrow and _follower

214 behaviour survivors, 96 accepted.

| function | behaviour | accepted |
|---|---|---|
| `_follower._apply_catchup` | 30 | 5 |
| `arrow.ArrowMirror.bootstrap` | 17 | 3 |
| `arrow.ArrowMirror._require_root_for_protected` | 14 | 12 |
| `_follower._holds_entity` | 13 | 6 |
| `arrow._storable` | 13 | 0 |
| `_follower._fetch_deltas` | 11 | 5 |
| `arrow.ArrowMirror.apply` | 11 | 7 |
| `arrow._reswizzle` | 11 | 5 |
| `arrow._semantic` | 11 | 0 |
| `arrow._temporal_tag` | 10 | 0 |
| `_follower._post_submit` | 9 | 2 |
| `_follower._raise_rejection` | 8 | 3 |
| `arrow._tag_of` | 8 | 2 |
| `_follower._container_holds_entity` | 7 | 6 |
| `_follower._refs_local_new` | 7 | 6 |
| `arrow.ArrowMirror.__init__` | 7 | 4 |
| `_follower._submit_op` | 5 | 5 |
| `arrow._list_tag` | 5 | 0 |
| `_follower._reject_unsupported_containers` | 4 | 6 |
| `_follower._contribute` | 3 | 4 |
| `_follower.open_follower` | 3 | 0 |
| `arrow.ArrowMirror._decode_ops` | 3 | 8 |
| `_follower._natural_key_of` | 2 | 3 |
| `_follower._reject_blob_fields` | 1 | 4 |
| `_follower._scanned_field_names` | 1 | 0 |

## testing and _snapshot

172 behaviour survivors, 126 accepted.

| function | behaviour | accepted |
|---|---|---|
| `testing._check_version_refusal` | 29 | 35 |
| `_snapshot.Snapshot._all_ordered` | 28 | 0 |
| `_snapshot.Snapshot.query` | 17 | 0 |
| `testing._check_unknown_op` | 13 | 9 |
| `testing._check_gap_refusal` | 12 | 14 |
| `_snapshot._view_value` | 10 | 15 |
| `testing.CountingConsumer.apply` | 10 | 11 |
| `testing._check_stamp_indifference` | 9 | 6 |
| `_snapshot._freeze` | 7 | 5 |
| `_snapshot.Snapshot.all` | 6 | 5 |
| `testing._check_ordering` | 6 | 2 |
| `_snapshot.Snapshot._all_streamed` | 5 | 0 |
| `_snapshot._SnapshotCore.class_indexes` | 5 | 0 |
| `testing._check_idempotency` | 5 | 6 |
| `testing._check_delete_totality` | 3 | 5 |
| `_snapshot._SnapshotCore.warn_unseen` | 2 | 2 |
| `_snapshot.RedactedView.__getattr__` | 1 | 6 |
| `_snapshot.Snapshot._all_protected` | 1 | 0 |
| `_snapshot.Snapshot._readable_for` | 1 | 0 |
| `testing.CountingConsumer.bootstrap` | 1 | 0 |
| `testing._check_prior_unindex` | 1 | 5 |

## web._federation and web._pydantic

119 behaviour survivors, 64 accepted.

| function | behaviour | accepted |
|---|---|---|
| `web._federation.federation_router` | 39 | 0 |
| `web._federation._create_dto` | 16 | 0 |
| `web._pydantic._contains_model` | 9 | 6 |
| `web._pydantic._strip_optional` | 8 | 0 |
| `web._pydantic._field_info` | 7 | 0 |
| `web._pydantic.from_pydantic` | 7 | 3 |
| `web._federation._require_owner_thread` | 5 | 3 |
| `web._federation._writable_type` | 4 | 9 |
| `web._pydantic._contains_entity` | 4 | 0 |
| `web._pydantic._dto_value` | 4 | 9 |
| `web._federation._conflict_http_error` | 3 | 0 |
| `web._federation._upsert_checked` | 3 | 0 |
| `web._pydantic._boundary_annotation` | 3 | 0 |
| `web._pydantic.to_pydantic` | 3 | 5 |
| `web._federation._op_envelope` | 2 | 14 |
| `web._federation._prepare_ops` | 1 | 7 |
| `web._pydantic._guard_owner` | 1 | 0 |
| `web._pydantic._entity_dto_value` | 0 | 4 |
| `web._pydantic._lazy_dto_value` | 0 | 4 |

## _entity and _containers

109 behaviour survivors, 32 accepted.

| function | behaviour | accepted |
|---|---|---|
| `_entity._may_hold_entity` | 17 | 0 |
| `_entity._resolve_spec` | 17 | 0 |
| `_containers.PersistentList.__setitem__` | 13 | 3 |
| `_entity._check_r11_lazy_only` | 11 | 1 |
| `_entity._is_decimal` | 8 | 0 |
| `_entity._is_list_of_scalar` | 7 | 0 |
| `_containers.PersistentDict.setdefault` | 5 | 0 |
| `_containers.PersistentList.sort` | 5 | 0 |
| `_containers.wrap_value` | 5 | 2 |
| `_entity._contains_lazy` | 5 | 0 |
| `_containers.PersistentDict.__init__` | 3 | 3 |
| `_containers.PersistentList.extend` | 2 | 0 |
| `_entity._frozen_setattr` | 2 | 2 |
| `_entity._resolve_specs` | 2 | 0 |
| `_containers.PersistentDict.__setitem__` | 1 | 0 |
| `_containers.PersistentDict.pop` | 1 | 0 |
| `_containers.PersistentDict.update` | 1 | 0 |
| `_containers.PersistentList.append` | 1 | 0 |
| `_containers.PersistentList.insert` | 1 | 0 |
| `_entity._FieldMarkers.harvest` | 1 | 0 |
| `_entity._permissions_assign` | 1 | 0 |
| `_entity._check_blob_field` | 0 | 6 |
| `_entity._check_rename_and_glue` | 0 | 15 |

## the rest

150 behaviour survivors, 86 accepted.

| function | behaviour | accepted |
|---|---|---|
| `fts._excerpt` | 15 | 0 |
| `_redacted.twin_class` | 13 | 8 |
| `web._reflect._collect_entities` | 13 | 0 |
| `web.__getattr__` | 10 | 1 |
| `fts.FullTextIndex._match_expression` | 9 | 0 |
| `deltalog.DeltaLog.apply` | 7 | 10 |
| `_permissions.protect` | 6 | 4 |
| `_permissions.share` | 6 | 0 |
| `_permissions._staged` | 5 | 14 |
| `contract.applier.ReferenceApplier.apply` | 5 | 6 |
| `deltalog.DeltaLog.bootstrap` | 5 | 0 |
| `_conditions.parse_order_by` | 4 | 9 |
| `_records._ext_hook` | 4 | 0 |
| `fts._matched_mask` | 4 | 0 |
| `web._strawberry._make_relation_resolver` | 4 | 0 |
| `_lazy.Lazy._loaded` | 3 | 0 |
| `_lazy.LazyReferenceManager.__init__` | 3 | 1 |
| `_state.touch` | 3 | 2 |
| `web._app._OwnerWriter.__call__` | 3 | 0 |
| `_indexes._guard_temporal_comparable` | 2 | 4 |
| `_lazy.BlobHandle._bind` | 2 | 0 |
| `_lazy.Lazy._unloaded` | 2 | 0 |
| `_permissions.can_read_row` | 2 | 0 |
| `_records.swizzle` | 2 | 5 |
| `_redacted.make_twin` | 2 | 3 |
| `web._app.create_app` | 2 | 0 |
| `_conditions.FieldExpr.__ne__` | 1 | 0 |
| `_conditions.Pred._contains` | 1 | 0 |
| `_conditions.order_by_values` | 1 | 0 |
| `_conditions.query_target` | 1 | 0 |
| `_indexes.ClassIndexes._enter_sorted_run` | 1 | 0 |
| `_indexes._plan_or` | 1 | 0 |
| `_permissions.authority_towards` | 1 | 0 |
| `_records._lazy_ref_ext` | 1 | 3 |
| `_records._refuse_blob_values` | 1 | 4 |
| `_records._swizzle_leaf` | 1 | 3 |
| `_records.encode_scalar_tree` | 1 | 0 |
| `contract.applier.check_delta_header` | 1 | 2 |
| `fts.FullTextIndex._term_alternatives` | 1 | 2 |
| `web._app._StoreLifespan.__init__` | 1 | 0 |
| `_indexes.build_class_indexes` | 0 | 2 |
| `_permissions._check_level` | 0 | 3 |
