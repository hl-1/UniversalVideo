"""Explicit paid acceptance run; never collected by unittest discover.

Uses one disposable database, a unique project ES index and KB object prefix.
Run with the project's external environment loaded, plus --test-env for a
temporary account authorized to create only videodream_* test databases.
The fixture is synthetic and cannot establish production retrieval quality.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import statistics
import threading
import time
from unittest.mock import patch

from dotenv import dotenv_values
import pymysql


def run(args):
    os.environ.update({k: v for k, v in dotenv_values(args.test_env, interpolate=False).items() if v is not None})
    from backend.agents import config, gateway, runtime, store
    from backend.infrastructure import database as db
    from backend.modules.accounts import service as accounts
    from backend.modules.knowledge import api, service, storage
    from backend.tests.support import migrate_test_database

    tag = secrets.token_hex(6)
    original = db.database_config()
    name = "videodream_acceptance_test_" + tag
    administrator = pymysql.connect(**{k: v for k, v in original.items() if k != "database"})
    with administrator.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    report = {"fixture": "synthetic-labelled-v1", "production_quality_claim": False, "cases": [], "errors": []}
    kb_id = None
    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(patch.object(db, "database_config", return_value=original | {"database": name}))
        stack.enter_context(patch.dict(os.environ, {"VIDEODREAM_RAG_INDEX": "videodream_rag_test_" + tag}))
        try:
            migrate_test_database(original | {"database": name})
            db.schema_ready = False
            uid = accounts.create_user(accounts.Credentials(username="live_acceptance", password=secrets.token_urlsafe(24)))
            principal = accounts.Principal(uid, "live_acceptance", "user", "acceptance")
            marker = accounts.principal.set(principal)
            stack.callback(accounts.principal.reset, marker)
            kb_id = api.create_base(api.BaseInput(name="合成验收资料"))["id"]
            fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("rag_eval.json").read_text())
            references = {}
            for source in fixture["documents"]:
                task = service.enqueue_document(uid, kb_id, source["id"] + ".md", source["content"].encode(), "md", tag + source["id"])
                runtime.run(task["id"])
                with db.database() as cursor:
                    live = store.get(cursor, task["id"])
                if live["status"] != "succeeded":
                    raise RuntimeError("入库失败: " + store.encode(store.public(live)))
                references[source["id"]] = task["request"]["document_id"]
            print("published_documents", len(references), flush=True)

            def evaluate(case):
                token = accounts.principal.set(principal)
                tick = time.perf_counter()
                task = None
                heartbeat = None
                stop = threading.Event()
                worker_marker = None
                try:
                    with db.database() as cursor:
                        task, _ = store.create(cursor, uid, "knowledge_query", {"question": case["question"], "kb_ids": [kb_id], "rerank": False}, tag + case["id"])
                    task = runtime.claim(task["id"])
                    worker_marker = store.worker.set((task["id"], task["worker_id"]))
                    heartbeat = threading.Thread(target=runtime.heartbeat, args=(task["id"], task["worker_id"], stop, [time.monotonic()]), daemon=True)
                    heartbeat.start()
                    evidence = service.retrieve(task, top_k=20)
                    expected = {references[key] for key in case["sources"]}
                    retrieved = {p["document_id"] for p in evidence}
                    result = runtime.invoke_graph(service.answer_graph(), task["id"])["result"]
                    with db.database() as cursor:
                        current = store.get(cursor, task["id"], lock=True)
                        service.validate_evidence(cursor, uid, result.get("citations", []))
                        store.update(cursor, current, status="succeeded", result=result, finished_at=store.now(), lease_until=None, worker_id=None)
                    citations = {p["document_id"] for p in result.get("citations", [])}
                    return {"id": case["id"], "category": case["category"], "recall20": len(expected & retrieved) / len(expected) if expected else None,
                            "expected_sources_cited": bool(expected <= citations) if expected else not citations,
                            "reference_fact_found": all(str(value) in result["answer"] for value in case.get("expected_fragments", [])),
                            "insufficient": result["insufficient"], "answer": result["answer"], "citations": [p["content"] for p in result.get("citations", [])],
                            "seconds": round(time.perf_counter() - tick, 3), "status": "succeeded"}
                except Exception as exc:
                    return {"id": case["id"], "status": "failed", "error": type(exc).__name__, "detail": str(getattr(exc, "detail", exc))[:300], "seconds": round(time.perf_counter() - tick, 3)}
                finally:
                    stop.set()
                    if heartbeat:
                        heartbeat.join(timeout=1)
                    if worker_marker is not None:
                        store.worker.reset(worker_marker)
                    accounts.principal.reset(token)

            cases = fixture["questions"][:args.limit]
            with ThreadPoolExecutor(max_workers=2) as pool:
                for item in pool.map(evaluate, cases):
                    report["cases"].append(item)
                    print(item["id"], item["status"], flush=True)
            # Exercise SiliconFlow's real rerank metering through the ledger.
            with db.database() as cursor:
                task, _ = store.create(cursor, uid, "knowledge_query", {"question": "重排计量验证"}, tag + "rerank")
                store.update(cursor, task, status="running", lease_until=store.now() + timedelta(minutes=2))
            report["rerank_results"] = len(gateway.rerank(task["id"], "入馆需要什么", [{"content": "入馆需要预约码。"}, {"content": "停车收费。"}]))
            with db.database() as cursor:
                cursor.execute("SELECT phase,state,actual_tokens FROM agent_model_calls WHERE user_id=%s", (uid,))
                calls = cursor.fetchall()
            report["calls"] = {"count": len(calls), "actual_tokens": sum(c["actual_tokens"] or 0 for c in calls), "unknown": sum(c["state"] == "unknown" for c in calls)}
            successful = [c for c in report["cases"] if c["status"] == "succeeded"]
            recall = [c["recall20"] for c in successful if c["recall20"] is not None]
            times = sorted(c["seconds"] for c in report["cases"])
            report["metrics"] = {"cases": len(cases), "succeeded": len(successful), "recall20": statistics.mean(recall) if recall else None,
                                 "p50_seconds": statistics.median(times), "p95_seconds": times[min(len(times)-1, int(len(times)*.95))],
                                 "citation_metric": "expected source match; not a human correctness score", "cost_estimate": None,
                                 "cost_note": "供应商价格未配置，不虚构费用"}
        finally:
            if kb_id:
                client = storage.object_client()
                for item in client.list_objects(storage.bucket(), prefix=f"knowledge/{kb_id}/", recursive=True):
                    client.remove_object(storage.bucket(), item.object_name)
            with storage.search_client() as es:
                es.indices.delete(index=storage.index_name(), ignore_unavailable=True)
            with administrator.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{name}`")
            administrator.close()
            db.schema_ready = False
            report["elapsed_seconds"] = round(time.perf_counter()-started, 3)
            report["cleanup"] = {"database": name, "index": storage.index_name(), "objects_removed": True}
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report.get("metrics", {}), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-env", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=100)
    run(parser.parse_args())
