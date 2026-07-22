#!/usr/bin/env python3
"""上下文感知标注 — 用焦爱家的上文来提升谢渣渣回复的情绪标注准确度

问题：标注谢渣渣的"哦哦"时，不知道焦爱家说了什么
解决：查 REPLY_TO 链，把焦爱家的话注入标注 prompt

用法:
  python tools/context_enrich.py --sample 50          # 测试 50 条
  python tools/context_enrich.py --all                 # 全量重标注
  python tools/context_enrich.py --long-only           # 只重标注长消息 (>8字)
"""

from __future__ import annotations
import sys, os, json, time, argparse
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# 带上下文的标注 prompt
CONTEXT_PROMPT = (
    '分析谢渣渣的微信回复。你会看到焦爱家(对方)的上文和谢渣渣的回复。\n'
    '输出JSON:\n'
    '{"emotion":"neutral_ack|cold_response|warm_reply|angry|sad|joking",'
    '"intensity":0.5,'
    '"persons":["名字"],'
    '"conversation_role":"response|topic_opener|deflecting|ending|escalation|standalone",'
    '"context_relevance":"direct_answer|deflecting|topic_shift|emotional_response|acknowledgment"}\n'
    'context_relevance 说明:\n'
    '- direct_answer: 直接回答了对方的问题\n'
    '- deflecting: 回避/敷衍对方的问题\n'
    '- topic_shift: 转移话题\n'
    '- emotional_response: 情绪化的回应（不回答问题，表达感受）\n'
    '- acknowledgment: 收到/确认（"哦哦""好的""知道了"）\n'
    '只输出JSON。'
)


def get_context_for_message(driver, msg_id: int) -> str | None:
    """获取某条消息的上文（焦爱家说了什么）"""
    with driver.session() as s:
        # 找 REPLY_TO 链：她的消息 → 焦爱家的消息
        result = s.run(
            "MATCH (her:Message {msg_id: $mid})-[r:REPLY_TO]->(his:Message) "
            "WHERE his.sender_role = 'self' "
            "RETURN his.content AS ctx, his.msg_id AS ctx_id "
            "LIMIT 1",
            mid=msg_id
        ).single()

        if result:
            return result["ctx"]

        # 如果没有 REPLY_TO 边，找时间上最近的前一条焦爱家消息
        result2 = s.run(
            "MATCH (her:Message {msg_id: $mid}) "
            "MATCH (his:Message) "
            "WHERE his.sender_role = 'self' AND his.msg_id < her.msg_id "
            "RETURN his.content AS ctx, his.msg_id AS ctx_id "
            "ORDER BY his.msg_id DESC LIMIT 1",
            mid=msg_id
        ).single()

        return result2["ctx"] if result2 else None


def enrich_with_context(content: str, context: str | None) -> dict:
    """带上下文标注单条消息"""
    if context:
        user_text = f"上文(焦爱家): \"{context[:150]}\"\n谢渣渣回复: \"{content[:200]}\""
    else:
        user_text = f"谢渣渣消息: \"{content[:200]}\" (无上文)"

    raw = generate(CONTEXT_PROMPT, user_text, max_tokens=60, temperature=0.1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "emotion": "unknown", "intensity": 0, "persons": [],
            "conversation_role": "response", "context_relevance": "unknown"
        }


def get_messages_to_enrich(driver, sample: int = 0, long_only: bool = False) -> list[dict]:
    """获取需要标注的消息列表"""
    with driver.session() as s:
        query = (
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE m.content IS NOT NULL AND size(m.content) > 0 "
            "  AND m.context_relevance IS NULL "  # 断点续传：跳过已标注
        )
        if long_only:
            query += "AND size(m.content) > 8 "

        query += (
            "RETURN m.msg_id AS mid, m.content AS c, size(m.content) AS l "
            "ORDER BY m.msg_id"
        )

        if sample:
            # 随机抽样
            query = (
                "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
                "WHERE m.content IS NOT NULL AND size(m.content) > 0 "
            )
            if long_only:
                query += "AND size(m.content) > 8 "
            query += "RETURN m.msg_id AS mid, m.content AS c, size(m.content) AS l ORDER BY rand() LIMIT " + str(sample)

        return s.run(query).data()


def write_to_neo4j(driver, msg_id: int, enrichment: dict):
    """写入标注结果"""
    with driver.session() as s:
        s.run(
            "MATCH (m:Message {msg_id: $mid}) "
            "SET m.emotion = $em, m.intensity = $int, "
            "    m.conversation_role = $role, m.context_relevance = $cr",
            mid=msg_id,
            em=enrichment.get("emotion", "neutral"),
            int=enrichment.get("intensity", 0),
            role=enrichment.get("conversation_role", "response"),
            cr=enrichment.get("context_relevance", "unknown"),
        )
        # 人物 MENTIONS
        for pn in enrichment.get("persons", []):
            if pn and len(pn) >= 2:
                s.run("MERGE (pe:Person_Entity {name: $n}) SET pe.entity_type='person'", n=pn)
                s.run(
                    "MATCH (m:Message {msg_id: $mid}) "
                    "MATCH (pe:Person_Entity {name: $n}) "
                    "MERGE (m)-[:MENTIONS]->(pe)",
                    mid=msg_id, n=pn
                )


def run(sample: int = 0, all_msgs: bool = False, long_only: bool = False):
    driver = GraphDatabase.driver(URI, auth=AUTH)

    messages = get_messages_to_enrich(driver, sample=sample, long_only=long_only)
    label = f"抽样{sample}" if sample else "全量"
    if long_only:
        label += " (仅长消息)"
    print(f"{label}: {len(messages)} 条")

    # 统计：多少条有上文
    with_context = 0
    without_context = 0

    start = time.time()
    stats = Counter()
    relevance_stats = Counter()
    buf = []
    B = 20

    for i, msg in enumerate(messages):
        ctx = get_context_for_message(driver, msg["mid"])
        if ctx:
            with_context += 1
        else:
            without_context += 1

        enrichment = enrich_with_context(msg["c"], ctx)
        buf.append((msg["mid"], enrichment))

        em = enrichment.get("emotion", "unknown")
        cr = enrichment.get("context_relevance", "unknown")
        stats[em] += 1
        relevance_stats[cr] += 1

        if len(buf) >= B or i == len(messages) - 1:
            for mid, e in buf:
                write_to_neo4j(driver, mid, e)
            buf = []

        if (i + 1) % 50 == 0 or i < 3:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(messages) - i - 1) / rate / 60 if rate > 0 else 0
            print(f"[{i+1}/{len(messages)}] {rate:.1f}/s ETA:{eta:.0f}min | "
                  f"有上文:{with_context} 无上文:{without_context} | "
                  f"{em} {enrichment.get('persons',[])} | {msg['c'][:30]}...",
                  flush=True)

    elapsed = time.time() - start
    print(f"\n✅ 完成 {len(messages)} 条 ({elapsed:.0f}s)")
    print(f"  有上文: {with_context} ({100*with_context/len(messages):.1f}%)")
    print(f"  无上文: {without_context} ({100*without_context/len(messages):.1f}%)")
    print(f"  情绪分布: {dict(stats.most_common())}")
    print(f"  上下文相关性: {dict(relevance_stats.most_common())}")

    driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--long-only", action="store_true")
    args = parser.parse_args()

    if args.sample:
        run(sample=args.sample, long_only=args.long_only)
    elif args.all:
        run(all_msgs=True, long_only=args.long_only)
    else:
        print("用法: --sample N (测试) | --all (全量) [--long-only]")
        print("示例: python tools/context_enrich.py --sample 20")
