"""Versioned, declarative selection rules. No uploaded code is evaluated."""
from __future__ import annotations

import time
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, model_validator
from backend.modules.social import store as s
from backend.modules.social.models import Input
from backend.modules.music.agent.scenes import SCENES


class Filters(Input):
    languages: list[str] = Field(default_factory=list, max_length=8)
    excluded_themes: list[str] = Field(default_factory=list, max_length=20)
    excluded_artists: list[str] = Field(default_factory=list, max_length=20)
    excluded_tracks: list[str] = Field(default_factory=list, max_length=50)
    vocals: Literal["allowed", "instrumental"] = "allowed"
    bpm_min: float | None = Field(default=None, ge=0, le=300)
    bpm_max: float | None = Field(default=None, ge=0, le=300)
    energy_min: float | None = Field(default=None, ge=0, le=1)
    energy_max: float | None = Field(default=None, ge=0, le=1)
    variation_max: float | None = Field(default=None, ge=0, le=1)
    rhythm_stability_min: float | None = Field(default=None, ge=0, le=1)
    unknown_feature: Literal["exclude", "allow_unscored"] = "exclude"

    @model_validator(mode="after")
    def ranges(self):
        for field in ("bpm", "energy"):
            low, high = getattr(self, field + "_min"), getattr(self, field + "_max")
            if low is not None and high is not None and low > high:
                raise ValueError("筛选下限不能超过上限")
        for values in (self.languages, self.excluded_themes, self.excluded_artists, self.excluded_tracks):
            if any(not value.strip() or len(value) > 100 for value in values):
                raise ValueError("筛选项不能为空或超过 100 字")
        return self


class Ranking(Input):
    scene: int = Field(default=70, ge=0, le=100)
    artist: int = Field(default=15, ge=0, le=100)
    personal: int = Field(default=15, ge=0, le=100)

    @model_validator(mode="after")
    def positive(self):
        if not self.scene + self.artist + self.personal:
            raise ValueError("至少设置一个非零权重")
        return self


class Diversity(Input):
    max_per_artist: int = Field(default=2, ge=1, le=20)
    reduce_duplicates: bool = True


class Output(Input):
    goal: Literal["count", "duration"] = "count"
    target_count: int = Field(default=20, ge=1, le=50)
    target_duration_minutes: int = Field(default=45, ge=1, le=300)


class Config(Input):
    scene: str = Field(default="放松", max_length=80)
    filters: Filters = Field(default_factory=Filters)
    ranking: Ranking = Field(default_factory=Ranking)
    diversity: Diversity = Field(default_factory=Diversity)
    source_mode: Literal["personal_first", "personal_only", "discover"] = "personal_first"
    output: Output = Field(default_factory=Output)

    @model_validator(mode="after")
    def valid_scene(self):
        if self.scene not in SCENES:
            raise ValueError("请选择支持的场景")
        return self


class RuleInput(Input):
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=10)
    config: Config
    change_note: str = Field(default="", max_length=500)
    origin_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class RuleAction(Input):
    action: Literal["publish", "withdraw", "save", "unsave", "follow", "unfollow"]
    version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def version(cursor, version_id, owner=False):
    cursor.execute("""SELECT v.*,r.author_id,r.status,r.title,r.summary,r.published_version_id FROM social_rule_versions v
        JOIN social_rules r ON r.id=v.rule_id WHERE v.id=%s""", (version_id,))
    row = s.decode(cursor.fetchone(), "config")
    if row["status"] != "published" and row["author_id"] != s.uid() or owner and row["author_id"] != s.uid():
        raise HTTPException(404, "规则版本不可用。")
    if row["author_id"] != s.uid() and row["id"] != row["published_version_id"]:
        cursor.execute("SELECT 1 FROM social_rule_library WHERE user_id=%s AND version_id=%s AND saved=TRUE", (s.uid(), version_id))
        if not cursor.fetchone():
            raise HTTPException(404, "该版本尚未发布或未收藏。")
    return row


def listing(mine=False):
    with s.database() as cursor:
        cursor.execute("""SELECT r.*,u.username AS author_name,l.version_id AS saved_version_id,l.saved,l.follow_updates
            FROM social_rules r LEFT JOIN users u ON u.id=r.author_id
            LEFT JOIN social_rule_library l ON l.rule_id=r.id AND l.user_id=%s
            WHERE """ + ("r.author_id=%s" if mine else "r.status='published'") + " ORDER BY r.updated_at DESC LIMIT 100", (s.uid(), s.uid()) if mine else (s.uid(),))
        return {"items": [s.decode(x, "tags") for x in cursor.fetchall()], "scenes": SCENES}


def get(key):
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_rules WHERE id=%s AND (status='published' OR author_id=%s)", (key, s.uid()))
        row = s.decode(cursor.fetchone(), "tags")
        cursor.execute("SELECT * FROM social_rule_versions WHERE rule_id=%s AND (%s=%s OR id=%s OR id IN (SELECT version_id FROM social_rule_library WHERE user_id=%s AND saved=TRUE)) ORDER BY version DESC",
            (key, row["author_id"], s.uid(), row["published_version_id"], s.uid()))
        row["versions"] = [s.decode(x, "config") for x in cursor.fetchall()]
        return row


def save(payload, key=None):
    s.enabled("social_rules_enabled")
    s.rate("rule-save", 30)
    now, version_id = time.time(), s.identifier()
    config = payload.config.model_dump()
    with s.database() as cursor:
        if key:
            cursor.execute("SELECT * FROM social_rules WHERE id=%s AND author_id=%s FOR UPDATE", (key, s.uid()))
            s.decode(cursor.fetchone())
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM social_rule_versions WHERE rule_id=%s", (key,))
            number = cursor.fetchone()["n"]
            cursor.execute("UPDATE social_rules SET title=%s,summary=%s,tags=%s,scene=%s,updated_at=%s WHERE id=%s", (payload.title, payload.summary, s.encode(payload.tags), config["scene"], now, key))
        else:
            key, number = s.identifier(), 1
            if payload.origin_version_id:
                version(cursor, payload.origin_version_id)
            cursor.execute("""INSERT INTO social_rules (id,author_id,title,summary,tags,scene,origin_version_id,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (key, s.uid(), payload.title, payload.summary, s.encode(payload.tags), config["scene"], payload.origin_version_id, now, now))
        cursor.execute("""INSERT INTO social_rule_versions (id,rule_id,version,schema_version,engine_version,config,content_hash,change_note,created_at)
            VALUES (%s,%s,%s,'1','social-rule-1',%s,%s,%s,%s)""", (version_id, key, number, s.encode(config), s.digest(s.encode(config)), payload.change_note, now))
    return get(key)


def action(key, payload):
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_rules WHERE id=%s FOR UPDATE", (key,))
        row = s.decode(cursor.fetchone())
        if payload.action in {"publish", "withdraw"}:
            if row["author_id"] != s.uid():
                raise HTTPException(403, "仅作者可发布或下架规则。")
            selected = payload.version_id or row["published_version_id"]
            if payload.action == "publish":
                candidate = version(cursor, selected, True)
                if candidate["rule_id"] != key:
                    raise HTTPException(422, "规则版本不匹配。")
            cursor.execute("UPDATE social_rules SET status=%s,published_version_id=%s,updated_at=%s WHERE id=%s", ("published" if payload.action == "publish" else "withdrawn", selected, time.time(), key))
            if payload.action == "publish":
                cursor.execute("SELECT user_id FROM social_rule_library WHERE rule_id=%s AND follow_updates=TRUE AND version_id<>%s", (key, selected))
                for follower in cursor.fetchall():
                    s.notify(cursor, follower["user_id"], "rule_update", key, {"rule_id": key, "version_id": selected})
        else:
            if row["status"] != "published":
                raise HTTPException(410, "规则已下架。")
            selected = payload.version_id or row["published_version_id"]
            candidate = version(cursor, selected)
            if candidate["rule_id"] != key:
                raise HTTPException(422, "版本不属于此规则。")
            cursor.execute("SELECT saved,follow_updates FROM social_rule_library WHERE user_id=%s AND rule_id=%s", (s.uid(), key))
            previous = cursor.fetchone() or {"saved": False, "follow_updates": False}
            saved = payload.action != "unsave" if payload.action in {"save", "unsave"} else previous["saved"]
            following = payload.action == "follow" if payload.action in {"follow", "unfollow"} else previous["follow_updates"]
            cursor.execute("""INSERT INTO social_rule_library (user_id,rule_id,version_id,saved,follow_updates,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                version_id=VALUES(version_id),saved=VALUES(saved),follow_updates=VALUES(follow_updates),updated_at=VALUES(updated_at)""",
                (s.uid(), key, selected, saved, following, time.time(), time.time()))
    return {"ok": True}


def evaluate(track, config):
    """Return score and reasons; missing audio evidence cannot earn points."""
    config = Config.model_validate(config).model_dump()
    filters, reasons = config["filters"], []
    analysis = track.get("analysis") or {}
    features = analysis.get("features") or track.get("features") or {}
    evidence = (analysis.get("evidence") or track.get("evidence") or {}).get("fields") or {}
    def proven(field, full=False):
        proof = evidence.get(field) or {}
        if proof.get("confidence", 0) < .7 or full and proof.get("coverage") != "full":
            return None
        return features.get(field)
    from backend.modules.music.discovery.identity import artist_members
    artists = artist_members(track.get("artist", ""))
    if any(x.casefold() in {a.casefold() for a in artists} for x in filters["excluded_artists"]):
        return None, ["歌手已排除"]
    if track.get("title") in filters["excluded_tracks"] or track.get("key") in filters["excluded_tracks"]:
        return None, ["歌曲已排除"]
    checks = [("language", filters["languages"], "语言"), ("vocals", [False] if filters["vocals"] == "instrumental" else [], "人声")]
    for field, wanted, label in checks:
        value = proven(field)
        if wanted and value is None:
            if filters["unknown_feature"] == "exclude":
                return None, [label + "未知"]
            reasons.append(label + "未知，不计分")
        elif wanted and value not in wanted:
            return None, [label + "不符合"]
    themes = proven("themes")
    if filters["excluded_themes"]:
        if themes is None and filters["unknown_feature"] == "exclude":
            return None, ["主题未知"]
        if set(themes or []) & set(filters["excluded_themes"]):
            return None, ["包含排除主题"]
    for field, bound, operator in (("bpm", "bpm_min", "min"), ("bpm", "bpm_max", "max"), ("energy", "energy_min", "min"), ("energy", "energy_max", "max"), ("variation", "variation_max", "max"), ("rhythm_stability", "rhythm_stability_min", "min")):
        limit = filters[bound]
        if limit is None:
            continue
        value = proven("tempo" if field == "bpm" else field, full=True)
        if value is None:
            if filters["unknown_feature"] == "exclude":
                return None, [field + "未知"]
            reasons.append(field + "未知，不计分")
        elif operator == "min" and value < limit or operator == "max" and value > limit:
            return None, [field + "超出范围"]
    ranking = config["ranking"]
    match = track.get("match") or {}
    from backend.modules.music.agent.features import scene_relevance
    scene_score = scene_relevance(features, evidence, {"scene": config["scene"], "effect": ""})
    components = {"scene": scene_score, "artist": 1 if match.get("matched_artists") else None,
                  "personal": 1 if track.get("personal") or track.get("source", {}).get("kind") == "personal" else None}
    score = sum(max(0, min(1, value / 100 if value > 1 else value)) * ranking[key] for key, value in components.items() if isinstance(value, (int, float))) / sum(ranking.values())
    return score, reasons + ["满足已知筛选条件"]


def select(tracks, config):
    from backend.modules.music.discovery.identity import artist_members
    selected, rejected, candidates, counts, seen = [], [], [], {}, set()
    for track in tracks:
        score, reasons = evaluate(track, config)
        if score is None:
            rejected.append({"title": track["title"], "reasons": reasons})
        else:
            candidates.append((score, track, reasons))
    total = 0
    for score, track, reasons in sorted(candidates, key=lambda x: x[0], reverse=True):
        artists = artist_members(track.get("artist", ""))
        identity = track.get("key") or track.get("identifier") or track["title"]
        if any(counts.get(a, 0) >= config["diversity"]["max_per_artist"] for a in artists) or config["diversity"]["reduce_duplicates"] and identity in seen:
            rejected.append({"title": track["title"], "reasons": ["歌手数量或重复限制"]})
            continue
        output = config["output"]
        if output["goal"] == "count" and len(selected) >= output["target_count"] or output["goal"] == "duration" and total >= output["target_duration_minutes"] * 60:
            break
        selected.append(track | {"rule_score": score, "rule_reasons": reasons})
        total += track.get("duration") or 0
        for artist in artists:
            counts[artist] = counts.get(artist, 0) + 1
        seen.add(identity)
    return selected, rejected


def trial(config):
    s.rate("rule-trial", 15)
    with s.database() as cursor:
        cursor.execute("""SELECT t.*,f.features,f.evidence FROM music_tracks t LEFT JOIN music_track_features f
            ON f.user_id=t.user_id AND f.track_key=t.id AND f.expires_at>%s WHERE t.user_id=%s LIMIT 500""", (time.time(), s.uid()))
        tracks = []
        for row in cursor.fetchall():
            row = s.decode(row, "features", "evidence")
            tracks.append(row | {"personal": True})
    chosen, rejected = select(tracks, config.model_dump())
    return {"selected": chosen, "rejected": rejected, "usage": {"model_calls": 0, "network_requests": 0}, "message": "试跑使用已有音乐库和分析结果，不联网、不调用模型。"}


def prepare_request(request):
    s.enabled("social_rules_enabled")
    with s.database() as cursor:
        selected = version(cursor, request["rule_version_id"])
        if selected["status"] != "published":
            raise HTTPException(410, "规则已下架，不能创建新任务。")
    config = Config.model_validate(selected["config"]).model_dump()
    overrides = request.get("preferences", {})
    defaults = {"scene": config["scene"], "languages": config["filters"]["languages"],
        "vocals": config["filters"]["vocals"], "exclusions": config["filters"]["excluded_themes"],
        "source_mode": config["source_mode"], **config["output"]}
    preferences = defaults | overrides
    config = apply_preferences(config, preferences)
    return request | {"preferences": preferences, "effective_rule": config}


def apply_preferences(config, preferences):
    config = Config.model_validate(config).model_dump()
    for field in ("scene", "source_mode"):
        if field in preferences:
            config[field] = preferences[field]
    for field, target in (("languages", "languages"), ("vocals", "vocals"), ("exclusions", "excluded_themes")):
        if field in preferences:
            config["filters"][target] = preferences[field]
    config["output"].update({k: preferences[k] for k in config["output"] if k in preferences})
    return Config.model_validate(config).model_dump()
