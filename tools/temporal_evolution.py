#!/usr/bin/env python3
"""时序演化分析 — 关系随时间的生命周期

产出:
  1. 月度互动热力图 — 每个月哪些人最活跃
  2. 关系生命周期 — 升温/降温/稳定
  3. 关键时间节点 — 情绪转折点
  4. 群聊活跃度趋势

用法:
  python tools/temporal_evolution.py
  python tools/temporal_evolution.py --person "谢渣渣🙃"  # 聚焦某人
"""

from __future__ import annotations
import sys, os, json, argparse
from collections import defaultdict, Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")


def monthly_activity(driver, top_n: int = 15):
    """月度互动热力图 — 每个月最活跃的 N 个人"""
    print("=== 月度互动热力图 ===\n")

    with driver.session() as s:
        result = s.run("""
            MATCH (p:Person)-[:SAID]->(m:Message)
            WHERE m.formatted_time IS NOT NULL AND size(m.formatted_time) >= 7
            WITH p.name AS name,
                 substring(m.formatted_time, 0, 7) AS month,
                 count(m) AS cnt
            WHERE cnt >= 3
            RETURN name, month, cnt
            ORDER BY name, month
        """).data()

    # 按月份聚合
    month_data = defaultdict(lambda: defaultdict(int))
    all_months = set()
    for r in result:
        month_data[r["month"]][r["name"]] += r["cnt"]
        all_months.add(r["month"])

    sorted_months = sorted(all_months)

    # 每月 Top N
    print(f"{'月份':8s}", end="")
    for i in range(1, top_n + 1):
        print(f" {'Top'+str(i):20s}", end="")
    print()

    for month in sorted_months:
        top = sorted(month_data[month].items(), key=lambda x: -x[1])[:top_n]
        print(f"{month:8s}", end="")
        for name, cnt in top:
            print(f" {name[:18]+'…' if len(name)>19 else name:20s}", end="")
        print()

    return month_data


def relationship_lifecycle(driver, person: str = None):
    """关系生命周期 — 升温/降温分析"""
    target = person or "谢渣渣🙃"
    print(f"\n=== 关系生命周期: {target} ===\n")

    with driver.session() as s:
        # 按月统计消息量和情绪
        result = s.run("""
            MATCH (p:Person {name: $name})-[:SAID]->(m:Message)
            WHERE m.formatted_time IS NOT NULL AND size(m.formatted_time) >= 7
            WITH substring(m.formatted_time, 0, 7) AS month,
                 count(m) AS total,
                 sum(CASE WHEN m.emotion IN ['warm_reply','joking'] THEN 1 ELSE 0 END) AS warm,
                 sum(CASE WHEN m.emotion IN ['cold_response','angry'] THEN 1 ELSE 0 END) AS cold
            WHERE total >= 5
            RETURN month, total, warm, cold
            ORDER BY month
        """, name=target).data()

    if not result:
        print(f"  没有找到 {target} 的足够数据")
        return

    max_total = max(r["total"] for r in result)
    prev_total = None
    phases = []

    for r in result:
        month = r["month"]
        total = r["total"]
        warm = r["warm"]
        cold = r["cold"]
        warmth_ratio = warm / total if total else 0
        cold_ratio = cold / total if total else 0

        # 趋势判断
        if prev_total is not None:
            if total > prev_total * 1.3:
                trend = "↑升温"
            elif total < prev_total * 0.7:
                trend = "↓降温"
            else:
                trend = "→稳定"
        else:
            trend = " 起始"

        # 可视化
        bar_len = int(total / max_total * 30)
        bar = "█" * bar_len

        print(f"  {month} {bar} {total:>5d}条 {trend:4s} "
              f"温暖率={warmth_ratio:.0%} 冷率={cold_ratio:.0%}")

        if cold_ratio > 0.1 and prev_total and total < prev_total * 0.7:
            phases.append({"month": month, "event": "冷信号+降温", "total": total})

        prev_total = total

    # 关键转折点
    if phases:
        print(f"\n  ⚠️ 关键转折点:")
        for p in phases:
            print(f"    {p['month']}: {p['event']} ({p['total']}条)")


def group_activity_trend(driver):
    """群聊活跃度趋势"""
    print("\n=== 群聊活跃度趋势 ===\n")

    with driver.session() as s:
        result = s.run("""
            MATCH (p:Person)-[:SAID]->(m:Message)
            WHERE m.source_file IS NOT NULL AND m.source_file STARTS WITH '群聊'
              AND m.formatted_time IS NOT NULL AND size(m.formatted_time) >= 7
            WITH m.source_file AS grp,
                 substring(m.formatted_time, 0, 7) AS month,
                 count(m) AS cnt
            WHERE cnt >= 5
            RETURN grp, month, cnt
            ORDER BY grp, month
        """).data()

    if not result:
        print("  (需要 source_file 属性 — 请重新加载数据)")
        return

    # 按群分组
    groups = defaultdict(list)
    for r in result:
        short_name = r["grp"].replace("群聊_", "").replace(".json", "")[:25]
        groups[short_name].append((r["month"], r["cnt"]))

    for grp, months in sorted(groups.items(), key=lambda x: -sum(c for _, c in x[1])):
        total = sum(c for _, c in months)
        timeline = ""
        for m, c in sorted(months):
            if c > 100:
                timeline += "█"
            elif c > 20:
                timeline += "▌"
            else:
                timeline += "·"
        first = months[0][0]
        last = months[-1][0]
        print(f"  {grp:30s} {total:>5d}条 [{first}~{last}] {timeline}")


def export_timeline_data(driver, output_path: str = "tools/timeline_data.json"):
    """导出时序数据为 JSON，供可视化使用"""
    data = {"people": {}, "groups": {}, "months": set()}

    with driver.session() as s:
        # Top 20 people by month
        result = s.run("""
            MATCH (p:Person)-[:SAID]->(m:Message)
            WHERE m.formatted_time IS NOT NULL AND size(m.formatted_time) >= 7
            WITH p.name AS name, substring(m.formatted_time, 0, 7) AS month, count(m) AS cnt
            RETURN name, month, cnt ORDER BY cnt DESC LIMIT 500
        """).data()

        for r in result:
            if r["name"] not in data["people"]:
                data["people"][r["name"]] = {}
            data["people"][r["name"]][r["month"]] = r["cnt"]
            data["months"].add(r["month"])

    data["months"] = sorted(data["months"])
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n时序数据已导出: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--person", help="聚焦某个人的关系生命周期")
    parser.add_argument("--export", action="store_true", help="导出时序 JSON")
    args = parser.parse_args()

    driver = GraphDatabase.driver(URI, auth=AUTH)
    monthly_activity(driver)
    relationship_lifecycle(driver, args.person)
    group_activity_trend(driver)
    if args.export:
        export_timeline_data(driver)
    driver.close()
