from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

import tiktoken

from backend.agents import store
from backend.modules.knowledge import parsing, service


FOCUS = """# 场景歌单

### 25 分钟专注歌单示例

| 阶段 | 时长 | 选曲目的 |
| --- | ---: | --- |
| 进入状态 | 3 分钟 | 使用熟悉、音色温和的曲目 |
| 持续工作 | 19 分钟 | 保持相近的能量和音量 |
| 收尾整理 | 3 分钟 | 略微降低能量，整理笔记 |

以上三段合计 25 分钟。

## 运动

运动音乐需要结合训练阶段选择。
"""


class KnowledgeChunkTests(unittest.TestCase):
    def test_focus_answer_and_heading_are_in_one_retrievable_chunk(self):
        pieces = parsing.chunks(parsing.parse(FOCUS.encode(), "md"))
        focus = [p for p in pieces if "持续工作" in p["content"]]
        self.assertEqual(len(focus), 1)
        content = focus[0]["content"]
        for fact in ("25 分钟专注歌单", "3 分钟", "19 分钟", "收尾整理", "以上三段合计"):
            self.assertIn(fact, content)
        self.assertNotIn("运动音乐", content)

    def test_short_paragraphs_share_a_chunk_instead_of_exhausting_evidence_slots(self):
        text = "# 播放建议\n\n" + "\n\n".join(f"建议{i}：控制音量。" for i in range(8))
        pieces = parsing.chunks(parsing.parse(text.encode(), "md"))
        self.assertEqual(len(pieces), 1)
        for i in range(8):
            self.assertIn(f"建议{i}", pieces[0]["content"])

    def test_long_section_keeps_title_budget_and_source_offsets(self):
        paragraphs = ["# 专注音乐"] + [f"第{i}段：" + "温和音色有助于减少突兀变化。" * 15 for i in range(12)]
        text = "\n\n".join(paragraphs)
        pieces = parsing.chunks(parsing.parse(text.encode(), "md"))
        self.assertGreater(len(pieces), 2)
        encoding = tiktoken.get_encoding("cl100k_base")
        for piece in pieces:
            self.assertTrue(piece["content"].startswith("章节：专注音乐\n\n"))
            self.assertLessEqual(len(encoding.encode(piece["content"])), 600)
            locator = piece["locator"]
            body = piece["content"].split("\n\n", 1)[1]
            self.assertEqual(text[locator["offset_start"]:locator["offset_end"]], body)

    def test_long_heading_and_special_token_text_remain_parseable(self):
        text = "# " + "复杂章节" * 70 + "\n\n" + "<|endoftext|>仍然只是文档内容。" * 40
        pieces = parsing.chunks(parsing.parse(text.encode(), "md"))
        encoding = tiktoken.get_encoding("cl100k_base")
        self.assertTrue(pieces)
        self.assertTrue(all(len(encoding.encode(p["content"], disallowed_special=())) <= 600 for p in pieces))

    def test_pages_and_video_timestamps_keep_distinct_locators(self):
        pages = [{"text": "第一页", "locator": {"page": 1}}, {"text": "第二页", "locator": {"page": 2}}]
        pieces = parsing.chunks(pages)
        self.assertEqual([p["locator"]["page"] for p in pieces], [1, 2])
        segments = [{"text": "开场", "locator": {"start_ms": 0, "end_ms": 3000}},
                    {"text": "说明", "locator": {"start_ms": 3000, "end_ms": 6000}}]
        parsed = parsing.parse(store.encode({"segments": segments}).encode(), "video_subtitle")
        pieces = parsing.chunks(parsed)
        self.assertEqual([(p["locator"]["start_ms"], p["locator"]["end_ms"]) for p in pieces], [(0, 3000), (3000, 6000)])


class KnowledgeContextTests(unittest.TestCase):
    def piece(self, number, section="25 分钟专注歌单", version=1, document="document"):
        return {"chunk_id": f"{document}-{version}-{number}", "document_id": document, "kb_id": "private-kb",
                "version": version, "chunk_no": number, "content": f"原文{number}", "locator": {"section": section}}

    def expand(self, seeds, neighbors):
        filters = [{"terms": {"kb_id": ["private-kb"]}}, {"term": {"version": 1}}]
        client = MagicMock()
        client.__enter__.return_value.search.return_value = {"hits": {"hits": [{"_source": p} for p in neighbors]}}
        with patch.object(service.storage, "search_client", return_value=client):
            result = service.expand_context(seeds, ["videodream_rag_chunks_v1"], filters)
        request = client.__enter__.return_value.search.call_args.kwargs
        self.assertEqual(request["query"]["bool"]["filter"], filters)
        return result, request

    def test_legacy_heading_brings_back_table_without_changing_citation(self):
        title, table, note = [self.piece(i) for i in (5, 6, 7)]
        table["content"] = "进入3分钟；持续19分钟；收尾3分钟"
        original = deepcopy([title, table, note])
        result, request = self.expand([title, note], [table, note])
        self.assertEqual(result, [title, note, table])
        self.assertEqual([title, table, note], original)
        self.assertEqual(request["source_excludes"], ["embedding"])
        self.assertEqual(len({p["chunk_id"] for p in result}), len(result))

    def test_context_never_crosses_document_version_or_section(self):
        seed = self.piece(5)
        neighbors = [self.piece(6, section="运动"), self.piece(4, version=2), self.piece(4, document="other")]
        result, request = self.expand([seed], neighbors)
        self.assertEqual(result, [seed])
        constraints = request["query"]["bool"]["should"][0]["bool"]["filter"]
        self.assertIn({"term": {"document_id": "document"}}, constraints)
        self.assertIn({"term": {"version": 1}}, constraints)
        self.assertIn({"term": {"locator.section": "25 分钟专注歌单"}}, constraints)

    def test_context_does_not_cross_pdf_page(self):
        seed, neighbor = self.piece(0), self.piece(1)
        seed["locator"], neighbor["locator"] = {"page": 1}, {"page": 2}
        result, request = self.expand([seed], [neighbor])
        self.assertEqual(result, [seed])
        self.assertIn({"term": {"locator.page": 1}}, request["query"]["bool"]["should"][0]["bool"]["filter"])

    def test_empty_evidence_does_not_query_search(self):
        with patch.object(service.storage, "search_client") as client:
            self.assertEqual(service.expand_context([], [], []), [])
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
