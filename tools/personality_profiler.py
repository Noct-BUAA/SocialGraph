#!/usr/bin/env python3
"""
Task D: 人格特征向量 — 500条采样 → 量化验证 PART B

输出 personality_profile.json → 每个维度有分数 + 原文证据
"""

import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from neo4j import GraphDatabase
from collections import Counter

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

SYSTEM_PROMPT = """分析以下谢渣渣的微信消息（采样自11467条对话记录），提取结构化人格剖面。输出JSON:

{
  "expression_style": {
    "avg_length": 数字(平均字数),
    "emoji_frequency": 0.0-1.0,
    "swear_frequency": 0.0-1.0,
    "sentence_splitting": 0.0-1.0(是否倾向拆成多条短消息),
    "formality": 0.0-1.0(0=极口语,1=正式)
  },
  "emotional_patterns": {
    "dominant_emotion": "最常见情绪",
    "mood_volatility": 0.0-1.0(情绪变化剧烈程度),
    "cold_triggers": ["触发她冷淡的话题/行为"],
    "warm_triggers": ["触发她热情的话题/行为"]
  },
  "social_network": {
    "most_mentioned": ["她最常提到的人 前3"],
    "closest_relationship": "最亲密的人",
    "conflict_relationships": ["关系紧张的人"]
  },
  "self_perception": {
    "core_identity": "她怎么定义自己(一句话)",
    "insecurities": ["不安全感来源"],
    "values": ["她看重的价值观 前3"]
  },
  "communication_quirks": {
    "signature_phrases": ["标志性口头禅 前3"],
    "humor_style": "幽默风格(一句话)",
    "conflict_style": "吵架模式(一句话)"
  }
}

基于这些消息样本推断，每个字段尽量给出，不确定的标注\"insufficient_data\"。
只输出JSON。"""


def sample_messages(driver, n=500) -> list:
    with driver.session() as s:
        r = s.run(
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) > 10 "
            "RETURN m.content AS c ORDER BY rand() LIMIT $n",
            n=n,
        )
        return [rec["c"] for rec in r]


def build_sample_text(messages: list) -> str:
    """每 20 条合并为一段，让 Qwen 能看到上下文"""
    chunks = []
    for i in range(0, len(messages), 20):
        batch = messages[i:i+20]
        chunks.append("\n".join(f"- {m}" for m in batch))
    return "\n\n---\n\n".join(chunks[:25])  # 最多 25 段（500条）


def compute_stats(messages: list, profile: dict) -> dict:
    """补充基于简单统计的量化指标（不需要 Qwen）"""
    lengths = [len(m) for m in messages]
    emoji_count = sum(1 for m in messages if "[" in m and "]" in m)
    swear_count = sum(1 for m in messages if any(w in m for w in ["草","吗的","傻逼","他妈","我日"]))
    return {
        "actual_avg_length": round(sum(lengths) / len(lengths), 1),
        "actual_emoji_rate": round(emoji_count / len(messages), 3),
        "actual_swear_rate": round(swear_count / len(messages), 3),
        "sample_size": len(messages),
    }


def main():
    print("Task D: 人格特征向量分析")
    driver = GraphDatabase.driver(URI, auth=AUTH)

    # 采样
    print("采样 500 条消息...")
    messages = sample_messages(driver, 500)
    print(f"  采样 {len(messages)} 条")

    # 合并为分析文本
    sample_text = build_sample_text(messages)
    print(f"  合并为 {len(sample_text)} 字符的分析文本")

    # Qwen 分析
    print("Qwen 分析中...")
    start = time.time()
    raw = generate(SYSTEM_PROMPT, sample_text[:8000], max_tokens=500, temperature=0.1)
    elapsed = time.time() - start

    # 解析
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1] if "\n" in clean else clean[3:]
            if clean.endswith("```"): clean = clean[:-3]
            if clean.startswith("json"): clean = clean[4:].strip()
        profile = json.loads(clean)
    except json.JSONDecodeError:
        profile = {"error": "parse_failed", "raw": raw[:200]}

    # 补充统计
    stats = compute_stats(messages, profile)
    profile["_computed_stats"] = stats

    # 保存
    profile["_metadata"] = {
        "sample_size": len(messages),
        "analysis_time_s": round(elapsed, 1),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    out_path = "tools/personality_profile.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"分析完成 ({elapsed:.1f}s)")
    print(f"统计: 平均长度={stats['actual_avg_length']}字, "
          f"emoji率={stats['actual_emoji_rate']}, 脏话率={stats['actual_swear_rate']}")
    if "expression_style" in profile:
        es = profile["expression_style"]
        print(f"Qwen: 口语化={es.get('formality','?')}, "
              f"拆句倾向={es.get('sentence_splitting','?')}")
    print(f"\n完整报告: {out_path}")

    driver.close()


if __name__ == "__main__":
    main()
