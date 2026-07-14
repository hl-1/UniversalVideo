import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend import main


def transcript(count: int = 12) -> list[main.SubtitleSegment]:
    return [
        main.SubtitleSegment(start=float(index * 10), end=float(index * 10 + 5), timestamp=f"0:{index * 10:02d}", text=f"字幕 {index}")
        for index in range(count)
    ]


def finished_summary(task_id: str = "summary123") -> main.SummaryTask:
    task = main.SummaryTask(task_id, "https://www.bilibili.com/video/BV1eKMn6MEHo/")
    task.status = "finished"
    task.result = main.SummaryResult(
        title="测试视频",
        webpage_url=task.url,
        language="zh-CN",
        source="人工字幕",
        summary_markdown="摘要",
        transcript=transcript(),
    )
    return task


class MindMapSanitizerTests(unittest.TestCase):
    def test_prompt_chunks_are_bounded_and_cover_all_segment_indexes(self):
        segments = [
            main.SubtitleSegment(start=float(index), timestamp=f"0:{index:02d}", text="长字幕" * 5000)
            for index in range(4)
        ]

        chunks = main._mind_map_prompt_chunks(segments)

        self.assertTrue(all(len(prompt) <= main.MAX_SUMMARY_TRANSCRIPT_CHARS for prompt, _ in chunks))
        self.assertEqual(set().union(*(ids for _, ids in chunks)), {0, 1, 2, 3})
        self.assertTrue(chunks[0][0].startswith("[0] [0:00]"))

    def test_sanitizer_resolves_references_and_timestamp(self):
        payload = {
            "title": "学习地图",
            "nodes": [{"title": "核心", "summary": "说明", "segment_ids": [2, 1, 99], "children": []}],
        }

        result = main._sanitize_mind_map_payload(payload, transcript(), "默认标题")

        self.assertEqual(result.title, "学习地图")
        self.assertEqual(result.nodes[0].segment_ids, [1, 2])
        self.assertEqual(result.nodes[0].timestamp, 10.0)
        self.assertEqual(result.nodes[0].id, "node-1")

    def test_sanitizer_drops_invalid_nodes_and_limits_tree(self):
        nested = {"title": "第五层", "segment_ids": [0], "children": []}
        for level in range(4, 0, -1):
            nested = {"title": f"第{level}层", "segment_ids": [level], "children": [nested]}
        payload = {
            "title": "",
            "nodes": [
                {"title": "", "segment_ids": [0]},
                {"title": "无引用", "segment_ids": [999]},
                nested,
                *[{"title": f"节点 {index}", "segment_ids": [index]} for index in range(11)],
            ],
        }

        result = main._sanitize_mind_map_payload(payload, transcript(), "默认标题")

        self.assertEqual(result.title, "默认标题")
        self.assertEqual(len(result.nodes), 10)
        current = result.nodes[0]
        depth = 1
        while current.children:
            depth += 1
            current = current.children[0]
        self.assertEqual(depth, 4)

    def test_sanitizer_skips_malformed_sibling_without_losing_later_nodes(self):
        payload = {
            "nodes": [None, "bad", {"title": "保留节点", "segment_ids": [1], "children": []}],
        }

        result = main._sanitize_mind_map_payload(payload, transcript(), "默认标题")

        self.assertEqual([node.title for node in result.nodes], ["保留节点"])

    def test_sanitizer_rejects_fully_invalid_payload(self):
        with self.assertRaisesRegex(RuntimeError, "没有有效节点"):
            main._sanitize_mind_map_payload({"title": "空", "nodes": []}, transcript(), "默认标题")

    def test_sanitizer_rejects_reference_not_present_in_prompt_chunk(self):
        payload = {"nodes": [{"title": "猜测节点", "segment_ids": [5]}]}

        with self.assertRaisesRegex(RuntimeError, "没有有效节点"):
            main._sanitize_mind_map_payload(payload, transcript(), "默认标题", allowed_segment_ids={0, 1})


class MindMapTaskTests(unittest.TestCase):
    def setUp(self):
        main.summary_tasks.clear()
        main.mind_map_tasks.clear()

    def tearDown(self):
        main.summary_tasks.clear()
        main.mind_map_tasks.clear()

    def test_create_reuses_finished_task_unless_regenerated(self):
        summary = finished_summary()
        main.summary_tasks[summary.task_id] = summary
        existing = main.MindMapTask("map123", summary.task_id)
        existing.status = "finished"
        existing.result = main.MindMapResult(title="旧结果", nodes=[], generated_at="2026-07-13T00:00:00Z")
        main.mind_map_tasks[summary.task_id] = existing

        reused = main.create_mind_map_endpoint(summary.task_id, regenerate=False)

        self.assertEqual(reused.task_id, "map123")
        with patch.object(main.executor, "submit") as submit:
            regenerated = main.create_mind_map_endpoint(summary.task_id, regenerate=True)
        self.assertNotEqual(regenerated.task_id, "map123")
        submit.assert_called_once()

    def test_create_retries_failed_task_without_regenerate_flag(self):
        summary = finished_summary()
        main.summary_tasks[summary.task_id] = summary
        failed = main.MindMapTask("failed123", summary.task_id)
        failed.status = "failed"
        main.mind_map_tasks[summary.task_id] = failed

        with patch.object(main.executor, "submit") as submit:
            retried = main.create_mind_map_endpoint(summary.task_id)

        self.assertNotEqual(retried.task_id, failed.task_id)
        submit.assert_called_once()

    def test_get_builds_response_while_task_lock_is_held(self):
        summary = finished_summary()
        main.summary_tasks[summary.task_id] = summary
        task = main.MindMapTask("map123", summary.task_id)
        main.mind_map_tasks[summary.task_id] = task

        with patch.object(task, "to_response", side_effect=lambda: self._assert_locked_response(task)):
            response = main.mind_map_task_endpoint(summary.task_id)

        self.assertEqual(response.task_id, task.task_id)

    def _assert_locked_response(self, task):
        self.assertTrue(main.mind_map_tasks_lock.locked())
        return task.to_response.__wrapped__() if hasattr(task.to_response, "__wrapped__") else main.MindMapTask.to_response(task)

    def test_runner_uses_full_internal_transcript(self):
        summary = finished_summary()
        summary.transcript = transcript(600)
        summary.result.transcript = summary.transcript[:500]
        task = main.MindMapTask("map123", summary.task_id)
        result = main.MindMapResult(
            title="地图",
            nodes=[main.MindMapNode(id="node-1", title="节点", timestamp=0, segment_ids=[0], children=[])],
            generated_at="2026-07-13T00:00:00Z",
        )

        with patch.object(main, "_generate_ai_mind_map", return_value=result) as generate:
            main._run_mind_map(task, summary)

        self.assertEqual(len(generate.call_args.args[1]), 600)

    def test_create_rejects_unfinished_summary(self):
        task = finished_summary()
        task.status = "running"
        main.summary_tasks[task.task_id] = task

        with self.assertRaises(HTTPException) as context:
            main.create_mind_map_endpoint(task.task_id)

        self.assertEqual(context.exception.status_code, 409)

    def test_generator_rejects_invalid_json(self):
        fake_completion = type(
            "Completion",
            (),
            {"choices": [type("Choice", (), {"message": type("Message", (), {"content": "not-json"})()})()]},
        )()
        fake_client = type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": type("Completions", (), {"create": lambda self, **kwargs: fake_completion})()})()},
        )()
        with patch.object(main, "_load_ai_config", return_value={"api_key": "test", "model": "test"}), patch.object(
            main, "OpenAI", return_value=fake_client
        ):
            with self.assertRaisesRegex(RuntimeError, "合法 JSON"):
                main._generate_ai_mind_map("标题", transcript())


if __name__ == "__main__":
    unittest.main()
