"""Trend Radar stage 4: Claude maps candidates onto the closed Intent_Type set.

Returns a list of verdict dicts (one per kept candidate, DROPs removed).
Model and effort come from radar.yml:llm. Credentials: ANTHROPIC_API_KEY.
Server-side refusal fallbacks are enabled (fallbacks="default").
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic
import yaml

log = logging.getLogger("radar.classify")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

INTENTS = {
    "Tool_Comparison":      {"page": "对比页", "schema": "SoftwareApplication", "when": "'XX vs YY' 类热点、竞品新版本 / 新竞品出现、竞品动态"},
    "How_To_Tutorial":      {"page": "教程页", "schema": "HowTo", "when": "新技术、新工作流讨论，用户想学怎么做"},
    "Use_Case_Industry":    {"page": "行业解决方案页", "schema": "FAQPage", "when": "游戏 / 影视 / 电商 / 教育 / 3D 打印等垂直行业新闻或平台政策变化"},
    "Feature_Trend_Tie_in": {"page": "功能页 + 博客", "schema": "Article", "when": "AI 新模型 / 新技术发布，可与 Meshy 某个功能挂钩"},
    "Community_Showcase":   {"page": "博客（案例研究）", "schema": "Article", "when": "创作者作品、案例走红、社区挑战"},
    "Pricing_Value":        {"page": "定价对比区块", "schema": "Product", "when": "竞品涨价、免费额度变化、平台分成政策变化"},
    "FAQ_Troubleshoot":     {"page": "帮助中心", "schema": "FAQPage", "when": "常见报错、使用困惑、'how to download / convert / fix' 类搜索"},
}


def build_system(redlines: dict) -> str:
    ip = redlines.get("protected_ip", {}) or {}
    ip_flat = sorted({t for group in ip.values() for t in (group or []) if isinstance(t, str)})
    return f"""你是 Meshy 的 SEO 选题编辑。Meshy 是 AI 3D 模型生成工具（text-to-3D、image-to-3D、AI texturing、auto-rigging、animation、retopology、3D agent），用户是游戏开发者、3D 艺术家、3D 打印爱好者、独立创作者、电商与教育从业者。

任务：对每条候选热点判断能否"搭桥"回 Meshy 的业务与搜索意图，并映射到一个封闭的意图类型；搭不上就 DROP。宁缺毋滥：每天只需要 3-5 条真正值得做页面的选题。

意图类型只能从以下集合选择（严格，不得新造）：
{json.dumps(INTENTS, ensure_ascii=False, indent=1)}

判定规则：
1. 桥接必须自然：读者搜这个热点时，Meshy 的某个功能或页面是他们下一步真的会用到的东西。牵强的"蹭"直接 DROP。
2. 站内已有页面（existing_page_overlap 字段，来自 meshy.ai sitemap）：
   - 已有同主题页 → decision = update_existing，填 target_url，不新建（避免关键词蚕食）
   - 有相邻页但角度不同 → decision = new_page，并在 internal_links 列出应内链的相邻页
   - 只有单一来源、非 Recurring、且事件本身不大 → decision = watch
3. 已在队列中的选题（下面 already_queued 列表）：同一事件不要重复入队，除非有实质新进展；重复 → DROP 并标 duplicate。
4. 红线：命中即 DROP，并在 risk_flags 标注原因：
   - 受保护 IP 作为页面主题（IP 只作行业新闻背景可保留）。IP 列表：{", ".join(ip_flat)}
   - 政治、宗教、战争、灾难、疾病、暴力、成人、赌博、加密货币炒作
   - 竞品品牌词的歧义噪音（如 "zara sale" 之于 Tripo，"love scenario lyrics" 之于 Scenario）
   - 与创作者工作流无关的工业 / 医疗 / 建筑 3D 打印与扫描新闻
   - 页面角度依赖以下任何一种承诺：{json.dumps(redlines.get('forbidden_claims', []), ensure_ascii=False)}
5. 每条给 confidence（0-1）。低于 0.5 的用 watch 而不是 new_page。

只输出一个 JSON 数组，不要解释文字、不要 Markdown 代码块。数组元素格式：
{{"id": <候选 id>, "decision": "new_page|update_existing|watch|drop",
 "intent_type": "<集合内的键，drop 时填 DROP>", "page_type": "<对应页面类型>",
 "topic_en": "<英文工作标题，可直接作为页面初稿标题>", "target_keyword": "<主目标搜索词>",
 "bridge": "<一句话：读者为什么会从这个热点走到 Meshy>",
 "target_url": "<update_existing 时填站内路径，否则空字符串>", "internal_links": ["<站内路径>"],
 "risk_flags": ["IP"|"brand_noise"|"sensitive"|"off_topic"|"forbidden_claim"|"duplicate"|"weak_bridge"],
 "confidence": 0.0}}"""


def classify(candidates: list[dict], already_queued: list[str], cfg: dict, redlines: dict) -> tuple[list[dict], dict]:
    llm = cfg.get("llm", {})
    model = llm.get("model", "claude-opus-5")
    payload = [
        {"id": c["id"], "title": c["title"], "score": c["score"], "sources": c["sources"][:6],
         "source_types": c["source_types"], "meta": c["meta"][:2],
         "existing_page_overlap": c["existing_page_overlap"], "urls": c.get("urls", [])[:2]}
        for c in candidates[: int(llm.get("top_n", 90))]
    ]
    user = ("already_queued（最近已进入审核队列的选题标题）：\n" + json.dumps(already_queued, ensure_ascii=False)
            + "\n\n候选热点（JSON）：\n" + json.dumps(payload, ensure_ascii=False))
    system = [{"type": "text", "text": build_system(redlines), "cache_control": {"type": "ephemeral"}}]
    client = anthropic.Anthropic()
    kwargs = dict(model=model, max_tokens=16000, system=system,
                  thinking={"type": "adaptive"}, output_config={"effort": llm.get("effort", "medium")},
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:  # older SDK without `fallbacks`
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"classification refused: {getattr(resp, 'stop_details', None)}")
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise RuntimeError(f"no JSON array in model output: {text[:300]!r}")
    verdicts = json.loads(text[start:end + 1])
    by_id = {c["id"]: c for c in candidates}
    kept = []
    for v in verdicts:
        c = by_id.get(v.get("id"))
        if not c:
            continue
        if v.get("decision") == "drop" or v.get("intent_type") not in INTENTS:
            continue
        v["page_type"] = v.get("page_type") or INTENTS[v["intent_type"]]["page"]
        v["schema_type"] = INTENTS[v["intent_type"]]["schema"]
        kept.append({**v, "candidate": c})
    order = {"new_page": 0, "update_existing": 1, "watch": 2}
    kept.sort(key=lambda v: (order.get(v["decision"], 9), -float(v.get("confidence", 0)), -v["candidate"]["score"]))
    usage = resp.usage.to_dict() if hasattr(resp.usage, "to_dict") else dict(resp.usage)
    log.info("classified %d candidates -> %d kept; usage=%s", len(payload), len(kept), usage)
    return kept, {"model": model, "usage": usage, "n_input": len(payload), "n_kept": len(kept)}
