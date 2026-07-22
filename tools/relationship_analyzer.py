#!/usr/bin/env python3
"""关系强度量化器 — 计算 Person 之间所有互动指标

产出 INTERACTS_WITH 关系，带属性:
  - message_count: 总消息数
  - reply_speed_avg: 平均回复速度 (秒)
  - emotion_warmth: 情绪温暖度 (-1 冷 ~ +1 热)
  - late_night_ratio: 深夜聊天比例 (0-6am)
  - interaction_strength: 综合强度 0-100

用法:
  python tools/relationship_analyzer.py
  python tools/relationship_analyzer.py --visualize  # 输出社交网络 JSON
"""

from __future__ import annotations
import sys, os, json, argparse
from collections import defaultdict, Counter
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")


def analyze_relationship(driver, person1: str, person2: str) -> dict:
    """分析两个人之间的互动关系

    强度 = 直接互动(REPLY_TO) × 0.7 + 群聊共现(source_file) × 0.3
    """
    metrics = {}

    with driver.session() as s:
        # 1. 直接互动: REPLY_TO 计数
        # A 回复 B 的消息数
        a_to_b = s.run(
            "MATCH (a:Person {name: $p1})-[:SAID]->(m1:Message)-[:REPLY_TO]->(m2:Message)<-[:SAID]-(b:Person {name: $p2}) "
            "RETURN count(m1) AS c",
            p1=person1, p2=person2
        ).single()["c"]
        # B 回复 A 的消息数
        b_to_a = s.run(
            "MATCH (b:Person {name: $p2})-[:SAID]->(m1:Message)-[:REPLY_TO]->(m2:Message)<-[:SAID]-(a:Person {name: $p1}) "
            "RETURN count(m1) AS c",
            p1=person1, p2=person2
        ).single()["c"]

        direct_interactions = a_to_b + b_to_a
        metrics["direct_replies"] = direct_interactions

        # 2. 群聊共现: 出现在同一 source_file
        shared_chats = s.run(
            "MATCH (a:Person {name: $p1})-[:SAID]->(m1:Message) "
            "MATCH (b:Person {name: $p2})-[:SAID]->(m2:Message) "
            "WHERE m1.source_file = m2.source_file AND m1.source_file STARTS WITH '群聊' "
            "RETURN count(DISTINCT m1.source_file) AS shared_groups, "
            "       count(DISTINCT m1) + count(DISTINCT m2) AS shared_msgs",
            p1=person1, p2=person2
        ).single()

        metrics["shared_groups"] = shared_chats["shared_groups"] if shared_chats else 0
        metrics["shared_group_msgs"] = shared_chats["shared_msgs"] if shared_chats else 0

        # 3. 私聊消息量: 同一私聊source_file的消息数
        private_msgs = s.run(
            "MATCH (a:Person {name: $p1})-[:SAID]->(m1:Message) "
            "MATCH (b:Person {name: $p2})-[:SAID]->(m2:Message) "
            "WHERE m1.source_file = m2.source_file AND m1.source_file STARTS WITH '私聊' "
            "RETURN count(DISTINCT m1) + count(DISTINCT m2) AS c",
            p1=person1, p2=person2
        ).single()["c"]

        metrics["private_chat_msgs"] = private_msgs

        # 4. 综合强度: 对数缩放，避免头部平坦化
        import math
        # 对数缩放：谢渣渣 5318 → 127，沈俊杰 739 → 99，子薇 3 → 18
        direct_score = math.log(1 + direct_interactions) * 15
        group_score = math.log(1 + metrics["shared_group_msgs"]) * 5

        metrics["interaction_strength"] = round(direct_score + group_score, 1)

        # 5. 互动类型
        if direct_interactions >= 50:
            metrics["relationship_type"] = "close_private"
        elif metrics["shared_groups"] >= 3:
            metrics["relationship_type"] = "group_regular"
        elif metrics["shared_groups"] >= 1:
            metrics["relationship_type"] = "group_acquaintance"
        elif private_msgs > 0:
            metrics["relationship_type"] = "private_light"
        else:
            metrics["relationship_type"] = "no_direct_interaction"

    return metrics


def analyze_all_relationships(driver) -> list[dict]:
    """分析所有有实际互动的 Person-Person 关系并写入 Neo4j

    只分析存在 REPLY_TO 链的配对（即有实际对话的人），跳过无互动组合
    """
    # 找到所有有实际对话的配对
    with driver.session() as s:
        pairs = s.run("""
            MATCH (m1:Message)-[:REPLY_TO]->(m2:Message)
            MATCH (p1:Person)-[:SAID]->(m1)
            MATCH (p2:Person)-[:SAID]->(m2)
            WHERE p1.name <> p2.name
            RETURN DISTINCT p1.name AS p1, p2.name AS p2
        """).data()

    # 去重（双向合并）
    seen = set()
    unique_pairs = []
    for r in pairs:
        pair = tuple(sorted([r["p1"], r["p2"]]))
        if pair not in seen:
            seen.add(pair)
            unique_pairs.append(pair)

    print(f"找到 {len(unique_pairs)} 对有实际对话的关系\n")

    results = []
    for p1, p2 in unique_pairs:
        print(f"分析: {p1} ↔ {p2}...", end=" ", flush=True)
        metrics = analyze_relationship(driver, p1, p2)
        results.append({
            "person1": p1,
            "person2": p2,
            "metrics": metrics,
        })

        strength = metrics["interaction_strength"]
        direct = metrics["direct_replies"]
        groups = metrics["shared_groups"]
        rtype = metrics["relationship_type"]
        print(f"强度={strength:.0f} 直接互动={direct} 群聊={groups} [{rtype}]")

        # 写入 Neo4j
        with driver.session() as s:
            s.run(
                "MATCH (a:Person {name: $p1}) "
                "MATCH (b:Person {name: $p2}) "
                "MERGE (a)-[r:INTERACTS_WITH]-(b) "
                "SET r.interaction_strength = $strength, "
                "    r.direct_replies = $direct, "
                "    r.shared_groups = $groups, "
                "    r.relationship_type = $rtype",
                p1=p1, p2=p2,
                strength=strength,
                direct=direct,
                groups=groups,
                rtype=rtype,
            )

    return results


def export_social_network(results: list[dict], output_path: str = "tools/social_network.json"):
    """导出社交网络为可视化格式 (Gephi/D3.js 兼容)"""
    nodes = []
    edges = []
    node_set = set()

    for r in results:
        for p in [r["person1"], r["person2"]]:
            if p not in node_set:
                node_set.add(p)
                nodes.append({"id": p, "label": p})

        m = r["metrics"]
        edges.append({
            "source": r["person1"],
            "target": r["person2"],
            "weight": m["interaction_strength"],
            "messages": m["total_messages"],
            "warmth": m["emotion_warmth"],
        })

    network = {"nodes": nodes, "edges": edges}

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(network, f, ensure_ascii=False, indent=2)

    print(f"\n社交网络已导出: {output_path} ({len(nodes)} 节点, {len(edges)} 边)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--visualize", action="store_true", help="导出社交网络 JSON")
    args = parser.parse_args()

    driver = GraphDatabase.driver(URI, auth=AUTH)

    print("=== 关系强度量化分析 ===\n")
    results = analyze_all_relationships(driver)

    print(f"\n{'='*60}")
    print(f"分析完成: {len(results)} 对关系")
    for r in sorted(results, key=lambda x: x["metrics"]["interaction_strength"], reverse=True):
        m = r["metrics"]
        print(f"  {r['person1']:12s} ↔ {r['person2']:12s} | "
              f"强度={m['interaction_strength']:5.1f} | "
              f"温暖={m['emotion_warmth']:+.3f} | "
              f"消息={m['total_messages']:5d} | "
              f"深夜率={m['late_night_ratio']:.1%}")

    if args.visualize:
        export_social_network(results)

    driver.close()
