from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
import io
import secrets
import threading
from typing import Annotated, TypedDict
import unittest
from unittest.mock import patch

import httpx
import pymysql
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt

from backend.agents import actions, api, config, gateway, store
from backend.agents.checkpointer import MySQLSaver
from backend.infrastructure import database as db
from backend.modules.accounts import service as accounts
from backend.modules.knowledge import api as knowledge_api, parsing, service
from backend.modules.browser_agent import executor


class RuntimeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.config = db.database_config()
        cls.name = "videodream_upgrade_test_" + secrets.token_hex(5)
        cls.connection = pymysql.connect(**{k: v for k, v in cls.config.items() if k != "database"})
        with cls.connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{cls.name}` CHARACTER SET utf8mb4")
        cls.stack.enter_context(patch.object(db, "database_config", return_value=cls.config | {"database": cls.name}))
        from backend.tests.support import migrate_test_database
        migrate_test_database(cls.config | {"database": cls.name})
        db.schema_ready = False
        cls.uid = accounts.create_user(accounts.Credentials(username="upgrade_test", password="Upgrade-test-123!"))
        cls.other = accounts.create_user(accounts.Credentials(username="upgrade_other", password="Upgrade-test-123!"))

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        db.schema_ready = False
        with cls.connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{cls.name}`")
        cls.connection.close()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        marker = accounts.principal.set(accounts.Principal(self.uid, "upgrade_test", "user", "session"))
        self.addCleanup(accounts.principal.reset, marker)

    def task(self, *, kind="knowledge_query", tokens=20000, parent=None):
        with db.database() as cursor:
            row, _ = store.create(cursor, self.uid, kind, {"question": "test"}, secrets.token_hex(12), parent=parent,
                                  task_limits=config.limits(kind) | {"tokens": tokens, "daily_tokens": 10000000})
            return store.update(cursor, row, status="running", worker_id="test", lease_until=store.now() + timedelta(minutes=3))

    def test_nine_table_contract_and_foreign_keys(self):
        from backend.migrations.runner import schema_snapshot
        with db.database() as cursor:
            schema = schema_snapshot(cursor)
        names = {n for n in schema if n.startswith(("agent_", "knowledge_"))}
        self.assertEqual(len(names), 9)
        self.assertEqual(schema["agent_checkpoints"]["columns"]["payload"]["type"], "longblob")
        self.assertTrue(schema["agent_tasks"]["indexes"]["agent_task_idempotency"]["unique"])
        self.assertTrue(all(r["delete"] == "RESTRICT" for n in names for r in schema[n]["foreign_keys"]))

    def test_create_idempotency_and_payload_conflict(self):
        key = secrets.token_hex(12)
        with db.database() as cursor:
            row, created = store.create(cursor, self.uid, "knowledge_query", {"question": "A"}, key)
            other, second = store.create(cursor, self.uid, "knowledge_query", {"question": "A"}, key)
            self.assertEqual(row["id"], other["id"])
            self.assertTrue(created)
            self.assertFalse(second)
        with self.assertRaises(HTTPException) as caught, db.database() as cursor:
            store.create(cursor, self.uid, "knowledge_query", {"question": "B"}, key)
        self.assertEqual(caught.exception.status_code, 409)

    def test_user_isolation(self):
        row = self.task()
        with self.assertRaises(HTTPException) as caught, db.database() as cursor:
            store.get(cursor, row["id"], self.other)
        self.assertEqual(caught.exception.status_code, 404)

    def test_optimistic_revision_conflict(self):
        row = self.task()
        with db.database() as cursor:
            store.update(cursor, row, context={"message": "first"})
        with self.assertRaises(HTTPException), db.database() as cursor:
            store.update(cursor, row, status="cancelled")

    def test_events_contiguous_with_state_changes(self):
        row = self.task()
        with db.database() as cursor:
            row = store.update(cursor, row, status="succeeded")
            cursor.execute("SELECT sequence_no FROM agent_events WHERE task_id=%s ORDER BY sequence_no", (row["id"],))
            self.assertEqual([r["sequence_no"] for r in cursor.fetchall()], [1, 2, 3])

    def test_langgraph_interrupt_survives_new_saver(self):
        row = self.task()
        class State(TypedDict):
            task_id: str
            value: str
        def ask(state):
            return {"value": interrupt({"approval": True})}
        def builder():
            graph = StateGraph(State)
            graph.add_node("ask", ask)
            graph.add_edge(START, "ask")
            graph.add_edge("ask", END)
            return graph.compile(checkpointer=MySQLSaver())
        cfg = {"configurable": {"thread_id": row["id"]}}
        first = builder().invoke({"task_id": row["id"], "value": ""}, cfg)
        self.assertIn("__interrupt__", first)
        second = builder().invoke(Command(resume="approved"), cfg)
        self.assertEqual(second["value"], "approved")
        self.assertGreater(len(list(MySQLSaver().list(cfg))), 1)

    def test_pending_writes_prevent_successful_sibling_replay(self):
        row = self.task()
        class State(TypedDict):
            task_id: str
            values: Annotated[list, __import__("operator").add]
        count = {"first": 0, "second": 0}
        def first(state):
            count["first"] += 1
            return {"values": ["first"]}
        def second(state):
            count["second"] += 1
            if count["second"] == 1:
                raise RuntimeError("simulated worker interruption")
            return {"values": ["second"]}
        def builder():
            graph = StateGraph(State)
            graph.add_node("first", first)
            graph.add_node("second", second)
            graph.add_edge(START, "first")
            graph.add_edge(START, "second")
            graph.add_edge("first", END)
            graph.add_edge("second", END)
            return graph.compile(checkpointer=MySQLSaver())
        cfg = {"configurable": {"thread_id": row["id"]}}
        with self.assertRaises(RuntimeError):
            builder().invoke({"task_id": row["id"], "values": []}, cfg)
        result = builder().invoke(None, cfg)
        self.assertEqual(set(result["values"]), {"first", "second"})
        self.assertEqual(count, {"first": 1, "second": 2})

    def test_model_result_replay_never_dispatches_twice(self):
        row = self.task()
        model = {"model": "test", "base_url": "https://example.invalid/v1"}
        call, cached = gateway.reserve(row["id"], "text", model, {"q": "a"}, 100)
        self.assertFalse(cached)
        gateway.dispatch(call)
        gateway.settle(call, {"answer": "A"}, {"total_tokens": 40}, dispatched=True)
        replay, cached = gateway.reserve(row["id"], "text", model, {"q": "a"}, 100)
        self.assertTrue(cached)
        self.assertEqual(replay["response"], {"answer": "A"})

    def test_concurrent_root_reservations_cannot_exceed_budget(self):
        row = self.task(tokens=200)
        model = {"model": "test", "base_url": "https://example.invalid/v1"}
        def reserve(i):
            try:
                gateway.reserve(row["id"], "text", model, {"i": i}, 150)
                return True
            except HTTPException:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reserve, [1, 2])), [False, True])
        with db.database() as cursor:
            cursor.execute("UPDATE agent_model_calls SET state='settled',actual_tokens=0 WHERE task_id=%s", (row["id"],))

    def test_child_uses_root_budget(self):
        root = self.task(tokens=100)
        child = self.task(tokens=1000, parent=root["id"])
        with self.assertRaises(HTTPException):
            gateway.reserve(child["id"], "embedding", {"model": "test", "base_url": "https://test.invalid"}, {}, 101)

    def test_unknown_usage_blocks_user_calls_and_is_not_replayed(self):
        row = self.task()
        model = {"model": "test", "base_url": "https://test.invalid"}
        call, _ = gateway.reserve(row["id"], "text", model, {"q": "unknown"}, 100)
        gateway.dispatch(call)
        with self.assertRaises(gateway.UsagePending):
            gateway.settle(call, {"text": "unknown"}, {}, dispatched=True)
        other = self.task()
        with self.assertRaises(gateway.UsagePending):
            gateway.reserve(other["id"], "text", model, {}, 10)
        # Reconciliation is an accounting operation, separate from task resumption.
        gateway.settle(call, {"text": "known"}, {"total_tokens": 25}, dispatched=True)

    def test_cancelled_late_response_settles_without_publishing(self):
        row = self.task()
        call, _ = gateway.reserve(row["id"], "text", {"model": "test", "base_url": "https://test.invalid"}, {}, 100)
        gateway.dispatch(call)
        with db.database() as cursor:
            store.update(cursor, store.get(cursor, row["id"], lock=True), status="cancelled")
        gateway.settle(call, {"text": "late"}, {"total_tokens": 10}, dispatched=True)
        with db.database() as cursor:
            current = store.get(cursor, row["id"])
            self.assertEqual(current["status"], "cancelled")
            self.assertIsNone(current["result"])

    def approval(self):
        row = self.task(kind="browser_playlist")
        action = actions.prepare(row["id"], "save_imported_playlist", {"name": "A", "tracks": []}, step="save", approval=True, observation="a" * 64)
        with db.database() as cursor:
            row = store.update(cursor, row, status="waiting_approval", context={"approval_observation": "a" * 64})
        return row, action

    def test_approval_binds_user_payload_revision_and_expiry(self):
        row, action = self.approval()
        for uid, revision, fingerprint in [(self.other, row["revision"], action["payload_hash"]),
                                            (self.uid, row["revision"] - 1, action["payload_hash"]),
                                            (self.uid, row["revision"], "b" * 64)]:
            with self.assertRaises(HTTPException):
                actions.decision(action["id"], uid, revision, fingerprint, True)
        with db.database() as cursor:
            cursor.execute("UPDATE agent_actions SET approval_expires_at=%s WHERE id=%s", (store.now() - timedelta(seconds=1), action["id"]))
        with self.assertRaises(HTTPException):
            actions.decision(action["id"], self.uid, row["revision"], action["payload_hash"], True)

    def test_duplicate_approval_and_write_are_idempotent(self):
        row, action = self.approval()
        actions.decision(action["id"], self.uid, row["revision"], action["payload_hash"], True)
        actions.decision(action["id"], self.uid, row["revision"], action["payload_hash"], True)
        with db.database() as cursor:
            row = store.update(cursor, store.get(cursor, row["id"], lock=True), status="running")
        called = []
        def write(_):
            called.append(True)
            return {"saved": True}
        self.assertEqual(actions.execute(action, write), {"saved": True})
        self.assertEqual(actions.execute(action, write), {"saved": True})
        self.assertEqual(len(called), 1)

    def test_unknown_write_requires_reconciliation(self):
        row = self.task()
        action = actions.prepare(row["id"], "read", {}, step="unknown")
        with self.assertRaises(RuntimeError):
            actions.execute(action, lambda _: (_ for _ in ()).throw(RuntimeError("unknown")))
        with self.assertRaises(HTTPException):
            actions.execute(action, lambda _: {"wrong": True})
        self.assertEqual(actions.execute(action, lambda _: self.fail("must reconcile"), reconcile=lambda _: {"verified": True}), {"verified": True})

    def test_knowledge_permissions_revocation_and_version(self):
        kb = knowledge_api.create_base(knowledge_api.BaseInput(name="private"))
        with db.database() as cursor:
            with self.assertRaises(HTTPException):
                service.require(cursor, kb["id"], self.other)
        knowledge_api.set_member(kb["id"], knowledge_api.MemberInput(user_id=str(self.other), revision=1, role="viewer"))
        with db.database() as cursor:
            self.assertEqual(service.require(cursor, kb["id"], self.other)["role"], "viewer")
            with self.assertRaises(HTTPException):
                service.require(cursor, kb["id"], self.other, "editor")
        knowledge_api.remove_member(kb["id"], str(self.other), 2)
        with db.database() as cursor, self.assertRaises(HTTPException):
            service.allowed(cursor, self.other, [kb["id"]])

    def test_deleted_or_superseded_document_is_not_evidence(self):
        kb = knowledge_api.create_base(knowledge_api.BaseInput(name="versions"))
        key = secrets.token_hex(16)
        with db.database() as cursor:
            stamp = store.now()
            cursor.execute("INSERT INTO knowledge_documents (id,kb_id,uploaded_by,title,status,active_version,created_at,updated_at) VALUES (%s,%s,%s,'test','published',2,%s,%s)", (key, kb["id"], self.uid, stamp, stamp))
        with db.database() as cursor, self.assertRaises(HTTPException):
            service.validate_evidence(cursor, self.uid, [{"document_id": key, "version": 1}])
        knowledge_api.delete_document(key, 0)
        with db.database() as cursor, self.assertRaises(HTTPException):
            service.document(cursor, key, self.uid)

    def test_api_hides_other_users_task(self):
        from backend.tests.support import domain_app
        row = self.task()
        marker = accounts.principal.set(accounts.Principal(self.other, "upgrade_other", "user"))
        try:
            client = TestClient(domain_app(api))
            self.assertEqual(client.get("/api/agent/tasks/" + row["id"]).status_code, 404)
        finally:
            accounts.principal.reset(marker)

    def test_verified_usage_allows_replay_and_preserves_audit(self):
        row = self.task()
        model = {"model": "test", "base_url": "https://example.test"}
        call, _ = gateway.reserve(row["id"], "text", model, {"a": 1}, 100)
        gateway.dispatch(call)
        with self.assertRaises(gateway.UsagePending):
            gateway.settle(call, {"answer": "retained"}, {}, dispatched=True)
        with db.database() as cursor:
            live = store.get(cursor, row["id"])
        gateway.reconcile(call["id"], live["revision"], 30, "provider-record-123", self.uid)
        with db.database() as cursor:
            live = store.get(cursor, row["id"], lock=True)
            self.assertEqual(live["status"], "interrupted")
            store.update(cursor, live, status="running")
        replay, cached = gateway.reserve(row["id"], "text", model, {"a": 1}, 100)
        self.assertTrue(cached)
        self.assertEqual(replay["response"], {"answer": "retained"})
        self.assertEqual(replay["usage"]["reconciliation"]["provider_reference"], "provider-record-123")

    def test_unsent_failure_retry_retains_old_zero_charge(self):
        row = self.task()
        model = {"model": "test", "base_url": "https://example.test"}
        call, _ = gateway.reserve(row["id"], "text", model, {"a": 1}, 100)
        gateway.settle(call, None, {}, dispatched=False, error="invalid_config")
        second, cached = gateway.reserve(row["id"], "text", model, {"a": 1}, 100)
        self.assertFalse(cached)
        self.assertEqual(second["attempt"], 1)
        with db.database() as cursor:
            cursor.execute("SELECT actual_tokens FROM agent_model_calls WHERE id=%s", (call["id"],))
            self.assertEqual(cursor.fetchone()["actual_tokens"], 0)

    def test_replaced_worker_cannot_dispatch_reserved_request(self):
        row = self.task()
        call, _ = gateway.reserve(row["id"], "text", {"model": "test", "base_url": "https://example.test"}, {}, 100)
        token = store.worker.set((row["id"], row["worker_id"]))
        try:
            with db.database() as cursor:
                store.update(cursor, store.get(cursor, row["id"], lock=True), worker_id="replacement")
            with self.assertRaises(HTTPException):
                gateway.dispatch(call)
            with db.database() as cursor:
                cursor.execute("SELECT state,dispatched FROM agent_model_calls WHERE id=%s", (call["id"],))
                current = cursor.fetchone()
                self.assertEqual(current["state"], "reserved")
                self.assertFalse(current["dispatched"])
        finally:
            store.worker.reset(token)
            gateway.settle(call, None, {}, dispatched=False)

    def test_permission_recheck_uses_current_read_after_snapshot(self):
        kb = knowledge_api.create_base(knowledge_api.BaseInput(name="concurrent revoke"))
        knowledge_api.set_member(kb["id"], knowledge_api.MemberInput(user_id=str(self.other), revision=1))
        with db.database() as cursor:
            self.assertEqual(service.require(cursor, kb["id"], self.other)["role"], "viewer")
            knowledge_api.remove_member(kb["id"], str(self.other), 2)
            with self.assertRaises(HTTPException):
                service.require(cursor, kb["id"], self.other, lock=True)

    def test_upload_race_keeps_one_document_and_cleans_orphan(self):
        kb = knowledge_api.create_base(knowledge_api.BaseInput(name="parallel upload"))
        barrier = threading.Barrier(2)
        removed = []
        def put(*args):
            barrier.wait(timeout=10)
        with patch.object(service.config, "model_config", return_value={"model": "test-embedding", "dimensions": 1024}), patch.object(service.storage, "ensure_index", return_value="videodream_rag_test"), patch.object(service.storage, "put", side_effect=put), patch.object(service.storage, "object_client") as objects:
            objects.return_value.remove_object.side_effect = lambda *args: removed.append(args)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(service.enqueue_document, self.uid, kb["id"], "same.txt", b"same content", "txt", "same-upload-key") for _ in range(2)]
                results = [f.result(timeout=15) for f in futures]
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(removed), 1)
        with db.database() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM knowledge_documents WHERE kb_id=%s", (kb["id"],))
            self.assertEqual(cursor.fetchone()["n"], 1)

    def test_document_cleanup_waits_for_ingest_lock(self):
        from backend.modules.knowledge import storage
        document_id = secrets.token_hex(16)
        with storage.document_lock(document_id), patch.object(storage, "_remove_document") as remove:
            with self.assertRaises(HTTPException) as caught:
                storage.remove_document("test", document_id)
            self.assertEqual(caught.exception.status_code, 409)
            remove.assert_not_called()
        with storage.document_lock(document_id):
            pass


class PureContractTests(unittest.TestCase):
    def test_mcp_tools_honor_disabled_entry(self):
        from backend.agents import mcp
        with patch.dict("os.environ", {"VIDEODREAM_AGENT_ENABLED": "false"}):
            for tool, args in ((mcp.task_status, ("task",)), (mcp.knowledge_query, ("question", [], "idempotency"))):
                with self.assertRaises(HTTPException) as caught:
                    tool(*args)
                self.assertEqual(caught.exception.status_code, 503)

    def test_price_snapshot_uses_real_units_and_rejects_invalid_rates(self):
        value = store.encode({"test": {"input_per_million": "1", "output_per_million": "2", "currency": "CNY", "version": "fixture"}})
        with patch.dict("os.environ", {"VIDEODREAM_AGENT_PRICING": value}):
            self.assertEqual(gateway.pricing("test", {"input_tokens": 1000, "output_tokens": 100})["amount"], "0.0012")
            self.assertIsNone(gateway.pricing("unconfigured"))
        with patch.dict("os.environ", {"VIDEODREAM_AGENT_PRICING": '{"test":{"input_per_million":-1,"output_per_million":0}}'}):
            with self.assertRaises(HTTPException):
                gateway.pricing("test")

    def test_rrf_deduplicates_and_prefers_agreement(self):
        a, b, c = [{"chunk_id": key} for key in "abc"]
        self.assertEqual([p["chunk_id"] for p in service.rrf([a, b], [b, c])], ["b", "a", "c"])

    def test_text_chunks_keep_section_offsets_and_budget(self):
        data = ("# 标题\n\n" + "可引用的知识。" * 900).encode()
        segments = parsing.parse(data, "md")
        chunks = parsing.chunks(segments)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c["locator"]["offset_end"] > c["locator"]["offset_start"] for c in chunks))
        self.assertTrue(all(gateway.tokens(c["content"]) <= 610 for c in chunks))

    def test_video_preserves_timestamps(self):
        segments = parsing.parse(store.encode({"segments": [{"text": "hello", "locator": {"start_ms": 1000, "end_ms": 3000}}]}).encode(), "video_subtitle")
        self.assertEqual(parsing.chunks(segments)[0]["locator"]["start_ms"], 1000)

    def test_parser_limits_and_empty_content(self):
        for data in (b"", b"x" * 200001):
            with self.assertRaises(HTTPException):
                parsing.parse(data, "txt")

    def test_browser_site_boundary(self):
        self.assertTrue(executor.permitted("https://music.163.com/playlist?id=123"))
        for url in ("http://music.163.com", "https://music.163.com.evil.test", "https://127.0.0.1", "https://music.163.com:8001", "https://user:pass@music.163.com"):
            self.assertFalse(executor.permitted(url))
