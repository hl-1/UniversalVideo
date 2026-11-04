from __future__ import annotations

import io
import re
import zipfile

import tiktoken
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

PARSER_VERSION = "text-locators-v2"
CHUNK_CONFIG = {"size": 600, "overlap": 80, "splitter": "cl100k-section-recursive-v2",
                "locators": "page-section-offset-time-v1", "section_prefix_tokens": 120}


def parse(data, source_kind):
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "单文件上限为 20 MiB")
    if source_kind == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise HTTPException(422, "请先解密 PDF")
        segments = [{"text": page.extract_text() or "", "locator": {"page": i + 1}} for i, page in enumerate(reader.pages)]
    elif source_kind == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(v.file_size for v in archive.infolist()) > 64 * 1024 * 1024:
                raise HTTPException(413, "DOCX 解压内容过大")
        from docx import Document as WordDocument
        document = WordDocument(io.BytesIO(data))
        section, segments = "", []
        for paragraph in document.paragraphs:
            if paragraph.style and paragraph.style.name.startswith("Heading"):
                section = paragraph.text[:255]
            if paragraph.text.strip():
                segments.append({"text": paragraph.text, "locator": {"section": section}})
        for table in document.tables:
            for row in table.rows:
                segments.append({"text": " | ".join(c.text for c in row.cells), "locator": {"section": section}})
    elif source_kind in {"video_subtitle", "video_summary"}:
        import json
        segments = json.loads(data)["segments"]
    elif source_kind in {"md", "txt"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "Markdown/TXT 请使用 UTF-8 编码") from None
        section, segments = "", []
        for paragraph in re.split(r"\n\s*\n", text):
            heading = re.match(r"^#{1,6}\s+(.+)", paragraph)
            if heading:
                section = heading[1][:255]
            segments.append({"text": paragraph, "locator": {"section": section}})
    else:
        raise HTTPException(422, "仅支持文本 PDF、DOCX、Markdown 和 TXT")
    total = 0
    clean = []
    for segment in segments:
        text = str(segment["text"]).replace("\x00", "").strip()
        total += len(text)
        if total > 200000:
            raise HTTPException(413, "解析文本超过 200,000 字符")
        if text:
            clean.append({"text": text, "locator": segment.get("locator", {})})
    if not clean:
        raise HTTPException(422, "未解析到文本；扫描 PDF 需要先进行 OCR")
    return clean


def chunks(segments):
    # Keep paragraphs, headings and tables together within their source location.
    # Pages and timed segments remain distinct so citations retain their locator.
    groups = []
    for segment in segments:
        if groups and groups[-1]["locator"] == segment["locator"]:
            groups[-1]["texts"].append(segment["text"])
        else:
            groups.append({"texts": [segment["text"]], "locator": segment["locator"]})
    encoding = tiktoken.get_encoding("cl100k_base")
    result, base = [], 0
    for group in groups:
        text = "\n\n".join(group["texts"])
        section = group["locator"].get("section", "")
        prefix = f"章节：{section}\n\n" if section else ""
        while len(encoding.encode(prefix, disallowed_special=())) > CHUNK_CONFIG["section_prefix_tokens"]:
            section = section[:-1]
            prefix = f"章节：{section}\n\n"
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base", chunk_size=CHUNK_CONFIG["size"] - len(encoding.encode(prefix, disallowed_special=())),
            chunk_overlap=CHUNK_CONFIG["overlap"], add_start_index=True, disallowed_special=(),
            separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""])
        docs = splitter.split_documents([Document(page_content=text, metadata=group["locator"])])
        for doc in docs:
            metadata = dict(doc.metadata)
            start = metadata.pop("start_index", 0)
            result.append({"content": prefix + doc.page_content, "locator": metadata | {"offset_start": base + start,
                           "offset_end": base + start + len(doc.page_content)}})
        base += len(text) + 2
    return result
