#!/usr/bin/env python3
"""
Neo4j 知识图谱补充导入 — 实体 + 事实 + 关系

用法：
    python tools/kg_loader.py --entities tools/entities.json --facts tools/facts.json \
        --uri bolt://127.0.0.1:7687 --user neo4j --password neo4j123
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from neo4j import GraphDatabase


def load_entities(driver, entities: list[dict]) -> int:
    """导入实体节点"""
    count = 0
    with driver.session() as s:
        for e in entities:
            etype = e["type"]
            if etype == "person":
                s.run(
                    "MERGE (n:Entity:Person_Entity {name: $name}) "
                    "SET n.entity_id = $eid, n.entity_type = 'person', n.role = $role, "
                    "n.aliases = $aliases",
                    name=e["name"], eid=e["entity_id"],
                    role=e.get("role", "unknown"),
                    aliases=e.get("aliases", []),
                )
            elif etype == "topic":
                s.run(
                    "MERGE (n:Entity:Topic {name: $name}) "
                    "SET n.entity_id = $eid, n.entity_type = 'topic', "
                    "n.keywords = $keywords, n.mention_score = $score",
                    name=e["name"], eid=e["entity_id"],
                    keywords=e.get("keywords", []),
                    score=e.get("mention_score", 0),
                )
            elif etype == "event":
                s.run(
                    "MERGE (n:Entity:Event {name: $name}) "
                    "SET n.entity_id = $eid, n.entity_type = 'event', "
                    "n.first_mention = $first, n.last_mention = $last",
                    name=e["name"], eid=e["entity_id"],
                    first=e.get("first_mention", ""),
                    last=e.get("last_mention", ""),
                )
            count += 1
    return count


def load_facts_to_neo4j(driver, facts: list[dict]) -> int:
    """导入事实节点 + DERIVED_FROM 关系"""
    count = 0
    with driver.session() as s:
        batch_size = 500
        for i in range(0, len(facts), batch_size):
            batch = facts[i:i + batch_size]
            params = [
                {
                    "fid": f["fact_id"],
                    "subject": f["subject"],
                    "predicate": f["predicate"],
                    "object": f["object"][:200],
                    "msg_id": f["source_msg_id"],
                    "category": f["category"],
                    "confidence": f["confidence"],
                }
                for f in batch
            ]
            s.run(
                "UNWIND $batch AS row "
                "CREATE (f:Fact {"
                "  fact_id: row.fid, subject: row.subject, "
                "  predicate: row.predicate, object: row.object, "
                "  msg_id: row.msg_id, category: row.category, "
                "  confidence: row.confidence"
                "}) "
                "WITH f, row "
                "MATCH (m:Message {msg_id: row.msg_id}) "
                "MATCH (p:Person {name: row.subject}) "
                "CREATE (f)-[:DERIVED_FROM]->(m) "
                "CREATE (p)-[:HAS_FACT]->(f)",
                batch=params,
            )
            count += len(batch)
            print(f"  [{min(100, (i+len(batch))*100//len(facts))}%] {count}/{len(facts)} 事实节点")
    return count


def link_entities_to_messages(driver) -> int:
    """创建 MENTIONS / ABOUT 关系 — 将实体链接到消息"""
    count = 0
    with driver.session() as s:
        # Person_Entity → Message (MENTIONS)
        r = s.run(
            "MATCH (e:Person_Entity), (m:Message) "
            "WHERE m.content CONTAINS e.name "
            "AND e.name <> '谢渣渣' AND e.name <> '焦爱家' AND e.name <> 'Jajfandy' "
            "WITH e, m LIMIT 5000 "
            "CREATE (m)-[:MENTIONS]->(e) "
            "RETURN count(*) AS cnt"
        )
        count += r.single()["cnt"]

        # Topic → Message (ABOUT)
        r2 = s.run(
            "MATCH (t:Topic), (m:Message) "
            "UNWIND t.keywords AS kw "
            "WITH t, m, kw WHERE m.content CONTAINS kw "
            "WITH DISTINCT t, m "
            "LIMIT 10000 "
            "CREATE (m)-[:ABOUT]->(t) "
            "RETURN count(*) AS cnt"
        )
        count += r2.single()["cnt"]
    return count


def link_knows_relationships(driver) -> int:
    """创建 KNOWS 关系 — 共现人物"""
    count = 0
    with driver.session() as s:
        r = s.run(
            "MATCH (p1:Person_Entity), (p2:Person_Entity) "
            "WHERE id(p1) < id(p2) "
            "MATCH (m1:Message)-[:MENTIONS]->(p1) "
            "MATCH (m1)-[:MENTIONS]->(p2) "
            "WITH p1, p2, count(DISTINCT m1) AS co_occurrence "
            "WHERE co_occurrence >= 2 "
            "CREATE (p1)-[:KNOWS {weight: co_occurrence}]->(p2) "
            "RETURN count(*) AS cnt"
        )
        count += r.single()["cnt"]
    return count


def main():
    p = argparse.ArgumentParser(description="Neo4j 知识图谱补充导入")
    p.add_argument("--entities", required=True)
    p.add_argument("--facts", required=True)
    p.add_argument("--uri", default="bolt://127.0.0.1:7687")
    p.add_argument("--user", default="neo4j")
    p.add_argument("--password", required=True)

    args = p.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    driver.verify_connectivity()
    print("✅ 连接成功")

    # 1. Load entities
    with open(args.entities, "r", encoding="utf-8") as f:
        entities_data = json.load(f)
    entities = entities_data.get("entities", [])
    print(f"\n导入实体 ({len(entities)} 个)...")
    n = load_entities(driver, entities)
    print(f"  ✅ {n} 实体节点")

    # 2. Load facts
    with open(args.facts, "r", encoding="utf-8") as f:
        facts_data = json.load(f)
    facts = facts_data.get("facts", [])
    print(f"\n导入事实 ({len(facts)} 条)...")
    n = load_facts_to_neo4j(driver, facts)
    print(f"  ✅ {n} 事实节点 + HAS_FACT / DERIVED_FROM 关系")

    # 3. Link entities to messages
    print(f"\n链接实体 → 消息...")
    n = link_entities_to_messages(driver)
    print(f"  ✅ {n} 条 MENTIONS/ABOUT 关系")

    # 4. KNOWS relationships
    print(f"\n构建人物共现网络...")
    n = link_knows_relationships(driver)
    print(f"  ✅ {n} 条 KNOWS 关系")

    # 5. 最终统计
    print(f"\n{'='*50}")
    with driver.session() as s:
        stats = [
            ("Person", "MATCH (n:Person) RETURN count(n)"),
            ("Person_Entity", "MATCH (n:Person_Entity) RETURN count(n)"),
            ("Topic", "MATCH (n:Topic) RETURN count(n)"),
            ("Event", "MATCH (n:Event) RETURN count(n)"),
            ("Message", "MATCH (n:Message) RETURN count(n)"),
            ("Fact", "MATCH (n:Fact) RETURN count(n)"),
            ("SAID", "MATCH ()-[r:SAID]->() RETURN count(r)"),
            ("REPLY_TO", "MATCH ()-[r:REPLY_TO]->() RETURN count(r)"),
            ("MENTIONS", "MATCH ()-[r:MENTIONS]->() RETURN count(r)"),
            ("ABOUT", "MATCH ()-[r:ABOUT]->() RETURN count(r)"),
            ("HAS_FACT", "MATCH ()-[r:HAS_FACT]->() RETURN count(r)"),
            ("DERIVED_FROM", "MATCH ()-[r:DERIVED_FROM]->() RETURN count(r)"),
            ("KNOWS", "MATCH ()-[r:KNOWS]->() RETURN count(r)"),
        ]
        for label, cypher in stats:
            r = s.run(cypher)
            val = r.single().values()[0]
            print(f"  {label:15s}: {val}")

    driver.close()
    print(f"\n✅ 知识图谱导入完成")
    print(f"   Neo4j Browser: http://localhost:7474")
    print(f"   试试: MATCH (p:Person)-[:HAS_FACT]->(f:Fact)-[:DERIVED_FROM]->(m:Message) RETURN p,f,m LIMIT 25")


if __name__ == "__main__":
    main()
