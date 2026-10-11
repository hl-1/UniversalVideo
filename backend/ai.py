from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from backend import accounts


def load_config(modality: Literal["text", "vision", "speech"] = "text", *, legacy_path: Path | None = None) -> dict[str, str]:
    if accounts.principal.get():
        settings = accounts.settings()
        config = {field: settings[f"ai_{field}"] for field in ("api_key", "model", "base_url")}
        vision_model = settings.get("ai_vision_model", "")
        asr_model = settings.get("ai_asr_model", "")
    else:
        config = accounts.local_ai_config()
        vision_model = accounts.environment("LLM_VISION_MODEL", "")
        asr_model = accounts.environment("LLM_ASR_MODEL", "")
        if not config["api_key"]:
            path = legacy_path or accounts.ROOT / "config" / "ai.json"
            if not path.is_file():
                raise RuntimeError("缺少 AI 配置：请填写 .env.universal-video.local 或根据 config/ai.example.json 创建 config/ai.json。")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RuntimeError("AI 配置文件不是合法 JSON。") from exc
            config = {"api_key": str(payload.get("api_key") or os.getenv("OPENAI_API_KEY") or "").strip(),
                      "model": str(payload.get("model") or "").strip(), "base_url": str(payload.get("base_url") or "").strip()}
            vision_model = vision_model or str(payload.get("vision_model") or "").strip()
            asr_model = asr_model or str(payload.get("asr_model") or "").strip()
    if not config["api_key"]:
        raise RuntimeError("AI 配置缺少 api_key，请在管理后台配置 AI 服务密钥。")
    if modality == "vision":
        vision_model = str(vision_model or "").strip()
        if not vision_model:
            raise RuntimeError("请在管理后台配置截图识别模型，或填写 LLM_VISION_MODEL。")
        config = {**config, "model": vision_model}
    if modality == "speech":
        asr_model = str(asr_model or "").strip()
        if not asr_model:
            raise RuntimeError("视频没有可用字幕，请在管理后台配置语音转写模型，或填写 LLM_ASR_MODEL。")
        config = {**config, "model": asr_model}
    if not config["model"]:
        raise RuntimeError("AI 配置缺少 model。")
    return config
