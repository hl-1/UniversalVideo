from __future__ import annotations

import json
import re

import tiktoken
from openai import OpenAI

from backend import ai, library
from backend.music_agent import budget, store
from backend.music_agent.models import ParsedNeed


def json_value(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def complete(run_id, messages, tools=None, max_output=2000, decision=False):
    config = ai.load_config()
    with library.database() as cursor:
        row = store.get(cursor, run_id)
    from backend.music_agent.service import check_active
    check_active(run_id)
    from backend import accounts
    if (not row["request"].get("parsed", {}).get("network_allowed", True)
            or not accounts.settings()["music_agent_network_enabled"]):
        raise budget.BudgetExceeded("已禁止新增网络分析请求")
    payload = store.encode({"messages": messages, "tools": tools or []})
    tokenizer = tiktoken.get_encoding("cl100k_base")
    # Reserve conservatively before dispatch; reservations are never refunded on errors/retries.
    reserved = max(len(tokenizer.encode(payload, disallowed_special=()))+256, len(payload.encode())//2)+max_output
    budget.charge(run_id, tokens=reserved, **({"decisions": 1} if decision else {"analysis": 1}))
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"], max_retries=0, timeout=45)
    mode = row["context"]["limits"].get("api", "auto")
    if mode == "auto":
        mode = "responses" if config["model"].startswith(("gpt-6", "gpt-5.6")) else "chat"
    if mode == "responses":
        converted = []
        for m in messages:
            if m["role"] == "tool":
                converted.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
            elif m.get("tool_calls"):
                for call in m["tool_calls"]:
                    converted.append({"type": "function_call", "call_id": call["id"], **call["function"]})
            else:
                converted.append(m)
        response = client.responses.create(model=config["model"], input=converted, max_output_tokens=max_output, store=False,
                                           **({"tools": [{"type": "function", **t["function"]} for t in tools]} if tools else {}))
        calls = [{"id": c.call_id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                 for c in response.output if c.type == "function_call"]
        text = response.output_text
        actual = response.usage.total_tokens if response.usage else reserved
    else:
        response = client.chat.completions.create(model=config["model"], messages=messages, max_completion_tokens=max_output,
                                                  **({"tools": tools, "tool_choice": "auto"} if tools else {}))
        message = response.choices[0].message
        calls = [c.model_dump() for c in (message.tool_calls or []) if c.type == "function"]
        text = message.content or ""
        actual = response.usage.total_tokens if response.usage else reserved
    if actual > reserved:
        budget.charge(run_id, tokens=actual-reserved)
    with library.database() as cursor:
        row = store.get(cursor, run_id, lock=True)
        if row["status"] == "running":
            used = row["context"].get("actual_tokens", 0)+actual
            store.update(cursor, row, context=row["context"] | {"actual_tokens": used})
    return {"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})}


def offline_parse(need, preferences):
    result = ParsedNeed().model_dump()
    for scene, terms in {"睡眠": ["睡", "助眠"], "工作": ["工作", "专注", "学习"], "游戏": ["游戏"],
                         "运动": ["运动", "跑步"], "励志": ["励志"], "情歌": ["情歌"], "放松": ["放松", "低落"]}.items():
        if any(t in need for t in terms):
            result["scene"] = scene
            break
    if "纯音乐" in need or "无人声" in need:
        result["vocals"] = "instrumental"
    for term, language in [("英文", "英文"), ("日文", "日文"), ("韩文", "韩文"), ("中文", "中文")]:
        if term in need:
            result["languages"] = [language]
    if "语言不限" in need:
        result["languages"] = []
    if "只用" in need or "仅个人" in need:
        result["source_mode"] = "personal_only"
    if "新歌" in need or "探索" in need:
        result["source_mode"] = "discover"
    if "不要联网" in need or "不联网" in need:
        result["network_allowed"] = False
    count = re.search(r"(\d+)\s*首", need)
    if count:
        result["target_count"] = min(50, max(1, int(count[1])))
    duration = re.search(r"(?:凑够|听满|至少)(?:(\d+)\s*分钟|(一|1)\s*小时)", need)
    if duration:
        result["goal"] = "duration"
        result["target_duration_minutes"] = min(300, int(duration[1] or 60))
    for exclusion in re.findall(r"(?:不要|排除)([^，,。；;]+)", need):
        if "联网" not in exclusion:
            if any(t in exclusion for t in ["失恋", "伤感", "爱情"]):
                result["excluded_themes"].append(exclusion)
            else:
                result["exclusions"].append(exclusion)
    result.update(preferences)
    for item in result["exclusions"][:]:
        if any(term in item for term in ("失恋","伤感","爱情","励志")):
            result["excluded_themes"].append(item)
            result["exclusions"].remove(item)
    result["query"] = result["scene"]
    result["title"] = result["scene"]+" · 场景歌单"
    return ParsedNeed.model_validate(result).model_dump()


def parse(run_id, request, skill):
    fallback = offline_parse(request["need"], request.get("preferences", {}))
    with library.database() as cursor:
        row = store.get(cursor,run_id)
    if not fallback["network_allowed"] or not row["context"]["limits"]["network_enabled"]:
        return fallback
    response = complete(run_id, [
        {"role": "system", "content": skill["text"]+"\nParse the need. Return only JSON matching this schema: "+store.encode(skill["schema"])},
        {"role": "user", "content": store.encode({"need": request["need"], "explicit_preferences": request.get("preferences", {})})}], decision=True)
    parsed = ParsedNeed.model_validate(json_value(response["content"])).model_dump()
    parsed.update(request.get("preferences", {}))
    for item in parsed["exclusions"][:]:
        if any(term in item for term in ("失恋","伤感","爱情","励志")):
            parsed["excluded_themes"].append(item)
            parsed["exclusions"].remove(item)
    return ParsedNeed.model_validate(parsed).model_dump()
