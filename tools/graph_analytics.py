#!/usr/bin/env python3
"""超大规模图分析 — PageRank / 社区发现 / 关系网络可视化

前置条件: Neo4j GDS 插件已安装 (neo4j-graph-data-science-*.jar 已在 plugins/)

用法:
  python tools/graph_analytics.py                    # 全量分析
  python tools/graph_analytics.py --pagerank          # 仅 PageRank
  python tools/graph_analytics.py --community         # 仅社区发现
  python tools/graph_analytics.py --export           # 导出 Gephi 格式
"""

from __future__ import annotations
import sys, os, json, argparse
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")


def check_gds(driver) -> bool:
    """检查 GDS 插件是否可用"""
    try:
        with driver.session() as s:
            s.run("RETURN gds.version() AS v").single()
        return True
    except:
        return False


def pagerank_analysis(driver):
    """PageRank — 找出社交网络中谁最重要

    用 INTERACTS_WITH 关系建图，计算每个人的 PageRank 分数
    """
    print("=== PageRank 社交中心度 ===\n")

    if check_gds(driver):
        # GDS 路径
        # 先清理旧投影
        try:
            with driver.session() as s:
                s.run("CALL gds.graph.drop('social_graph')")
        except:
            pass

        # 用 INTERACTS_WITH 关系（带加权 interaction_strength）
        with driver.session() as s:
            s.run("""
                CALL gds.graph.project(
                    'social_graph',
                    'Person',
                    'INTERACTS_WITH',
                    {relationshipProperties: 'interaction_strength'}
                )
            """)

            result = s.run("""
                CALL gds.pageRank.stream('social_graph', {relationshipWeightProperty: 'interaction_strength'})
                YIELD nodeId, score
                MATCH (p:Person) WHERE id(p) = nodeId
                RETURN p.name AS name, score
                ORDER BY score DESC
                LIMIT 20
            """).data()

            s.run("CALL gds.graph.drop('social_graph')")

    else:
        # Fallback: 手动计算（基于消息数量的简化 PageRank）
        print("  (GDS 不可用，使用简化的度中心性)\n")
        with driver.session() as s:
            result = s.run("""
                MATCH (p:Person)-[:SAID]->(m:Message)
                RETURN p.name AS name, p.role AS role, count(m) AS msg_count
                ORDER BY msg_count DESC
                LIMIT 20
            """).data()

            # 手动计算 PageRank 近似值
            total_msgs = sum(r["msg_count"] for r in result)
            for r in result:
                r["score"] = r["msg_count"] / total_msgs if total_msgs else 0

    print(f"{'排名':<5} {'名字':<25} {'分数':<10} {'角色':<10}")
    print("-" * 52)
    for i, r in enumerate(result[:20], 1):
        print(f"{i:<5} {r['name']:<25} {r['score']:<10.6f} {r.get('role', '?'):<10}")

    return result


def community_detection(driver):
    """Louvain 社区发现 — 基于 INTERACTS_WITH 找出自然形成的小圈子"""
    print("\n=== Louvain 社区发现 ===\n")

    if not check_gds(driver):
        print("  (需要 GDS 插件)")
        return

    # 清理旧投影
    try:
        with driver.session() as s:
            s.run("CALL gds.graph.drop('community_graph')")
    except:
        pass

    with driver.session() as s:
        # 基于 INTERACTS_WITH 建图
        s.run("""
            CALL gds.graph.project(
                'community_graph',
                'Person',
                'INTERACTS_WITH',
                {relationshipProperties: 'interaction_strength'}
            )
        """)

        # Louvain 算法
        result = s.run("""
            CALL gds.louvain.stream('community_graph', {relationshipWeightProperty: 'interaction_strength'})
            YIELD nodeId, communityId, intermediateCommunityIds
            MATCH (p:Person) WHERE id(p) = nodeId
            RETURN communityId AS community, collect(p.name)[0..10] AS members, count(p) AS size
            ORDER BY size DESC
        """).data()

        s.run("CALL gds.graph.drop('community_graph')")

    if result:
        for i, r in enumerate(result, 1):
            print(f"社区 {i} (大小={r['size']}):")
            for m in r['members'][:8]:
                print(f"  - {m}")
            if len(r['members']) > 8:
                print(f"  ... 还有 {len(r['members']) - 8} 人")
            print()
    else:
        print("  (Louvain 未返回结果)")

    return result


def temporal_analysis(driver):
    """时序分析 — 关系随时间的演化"""
    print("=== 时序关系演化 ===\n")

    with driver.session() as s:
        # 按月份统计每个人的互动
        result = s.run("""
            MATCH (p:Person)-[:SAID]->(m:Message)
            WHERE m.formatted_time IS NOT NULL
            WITH p.name AS name,
                 substring(m.formatted_time, 0, 7) AS month,
                 count(m) AS cnt
            WHERE cnt >= 5
            RETURN name, month, cnt
            ORDER BY name, month
        """).data()

    # 按人分组
    by_person = defaultdict(list)
    for r in result:
        by_person[r["name"]].append((r["month"], r["cnt"]))

    # 显示互动最多的 10 个人的时间线
    person_total = {name: sum(c for _, c in months) for name, months in by_person.items()}
    top_10 = sorted(person_total.items(), key=lambda x: -x[1])[:10]

    for name, total in top_10:
        months = by_person[name]
        timeline = " → ".join(f"{m}({c})" for m, c in sorted(months))
        first = months[0][0] if months else "?"
        last = months[-1][0] if months else "?"
        active_months = len(months)
        print(f"  {name:20s} {total:>5d}条 | {active_months:>2d}个月 | {first}~{last}")
        if len(months) <= 12:
            bars = "".join("█" if c > 20 else "▌" if c > 5 else "·" for _, c in sorted(months))
            print(f"    {bars}")

    return result


def export_gephi(driver, output_path: str = "tools/social_network.gexf"):
    """导出 Gephi GEXF 格式"""
    print(f"\n=== 导出 Gephi: {output_path} ===\n")

    nodes = []
    edges = []

    with driver.session() as s:
        # Nodes
        persons = s.run("""
            MATCH (p:Person)
            OPTIONAL MATCH (p)-[:SAID]->(m:Message)
            RETURN p.name AS name, p.role AS role, count(m) AS msg_count
        """).data()

        for i, p in enumerate(persons):
            nodes.append({
                "id": p["name"],
                "label": p["name"],
                "role": p["role"],
                "size": p["msg_count"],
            })

        # Edges via INTERACTS_WITH
        rels = s.run("""
            MATCH (a:Person)-[r:INTERACTS_WITH]-(b:Person)
            WHERE a.name < b.name
            RETURN a.name AS source, b.name AS target,
                   r.interaction_strength AS strength,
                   r.emotion_warmth AS warmth
        """).data()

        for r in rels:
            edges.append({
                "source": r["source"],
                "target": r["target"],
                "weight": r["strength"],
                "warmth": r["warmth"],
            })

    # 写 GEXF
    gexf = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<gexf xmlns="http://www.gexf.net/1.3" version="1.3">',
            '<graph mode="static" defaultedgetype="undirected">',
            '<attributes class="node"><attribute id="role" title="Role" type="string"/>',
            '<attribute id="size" title="Messages" type="integer"/></attributes>',
            '<attributes class="edge"><attribute id="strength" title="Strength" type="float"/>',
            '<attribute id="warmth" title="Warmth" type="float"/></attributes>',
            '<nodes>']

    for n in nodes:
        gexf.append(f'<node id="{n["id"]}" label="{n["label"]}">'
                    f'<attvalues><attvalue for="role" value="{n["role"]}"/>'
                    f'<attvalue for="size" value="{n["size"]}"/></attvalues></node>')

    gexf.append('</nodes><edges>')
    for i, e in enumerate(edges):
        gexf.append(f'<edge id="{i}" source="{e["source"]}" target="{e["target"]}" weight="{e["weight"]}">'
                    f'<attvalues><attvalue for="strength" value="{e["strength"]}"/>'
                    f'<attvalue for="warmth" value="{e["warmth"]}"/></attvalues></edge>')

    gexf.append('</edges></graph></gexf>')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(gexf))

    print(f"  {len(nodes)} 节点, {len(edges)} 边")
    print(f"  用 Gephi 打开: File → Open → {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pagerank", action="store_true")
    parser.add_argument("--community", action="store_true")
    parser.add_argument("--temporal", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--all", action="store_true", help="运行所有分析")
    args = parser.parse_args()

    run_all = args.all or not any([args.pagerank, args.community, args.temporal, args.export])

    driver = GraphDatabase.driver(URI, auth=AUTH)

    print("=" * 60)
    print("Neo4j 图分析引擎")
    print("=" * 60)

    # 当前图规模
    with driver.session() as s:
        nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        persons = s.run("MATCH (p:Person) RETURN count(p) AS c").single()["c"]
        print(f"\n图谱规模: {nodes:,} 节点, {rels:,} 关系, {persons} 人\n")

    if run_all or args.pagerank:
        pagerank_analysis(driver)

    if run_all or args.community:
        community_detection(driver)

    if run_all or args.temporal:
        temporal_analysis(driver)

    if run_all or args.export:
        export_gephi(driver)

    driver.close()
