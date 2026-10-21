import itertools
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import httpx
from backend.infrastructure import generation
from backend.modules.video import schemas, summary
from backend.modules.video.timing import ExecutionTimer


def chunk(content=None, *, reasoning=None, finish=None, usage=None):
    return NS(choices=[] if usage else [NS(index=0, delta=NS(content=content, reasoning_content=reasoning),
                                         finish_reason=finish)], usage=usage)


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        return iter(self.chunks)


class ModelRequestTests(unittest.TestCase):
    config = {"base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V4-Flash", "api_key": "test"}

    def setUp(self):
        self.events = []
        token = generation.request_metrics.set(lambda event, value: self.events.append((event, value)))
        self.addCleanup(generation.request_metrics.reset, token)

    def client(self, *responses):
        return NS(chat=NS(completions=NS(create=Mock(side_effect=responses))), close=Mock())

    def test_summary_and_json_map_both_disable_thinking_for_supported_model(self):
        payload = json.dumps({"title": "主题", "nodes": [{"title": "要点", "summary": "字幕内容", "segment_ids": [0], "children": []}]})
        streams = [Stream([chunk("## 概览\n摘要"), chunk(finish="stop")]),
                   Stream([chunk(payload), chunk(finish="stop")])]
        client = self.client(*streams)
        segments = [schemas.SubtitleSegment(start=0, timestamp="0:00", text="字幕内容")]
        with patch.object(summary, "_load_ai_config", return_value=self.config), patch.object(summary, "OpenAI", return_value=client):
            result = summary._generate_ai_summary("标题", "https://example.com/video", segments)
            mind_map = summary._generate_ai_mind_map("标题", segments)
        self.assertIn("摘要", result)
        self.assertEqual(mind_map.nodes[0].segment_ids, [0])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        for call in client.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["extra_body"], {"enable_thinking": False})
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual(call.kwargs["stream_options"], {"include_usage": True})
        self.assertEqual(client.chat.completions.create.call_args_list[1].kwargs["response_format"], {"type": "json_object"})
        self.assertTrue(all(stream.closed for stream in streams))

    def test_other_services_and_unknown_models_keep_existing_protocol(self):
        for config in [self.config | {"base_url": "https://api.openai.com/v1"},
                       self.config | {"base_url": "https://api.siliconflow.cn.example.com/v1"},
                       self.config | {"model": "other-model"}]:
            self.assertEqual(summary._model_request_options(config), {})
        self.assertFalse(summary._model_request_options(self.config | {"base_url": "https://api.siliconflow.com/v1"})["extra_body"]["enable_thinking"])

    def test_stream_records_first_output_first_content_and_usage_without_reasoning_text(self):
        clock = [0.0]
        usage = NS(prompt_tokens=100, completion_tokens=30, completion_tokens_details=NS(reasoning_tokens=12))
        class TimedStream(Stream):
            def __iter__(self):
                for when, item in [(1, chunk()), (1.5, chunk(reasoning="不得保存的思考")), (2, chunk("正")),
                                   (2.2, chunk("文")), (2.5, chunk(finish="stop")), (2.6, chunk(usage=usage))]:
                    clock[0] = when
                    yield item
        stream = TimedStream([])
        with patch.object(summary.time, "monotonic", side_effect=lambda: clock[0]):
            result = summary._model_completion(self.client(stream), **summary._model_request_options(self.config))
        request = self.events[-1][1]
        self.assertEqual(result.choices[0].message.content, "正文")
        self.assertEqual(request["first_output_ms"], 1500)
        self.assertEqual(request["first_content_ms"], 2000)
        self.assertEqual(request["elapsed_ms"], 2600)
        self.assertEqual(request["reasoning_tokens"], 12)
        self.assertEqual(request["prompt_tokens"], 100)
        self.assertFalse(request["thinking_enabled"])
        self.assertNotIn("不得保存", json.dumps(request, ensure_ascii=False))

    def test_interrupted_stream_discards_partial_text_and_retries_once(self):
        streams = [Stream([chunk("丢弃残缺内容")]), Stream([chunk("完整正文"), chunk(finish="stop")])]
        with patch.object(summary.time, "monotonic", side_effect=itertools.count(0, .25)), patch.object(summary.time, "sleep"):
            result = summary._model_completion(self.client(*streams), stream=True)
        self.assertEqual(result.choices[0].message.content, "完整正文")
        requests = [value for event, value in self.events if event == "model_request"]
        self.assertEqual([request["status"] for request in requests], ["failed", "finished"])
        self.assertEqual([request["attempt"] for request in requests], [1, 2])
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertIn(("retry", 1000), self.events)

    def test_cancellation_closes_stream_without_retry(self):
        stream = Stream([chunk("正文"), chunk(finish="stop")])
        checks = [0]
        def checkpoint():
            checks[0] += 1
            if checks[0] == 3:
                raise RuntimeError("任务已取消")
        token = generation.request_checkpoint.set(checkpoint)
        try:
            with self.assertRaisesRegex(RuntimeError, "任务已取消"):
                summary._model_completion(self.client(stream), stream=True)
        finally:
            generation.request_checkpoint.reset(token)
        self.assertTrue(stream.closed)
        self.assertEqual(sum(event == "attempt" for event, _ in self.events), 1)
        self.assertEqual(self.events[-1][1]["status"], "failed")

    def test_stream_network_failure_exhausts_two_attempts_and_releases_capacity(self):
        class BrokenStream(Stream):
            def __iter__(self):
                yield chunk("残缺正文")
                raise httpx.ReadError("connection lost")
        streams = [BrokenStream([]), BrokenStream([])]
        with patch.object(summary.time, "monotonic", side_effect=itertools.count(0, .25)), \
                patch.object(summary.time, "sleep"), self.assertRaises(httpx.ReadError):
            summary._model_completion(self.client(*streams), stream=True)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(sum(event == "attempt" for event, _ in self.events), 2)
        self.assertTrue(generation.provider_slots.acquire(blocking=False))
        generation.provider_slots.release()

    def test_nonstreaming_and_missing_usage_never_fabricate_zero_or_first_token(self):
        response = NS(choices=[NS(message=NS(content="正文"))])
        summary._model_completion(self.client(response))
        request = self.events[-1][1]
        self.assertIsNone(request["first_content_ms"])
        self.assertIsNone(request["reasoning_tokens"])
        self.assertIsNone(request["thinking_enabled"])

    def test_timer_keeps_independent_request_metrics_without_changing_retry_wait(self):
        timer = ExecutionTimer()
        timer.start("map_model", "导图")
        timer.metric("map_model", "attempt")
        timer.metric("map_model", "retry", 1000)
        value = {"first_content_ms": 123, "reasoning_tokens": 0}
        timer.metric("map_model", "model_request", value)
        value["reasoning_tokens"] = 999
        saved = timer.snapshot()
        stage = saved["stages"][-1]
        self.assertEqual(stage["retry_wait_ms"], 1000)
        self.assertEqual(stage["model_requests"][0]["reasoning_tokens"], 0)
        stage["model_requests"][0]["first_content_ms"] = 999
        self.assertEqual(timer.snapshot()["stages"][-1]["model_requests"][0]["first_content_ms"], 123)


if __name__ == "__main__":
    unittest.main()
