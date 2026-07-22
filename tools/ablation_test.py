#!/usr/bin/env python3
"""Ablation 测试 — 量化每个系统组件对回复质量的贡献

8 种配置 (3 组件 × on/off):
  Full | No-KG | No-Style | No-Session | KG-Only | Style-Only | Session-Only | None

用法:
  python tools/ablation_test.py --n 20
"""

from __future__ import annotations
import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from style_scorer import score as style_score
from emotion_classifier import classify, mood_delta
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# 配置定义
CONFIGS = {
    "Full":           {"kg": True,  "style": True,  "session": True},
    "No-KG":          {"kg": False, "style": True,  "session": True},
    "No-Style":       {"kg": True,  "style": False, "session": True},
    "No-Session":     {"kg": True,  "style": True,  "session": False},
    "KG-Only":        {"kg": True,  "style": False, "session": False},
    "Style-Only":     {"kg": False, "style": True,  "session": False},
    "Session-Only":   {"kg": False, "style": False, "session": True},
    "None":           {"kg": False, "style": False, "session": False},
}

# 真实分布
REAL_MEAN = 6.5
REAL_EMOJI_RATE = 0.056

BASE_SYSTEM_PROMPT = "你是谢渣渣，北航中法未来科技学院的大学女生。回复极短、口语化。"


def get_kg_context(driver, query: str) -> str:
    """模拟 Neo4j 图查询（简化版）"""
    try:
        from semantic_query import expand_keywords, graph_semantic_search
        keywords = expand_keywords(query)
        results = graph_semantic_search(driver, keywords, limit=5)
        if results:
            memories = [r["content"][:60] for r in results[:3]]
            return "相关记忆:\n" + "\n".join(f"- {m}" for m in memories)
    except:
        pass
    return ""


def get_session_context(mood: float, intimacy: float) -> str:
    """模拟 session state 注入"""
    rules = []
    if intimacy < 0.3:
        rules.append("RULE: 不要用'爱家''焦焦'等昵称")
    if mood < -0.3:
        rules.append("RULE: 回复≤8字，不加emoji")
    if mood > 0.3:
        rules.append("STYLE: 可以稍微热情一点")
    return "\n".join(rules) if rules else ""


def generate_reply(driver, his_msg: str, config: dict,
                   mood: float = 0.0, intimacy: float = 0.5) -> str:
    """按配置生成回复"""
    prompt_parts = [BASE_SYSTEM_PROMPT]

    # KG 组件
    if config["kg"]:
        kg_ctx = get_kg_context(driver, his_msg)
        if kg_ctx:
            prompt_parts.append(kg_ctx)

    # Session 组件
    if config["session"]:
        session_ctx = get_session_context(mood, intimacy)
        if session_ctx:
            prompt_parts.append(session_ctx)

    system_prompt = "\n".join(prompt_parts)
    user_text = f"焦爱家: \"{his_msg[:100]}\"\n谢渣渣回复:"
    raw = generate(system_prompt, user_text, max_tokens=30, temperature=0.7)
    return raw.strip().strip('"').strip().replace("谢渣渣:", "").replace("回复:", "").strip()


def score_reply(reply: str, use_style_checker: bool) -> dict:
    """对单条回复打分"""
    scores = {}

    # 长度分 (对比真实分布)
    length = len(reply)
    if length <= 0:
        scores["length"] = 0
    elif length <= 8:
        scores["length"] = 90  # 短发，接近真实
    elif length <= 15:
        scores["length"] = 70
    elif length <= 30:
        scores["length"] = 50
    else:
        scores["length"] = 20  # 太长

    # 风格合规
    if use_style_checker:
        r = style_score(reply)
        scores["style"] = 100 if r.get("passed", True) else r.get("style_score", 3) * 20
    else:
        scores["style"] = 80  # 无检查器时默认中等

    # Emoji 使用
    has_emoji = "[" in reply and "]" in reply
    has_wangchai = "[旺柴]" in reply
    if has_wangchai:
        scores["emoji"] = 0
    elif has_emoji and len(reply) <= 8:
        scores["emoji"] = 65  # 有 emoji 但短，勉强
    elif not has_emoji:
        scores["emoji"] = 95  # 无 emoji，好
    else:
        scores["emoji"] = 50

    # 综合
    scores["composite"] = scores.get("length", 50) * 0.4 + \
                          scores.get("style", 50) * 0.4 + \
                          scores.get("emoji", 50) * 0.2
    return scores


def run_ablation(n: int = 20):
    """主流程"""
    driver = GraphDatabase.driver(URI, auth=AUTH)

    # 1. 抽样测试消息（焦爱家的消息作为输入）
    with driver.session() as s:
        test_msgs = s.run(
            "MATCH (p:Person {role:'self'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) > 5 AND m.content IS NOT NULL "
            "RETURN m.content AS c "
            "ORDER BY rand() LIMIT $n", n=n
        ).data()

    print(f"=== Ablation 测试 ({len(test_msgs)} 条消息 × {len(CONFIGS)} 配置) ===\n")

    all_results = {}
    start = time.time()

    for config_name, config in CONFIGS.items():
        print(f"▶ {config_name} (KG={config['kg']}, Style={config['style']}, Session={config['session']})")
        config_start = time.time()

        replies = []
        mood = 0.0
        intimacy = 0.5

        for msg in test_msgs:
            reply = generate_reply(driver, msg["c"], config, mood, intimacy)

            # Session state 更新
            if config["session"]:
                em = classify(reply)
                mood += mood_delta(em)
                mood = max(-1, min(1, mood))

            replies.append(reply)

        # 打分
        total_scores = {"length": [], "style": [], "emoji": [], "composite": []}
        for reply in replies:
            s = score_reply(reply, config["style"])
            for k in total_scores:
                total_scores[k].append(s.get(k, 50))

        avg_scores = {k: round(sum(v)/len(v), 1) for k, v in total_scores.items()}

        elapsed = time.time() - config_start
        print(f"  {avg_scores['composite']:.1f}分 | "
              f"长度={avg_scores['length']:.1f} 风格={avg_scores['style']:.1f} Emoji={avg_scores['emoji']:.1f} | "
              f"{elapsed:.0f}s")

        all_results[config_name] = {
            "config": config,
            "avg_scores": avg_scores,
            "sample_replies": replies[:3],
        }

    total_elapsed = time.time() - start
    print(f"\n{'='*70}")
    print(f"Ablation 结果对比 ({total_elapsed:.0f}s)")
    print(f"{'='*70}")
    print(f"{'配置':15s} {'综合':>6s} {'长度':>6s} {'风格':>6s} {'Emoji':>6s} {'Δ vs Full':>8s}")
    print("-" * 55)

    full_score = all_results["Full"]["avg_scores"]["composite"]

    # 按综合分排序
    sorted_configs = sorted(all_results.items(), key=lambda x: x[1]["avg_scores"]["composite"], reverse=True)
    for name, data in sorted_configs:
        s = data["avg_scores"]
        delta = s["composite"] - full_score
        delta_str = f"{delta:+.1f}" if delta != 0 else "—"
        print(f"{name:15s} {s['composite']:6.1f} {s['length']:6.1f} {s['style']:6.1f} {s['emoji']:6.1f} {delta_str:>8s}")

    # 贡献度分析
    print(f"\n📊 组件贡献度分析:")
    print(f"{'组件':15s} {'开启时平均分':>12s} {'关闭时平均分':>12s} {'贡献度':>8s}")
    print("-" * 50)

    components = [
        ("Neo4j (KG)", "kg"),
        ("Style Scorer", "style"),
        ("Session State", "session"),
    ]

    for comp_name, comp_key in components:
        with_on = [d["avg_scores"]["composite"] for name, d in all_results.items() if d["config"][comp_key]]
        with_off = [d["avg_scores"]["composite"] for name, d in all_results.items() if not d["config"][comp_key]]
        avg_on = sum(with_on) / len(with_on) if with_on else 0
        avg_off = sum(with_off) / len(with_off) if with_off else 0
        contribution = avg_on - avg_off
        bar = "█" * max(0, int(contribution)) + "░" * max(0, int(10 - contribution))
        print(f"{comp_name:15s} {avg_on:>12.1f} {avg_off:>12.1f} {contribution:>+7.1f}  {bar}")

    # 保存结果
    output = {
        "n": n,
        "full_score": full_score,
        "configs": {name: {"avg_scores": d["avg_scores"]} for name, d in all_results.items()},
        "component_contributions": {},
    }
    for comp_name, comp_key in components:
        with_on = [d["avg_scores"]["composite"] for name, d in all_results.items() if d["config"][comp_key]]
        with_off = [d["avg_scores"]["composite"] for name, d in all_results.items() if not d["config"][comp_key]]
        output["component_contributions"][comp_name] = {
            "avg_on": round(sum(with_on)/len(with_on), 1) if with_on else 0,
            "avg_off": round(sum(with_off)/len(with_off), 1) if with_off else 0,
            "contribution": round(output["component_contributions"].get(comp_name, {}).get("contribution", 0)
                                 if isinstance(output["component_contributions"].get(comp_name, {}), dict)
                                 else (sum(with_on)/len(with_on) - sum(with_off)/len(with_off)), 1),
        }
        output["component_contributions"][comp_name]["contribution"] = round(
            output["component_contributions"][comp_name]["avg_on"] -
            output["component_contributions"][comp_name]["avg_off"], 1
        )

    out_path = os.path.join(os.path.dirname(__file__), "ablation_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out_path}")

    driver.close()
    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    args = parser.parse_args()
    run_ablation(n=args.n)
