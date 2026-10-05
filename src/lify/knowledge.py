"""只读歌曲知识卡：把推荐证据变成可验证的数据，不调用模型、不改歌曲事实。"""
from copy import deepcopy
import math


ROUTE_NAMES = {
    "audio": "听感",
    "text": "歌词主题",
    "catalog": "曲库条件",
    "topic_audit": "题材审核",
}


def _source_ref(track_id, route, evidence):
    """生成稳定证据引用；引用描述证据位置，不把估计升级成事实。"""
    version = evidence.get("version") or evidence.get("model")
    source = evidence.get("source")
    if not isinstance(version, str) or not version.strip() or not isinstance(source, str) or not source.strip():
        raise ValueError(f"歌曲 {track_id} 的 {route} 证据缺少来源或版本")
    return {
        "ref": f"track:{track_id}:{route}:{version}",
        "route": route,
        "source": str(source),
        "version": str(version),
    }


def build_track_card(item):
    """从一条候选构建 Track Knowledge Card；只陈述输入中已有的事实。"""
    track = item["track"]
    track_id = track["id"]
    evidence = item.get("evidence", {})
    refs, claims = {}, []
    for route, value in evidence.items():
        if not isinstance(value, dict):
            raise ValueError(f"歌曲 {track_id} 的 {route} 证据格式无效")
        ref = _source_ref(track_id, route, value)
        refs[route] = ref
        claim = {
            "kind": route,
            "label": ROUTE_NAMES.get(route, route),
            "query": value.get("query", ""),
            "evidence_ref": ref["ref"],
            "limitation": value.get("limitation", ""),
        }
        if route in {"audio", "text"} and value.get("similarity") is not None:
            claim["similarity"] = float(value["similarity"])
            if not math.isfinite(claim["similarity"]):
                raise ValueError("推荐解释的相似度必须是有限数值")
        if value.get("quote"):
            claim["quote"] = value["quote"]
        claims.append(claim)
    return {
        "document_id": f"track_card:{track_id}:{track['fingerprint']}",
        "track_id": track_id,
        "facts": {
            "title": track["title"],
            "artist": track["artist"],
            "duration_seconds": track["duration"],
            "language": track.get("language") or "unknown",
            "vocal": track.get("vocal") or "unknown",
        },
        "claims": claims,
        "source_refs": refs,
        "ranking_adjustments": deepcopy(item.get("ranking_adjustments", [])),
        # 展示估计画像，保持完整来源和版本；不写入 facts，不推导场景事实。
        **({"tag_estimates": deepcopy(track["audio_tag_profile"])} if track.get("audio_tag_profile") else {}),
        "limitations": sorted({c["limitation"] for c in claims if c.get("limitation")}),
    }


def validate_track_card(card, item):
    """验证解释中的每条 claim 都能回指同一歌曲的真实 evidence。"""
    track = item["track"]
    if card.get("track_id") != track["id"]:
        raise ValueError("歌曲知识卡关联了错误的 track_id")
    if card.get("document_id") != f"track_card:{track['id']}:{track['fingerprint']}":
        raise ValueError("歌曲知识卡版本与文件指纹不一致")
    evidence = item.get("evidence", {})
    refs = card.get("source_refs", {})
    for claim in card.get("claims", []):
        route = claim.get("kind")
        raw = evidence.get(route)
        ref = refs.get(route)
        if not raw or not ref or claim.get("evidence_ref") != ref.get("ref"):
            raise ValueError("推荐解释缺少可追溯证据")
        expected = _source_ref(track["id"], route, raw)
        if ref != expected:
            raise ValueError("推荐解释引用与原始证据不一致")
        if claim.get("quote") and claim["quote"] not in track.get("lyrics", {}).get("text", ""):
            raise ValueError("推荐解释引用并非歌曲歌词原文")
    # 校验完整投影，防止删除 claims、改分数/查询或替换 facts 后仍通过。
    # 这里只证明 card 与输入 evidence 一致，不证明模型判断正确。
    if card != build_track_card(item):
        raise ValueError("歌曲知识卡内容与原始事实或证据不一致")
    return card


def attach_track_cards(result):
    """在返回结果中附加知识卡；重复执行保持幂等。"""
    for item in result.get("items", []):
        card = build_track_card(item)
        validate_track_card(card, item)
        item["knowledge_card"] = card
    return result
