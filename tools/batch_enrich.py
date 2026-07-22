#!/usr/bin/env python3
"""
全量批处理: Task A+B+C — 单次 DeepSeek API 推理同时输出情绪+人物+对话角色

对 11467 条谢渣渣消息逐条推理，结果直接写入 Neo4j。

用法:
  python tools/batch_enrich.py --sample 100   # 采样测试
  python tools/batch_enrich.py --all           # 全量
  python tools/batch_enrich.py --resume 5000   # 从第 5000 条恢复
"""

import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

SYSTEM_PROMPT = """分析以下谢渣渣的微信消息，输出JSON。三个任务一次完成:

1. emotion: 情绪分类 + 强度
   - neutral_ack / cold_response / warm_reply / angry / sad / joking
   - intensity: 0.0-1.0

2. persons: 这条消息提到了谁？（人名列表）
   - 从消息中提取提到的人物名字
   - 没有就输出空数组

3. conversation_role: 这条消息在对话中的角色
   - topic_opener（开启话题）/ response（回应）/ deflecting（敷衍/回避）/ ending（结束话题）/ escalation（情绪升级）/ standalone（独立发言）

只输出JSON，格式: {"emotion":"...","intensity":0.5,"persons":["名字1"],"conversation_role":"response"}"""


def enrich_message(content: str) -> dict:
    raw = generate(SYSTEM_PROMPT, content[:300], max_tokens=80, temperature=0.1)
    try:
        # 清理可能的 markdown 包裹
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1] if "\n" in clean else clean[3:]
            if clean.endswith("```"): clean = clean[:-3]
            if clean.startswith("json"): clean = clean[4:].strip()
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"emotion": "unknown", "intensity": 0.0, "persons": [], "conversation_role": "response"}


def get_target_messages(driver, limit: int = None, offset: int = 0) -> list:
    with driver.session() as s:
        query = (
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) > 5 "
            "RETURN m.msg_id AS mid, m.content AS content "
            "ORDER BY m.msg_id"
        )
        if limit:
            query += f" SKIP {offset} LIMIT {limit}"
        r = s.run(query)
        return [{"msg_id": rec["mid"], "content": rec["content"]} for rec in r]


def write_to_neo4j(driver, msg_id: int, enrichment: dict):
    """写入情绪属性 + 创建 MENTIONS 边 + 对话角色"""
    with driver.session() as s:
        # A: 情绪
        s.run(
            "MATCH (m:Message {msg_id: $mid}) "
            "SET m.emotion = $emotion, m.intensity = $intensity, "
            "    m.conversation_role = $role",
            mid=msg_id,
            emotion=enrichment.get("emotion", "unknown"),
            intensity=enrichment.get("intensity", 0.0),
            role=enrichment.get("conversation_role", "response"),
        )
        # B: 人物 MENTIONS 边
        for person_name in enrichment.get("persons", []):
            if person_name and len(person_name) >= 2:
                s.run(
                    "MERGE (pe:Person_Entity {name: $name}) "
                    "SET pe.entity_type = 'person', pe.role = 'mentioned'",
                    name=person_name,
                )
                s.run(
                    "MATCH (m:Message {msg_id: $mid}) "
                    "MATCH (pe:Person_Entity {name: $name}) "
                    "MERGE (m)-[:MENTIONS]->(pe)",
                    mid=msg_id, name=person_name,
                )


def print_progress(current, total, start_time, enrichment, content):
    elapsed = time.time() - start_time
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    print(f"[{current}/{total}] {rate:.1f}/s ETA:{eta/60:.0f}min | "
          f"情绪={enrichment.get('emotion','?')} 人物={enrichment.get('persons',[])} "
          f"角色={enrichment.get('conversation_role','?')} | {content[:50]}...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0, help="采样 N 条测试")
    parser.add_argument("--all", action="store_true", help="全量 11467 条")
    parser.add_argument("--resume", type=int, default=0, help="从第 N 条恢复")
    args = parser.parse_args()

    driver = GraphDatabase.driver(URI, auth=AUTH)

    if args.sample:
        messages = get_target_messages(driver, limit=args.sample)
        print(f"采样模式: {len(messages)} 条\n")
    elif args.all:
        messages = get_target_messages(driver, offset=args.resume)
        print(f"全量模式: {len(messages)} 条 (从 #{args.resume} 开始)\n")
    else:
        messages = get_target_messages(driver, limit=10)
        print(f"默认: {len(messages)} 条 (--all 跑全量, --sample N 采样)\n")

    start = time.time()
    stats = {"emotions": {}, "total_persons": 0, "roles": {}}

    for i, msg in enumerate(messages, 1):
        enrichment = enrich_message(msg["content"])
        write_to_neo4j(driver, msg["msg_id"], enrichment)

        # 统计
        e = enrichment.get("emotion", "unknown")
        stats["emotions"][e] = stats["emotions"].get(e, 0) + 1
        stats["total_persons"] += len(enrichment.get("persons", []))
        r = enrichment.get("conversation_role", "?")
        stats["roles"][r] = stats["roles"].get(r, 0) + 1

        if i % 50 == 0 or i <= 5:
            print_progress(i, len(messages), start, enrichment, msg["content"])

    elapsed = time.time() - start
    print(f"\n{'='*50}")
    print(f"完成: {len(messages)} 条, {elapsed:.0f}s ({elapsed/len(messages):.1f}s/条)")
    print(f"情绪分布: {json.dumps(stats['emotions'], ensure_ascii=False)}")
    print(f"人物提及: {stats['total_persons']} 次")
    print(f"对话角色: {json.dumps(stats['roles'], ensure_ascii=False)}")

    driver.close()


if __name__ == "__main__":
    main()
