#!/usr/bin/env python3
"""
多人物 Persona 生成器 — 从 Neo4j 图关系提取人物画像

为聊天记录中提到的每个人生成精简 Persona。
核心：通过图关系（MENTIONS/SAID/KNOWS/ABOUT）而非文本归纳来构建人物画像。

用法：
    python tools/multi_persona.py --min-mentions 3 --output tools/personas/
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from typing import List, Dict
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")


def get_driver():
    return GraphDatabase.driver(URI, auth=AUTH)


def get_notable_persons(driver, min_mentions: int = 3) -> List[Dict]:
    """找出被提及次数 >= min_mentions 的人物实体"""
    with driver.session() as s:
        r = s.run(
            "MATCH (m:Message)-[:MENTIONS]->(pe:Person_Entity) "
            "WITH pe, count(m) AS mention_count "
            "WHERE mention_count >= $min "
            "RETURN pe.name AS name, pe.role AS role, mention_count "
            "ORDER BY mention_count DESC",
            min=min_mentions,
        )
        return [dict(rec) for rec in r]


def get_person_context(driver, name: str) -> Dict:
    """获取一个人的完整图上下文"""
    with driver.session() as s:
        # 被提及的消息（区分谁说）
        r1 = s.run(
            "MATCH (m:Message)-[:MENTIONS]->(pe:Person_Entity {name: $name}) "
            "MATCH (p:Person)-[:SAID]->(m) "
            "RETURN p.role AS speaker, m.content AS content, m.msg_id AS msg_id, m.formatted_time AS time "
            "ORDER BY m.msg_id DESC LIMIT 30",
            name=name,
        )
        mentions = [dict(rec) for rec in r1]

        # 共现人物
        r2 = s.run(
            "MATCH (m:Message)-[:MENTIONS]->(pe:Person_Entity {name: $name}) "
            "MATCH (m)-[:MENTIONS]->(other:Person_Entity) "
            "WHERE other.name <> $name "
            "WITH other, count(m) AS co_count "
            "WHERE co_count >= 2 "
            "RETURN other.name AS name, co_count "
            "ORDER BY co_count DESC LIMIT 10",
            name=name,
        )
        co_occurring = [dict(rec) for rec in r2]

        # 关联话题
        r3 = s.run(
            "MATCH (m:Message)-[:MENTIONS]->(pe:Person_Entity {name: $name}) "
            "MATCH (m)-[:ABOUT]->(t:Topic) "
            "WITH t, count(m) AS cnt "
            "RETURN t.name AS topic, cnt "
            "ORDER BY cnt DESC LIMIT 5",
            name=name,
        )
        topics = [dict(rec) for rec in r3]

    return {
        "name": name,
        "mention_count": len(mentions),
        "mentioned_by_target": len([m for m in mentions if m["speaker"] == "target"]),
        "mentioned_by_self": len([m for m in mentions if m["speaker"] == "self"]),
        "sample_mentions": [m["content"][:150] for m in mentions[:5]],
        "co_occurring": co_occurring,
        "topics": topics,
    }


def generate_persona_md(person: Dict, context: Dict) -> str:
    """为一个人生成精简 Persona Markdown"""
    name = person["name"]
    mentions = context["mention_count"]
    by_her = context["mentioned_by_target"]
    by_you = context["mentioned_by_self"]

    lines = [f"# {name}"]
    lines.append("")
    lines.append(f"聊天记录中被提及 {mentions} 次（她说 {by_her} 次，你说 {by_you} 次）")
    lines.append("")

    if context["topics"]:
        lines.append("## 关联话题")
        for t in context["topics"]:
            lines.append(f"- {t['topic']} ({t['cnt']} 条)")
        lines.append("")

    if context["co_occurring"]:
        lines.append("## 共现人物")
        for c in context["co_occurring"]:
            lines.append(f"- {c['name']}（共现 {c['co_count']} 次）")
        lines.append("")

    if context["sample_mentions"]:
        lines.append("## 提及原文（采样）")
        for i, m in enumerate(context["sample_mentions"]):
            lines.append(f"{i+1}. {m}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="多人物 Persona 生成器")
    parser.add_argument("--min-mentions", type=int, default=3, help="最低被提及次数")
    parser.add_argument("--output", default="tools/personas", help="输出目录")
    parser.add_argument("--list-only", action="store_true", help="仅列出人物，不生成详细 Persona")

    args = parser.parse_args()

    driver = get_driver()
    print("查询人物实体...")
    persons = get_notable_persons(driver, args.min_mentions)

    # 排除对话参与者
    persons = [p for p in persons if p["name"] not in ("谢渣渣", "焦爱家", "Jajfandy", "谢渣渣🙃")]
    print(f"找到 {len(persons)} 个值得关注的人物（提及 ≥ {args.min_mentions} 次）")

    if args.list_only:
        for p in persons:
            print(f"  {p['name']:10s} — {p['mention_count']:3d} 次 — role={p['role']}")
        driver.close()
        return

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_profiles = []
    for i, person in enumerate(persons):
        name = person["name"]
        print(f"[{i+1}/{len(persons)}] {name} ({person['mention_count']} 次)...")

        context = get_person_context(driver, name)
        all_profiles.append({"person": person, "context": context})

        # 写单个 Persona
        md = generate_persona_md(person, context)
        slug = name.replace(" ", "_").replace("/", "_")
        (out_dir / f"{slug}.md").write_text(md, encoding="utf-8")

    # 写汇总 JSON
    summary = {
        "total_persons": len(all_profiles),
        "persons": [
            {
                "name": p["person"]["name"],
                "mentions": p["person"]["mention_count"],
                "top_topics": [t["topic"] for t in p["context"]["topics"][:3]],
                "top_co_occurring": [c["name"] for c in p["context"]["co_occurring"][:3]],
            }
            for p in all_profiles
        ],
    }
    (out_dir / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    driver.close()
    print(f"\n✅ {len(all_profiles)} 个人物 Persona → {out_dir}/")
    print(f"   汇总: {out_dir}/_summary.json")


if __name__ == "__main__":
    main()
