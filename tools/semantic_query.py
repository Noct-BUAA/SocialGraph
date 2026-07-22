#!/usr/bin/env python3
"""
Neo4j 图语义查询 — Step 0 升级版

从 CONTAINS 字符串匹配 → 图关系展开
"体育老师" 通过 Topic 节点展开 → 找到 "体育课" + "体育" + "老师" 的全部相关消息

用法:
    python tools/semantic_query.py "体育老师" --limit 10
"""

from __future__ import annotations

import sys
import argparse
from typing import List, Dict
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# 核心词到话题的映射 — 闭包关系
# 每个话题下展开为所有关联词
TOPIC_EXPANSION = {
    "体育": ["体育", "体育课", "体育老师", "体育班", "运动", "跑步", "早八体育", "体测"],
    "学业": ["考试", "上课", "法语", "数学", "物理", "化学", "冯如杯", "论文", "保研", "答辩", "作业", "课程", "学分"],
    "视频组": ["视频", "拍摄", "剪辑", "剪映", "推送", "云盘", "微电影", "分镜", "特写", "片场", "镜头"],
    "金钱": ["钱", "工资", "劳务费", "发钱", "赚", "奖金", "稿费"],
    "生活": ["食堂", "外卖", "奶茶", "麦麦", "宿舍", "出去玩", "周末", "自行车", "睡觉", "饿了", "想吃", "好吃"],
    "感情": ["喜欢", "恋爱", "在一起", "分手", "男朋友", "暗恋", "暧昧", "表白", "前任"],
    "新媒体": ["新媒体", "公众号", "宣传", "招新", "面试", "袁老师", "老师", "蔡老师", "社长"],
    "杭航": ["杭航", "INSA", "中法", "北航", "法语训练营", "法国"],
    "人物": ["赵云朋", "zyp", "焦爱家", "谢渣渣", "谢欣眙", "房周仪", "李佳韵", "余可言", "蔡宜君",
             "薛雨凌", "辛冠宇", "姚亦涵", "张睿哲", "吴蔚", "袁晓慧", "谢雨倩", "辛子倩", "孟硕",
             "赵怡菲", "KOISHIGARINA", "焦老师", "欧阳老师"],
    "表情": ["哈哈", "笑死", "没招", "牛逼", "666", "牛魔", "草", "吗的", "无语", "绷不住"],
    "动漫": ["火影", "奶龙", "线条小狗", "阿拉斯加", "汤姆猫"],
    "摄影": ["拍照", "照片", "相机", "镜头", "光圈", "曝光", "自拍", "人像", "朋友圈"],
    "吐槽": ["草台班子", "抽象", "神经", "无语", "够了", "救命"],
}

# 建立反向索引: 词 → 话题
WORD_TO_TOPIC = {}
for topic, words in TOPIC_EXPANSION.items():
    for w in words:
        WORD_TO_TOPIC.setdefault(w, set()).add(topic)


def expand_keywords(user_input: str) -> List[str]:
    """从用户输入扩展关键词网络，按相关性分层。

    Layer 1 (核心): 只展开输入中直接匹配词所在的话题
    Layer 2 (外围): 同话题其他词 → 仅当 Layer 1 < 3 条结果时使用

    "体育老师" → Layer 1: ["体育", "体育课", "体育老师", "早八体育", "运动", "体测"]
    """
    layer1 = set()
    layer2 = set()

    # 找到输入中最强匹配的话题
    topic_hits = {}
    for word, topics in WORD_TO_TOPIC.items():
        if word in user_input:
            for topic in topics:
                topic_hits[topic] = topic_hits.get(topic, 0) + 1

    # 按匹配词数排序话题
    sorted_topics = sorted(topic_hits.items(), key=lambda x: -x[1])
    primary_topic = sorted_topics[0][0] if sorted_topics else None

    if primary_topic:
        layer1.update(TOPIC_EXPANSION.get(primary_topic, []))

    # 次要话题的词放 layer2
    for topic, _ in sorted_topics[1:]:
        layer2.update(TOPIC_EXPANSION.get(topic, []))

    # Layer 1 优先，去重
    layer2.difference_update(layer1)

    if not layer1:
        return [user_input], []

    return list(layer1), list(layer2)


def graph_semantic_search(user_input: str, limit: int = 15) -> List[Dict]:
    """图语义查询：分层关键词 + Topic 图 + 人物网络。

    Layer 1 关键词优先 → 不够再补 Layer 2
    """
    layer1, layer2 = expand_keywords(user_input)
    driver = GraphDatabase.driver(URI, auth=AUTH)

    with driver.session() as session:
        messages = {}

        # 核心查询: Layer 1 关键词 CONTAINS
        if layer1:
            kw_conditions = " OR ".join([f"m.content CONTAINS '{kw}'" for kw in layer1[:12]])
            result1 = session.run(
                f"MATCH (p:Person {{role: 'target'}})-[:SAID]->(m:Message) "
                f"WHERE ({kw_conditions}) "
                f"RETURN m.content AS content, m.msg_id AS msg_id, m.formatted_time AS time "
                f"ORDER BY m.msg_id DESC LIMIT $limit",
                limit=limit,
            )
            messages = {rec["msg_id"]: dict(rec) for rec in result1}

        # 补充查询: Layer 2 (仅当 Layer 1 结果太少)
        if len(messages) < 5 and layer2:
            kw2 = " OR ".join([f"m.content CONTAINS '{kw}'" for kw in layer2[:8]])
            result2 = session.run(
                f"MATCH (p:Person {{role: 'target'}})-[:SAID]->(m:Message) "
                f"WHERE ({kw2}) "
                f"RETURN m.content AS content, m.msg_id AS msg_id, m.formatted_time AS time "
                f"ORDER BY m.msg_id DESC LIMIT $limit",
                limit=limit,
            )
            for rec in result2:
                if rec["msg_id"] not in messages:
                    messages[rec["msg_id"]] = dict(rec)

        # Topic ABOUT 边查询
        topic_names = [t for t in TOPIC_EXPANSION.keys()
                       if any(kw in user_input for kw in TOPIC_EXPANSION[t])]
        if topic_names:
            result3 = session.run(
                "MATCH (m:Message)-[:ABOUT]->(t:Topic) WHERE t.name IN $topics "
                "MATCH (p:Person {role: 'target'})-[:SAID]->(m) "
                "RETURN m.content AS content, m.msg_id AS msg_id, m.formatted_time AS time "
                "ORDER BY m.msg_id DESC LIMIT $limit",
                topics=topic_names, limit=limit,
            )
            for rec in result3:
                if rec["msg_id"] not in messages:
                    messages[rec["msg_id"]] = dict(rec)

        # Person_Entity MENTIONS
        possible_names = [n for n in TOPIC_EXPANSION.get("人物", []) if n in user_input]
        if possible_names:
            result4 = session.run(
                "MATCH (m:Message)-[:MENTIONS]->(pe:Person_Entity) WHERE pe.name IN $names "
                "MATCH (p:Person {role: 'target'})-[:SAID]->(m) "
                "RETURN m.content AS content, m.msg_id AS msg_id, m.formatted_time AS time "
                "ORDER BY m.msg_id DESC LIMIT $limit",
                names=possible_names, limit=limit,
            )
            for rec in result4:
                if rec["msg_id"] not in messages:
                    messages[rec["msg_id"]] = dict(rec)

    driver.close()
    sorted_msgs = sorted(messages.values(), key=lambda x: x["msg_id"], reverse=True)
    return sorted_msgs[:limit]


def main():
    parser = argparse.ArgumentParser(description="Neo4j 图语义查询")
    parser.add_argument("query", help="用户输入文本")
    parser.add_argument("--limit", type=int, default=15, help="返回条数")
    parser.add_argument("--debug", action="store_true", help="打印扩展关键词")
    args = parser.parse_args()

    if args.debug:
        l1, l2 = expand_keywords(args.query)
        print(f"Layer 1 ({len(l1)}): {l1[:10]}")
        print(f"Layer 2 ({len(l2)}): {l2[:10]}")

    results = graph_semantic_search(args.query, args.limit)

    if not results:
        print("NO_RESULTS")
        return

    for msg in results:
        print(f"[msg#{msg['msg_id']}] {msg['time']} {msg['content'][:200]}")


if __name__ == "__main__":
    main()
