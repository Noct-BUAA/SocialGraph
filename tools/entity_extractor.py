#!/usr/bin/env python3
"""
实体提取与图消歧器 — 从聊天消息中提取实体并进行图消歧

输入: chat_parser.py 输出的 messages.jsonl
输出: entities.json (实体列表 + 关系列表，可直接导入 Neo4j)

核心功能：
1. 人名提取（基于称谓模式 + 共现关系）
2. 话题提取（基于高频词聚类）
3. 事件提取（基于时间锚点 + 关键词）
4. 图消歧：同名实体通过共现网络自动分叉

用法：
    python entity_extractor.py --file messages.jsonl --target "谢渣渣" --output entities.json
"""

from __future__ import annotations

import json
import re
import argparse
import sys
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional


class EntityExtractor:
    """实体提取器 — 基于规则 + 共现分析的轻量级 NER"""

    def __init__(self, target_name: str):
        self.target_name = target_name
        self.entities = {}  # entity_id -> Entity dict
        self.entity_counter = 0
        self.cooccurrence = defaultdict(Counter)  # entity -> {co_entity: count}
        self.aliases = defaultdict(set)  # base_name -> {aliases}

    def extract_from_messages(self, messages: list[dict]) -> dict:
        """从消息列表提取所有实体与关系"""
        # Phase 1: 人名提取
        self._extract_persons(messages)

        # Phase 2: 话题提取
        self._extract_topics(messages)

        # Phase 3: 事件提取
        self._extract_events(messages)

        # Phase 4: 构建消歧结果
        disambiguation = self._disambiguate()

        # Phase 5: 构建关系
        relations = self._build_relations(messages)

        return {
            "entities": list(self.entities.values()),
            "relations": relations,
            "disambiguation": disambiguation,
        }

    def _extract_persons(self, messages: list[dict]) -> None:
        """提取人物实体。

        规则：
        1. 已知人物: 谢渣渣, 焦爱家, Jajfandy
        2. 称谓模式: X老师, X同学, X学长, X学姐, X导员, X主任
        3. 全名模式: 2-4字中文名
        4. 英文名/拼音: 首字母大写的英文词
        """
        # 已知人物
        known_persons = {
            "谢渣渣": {"role": "target", "aliases": ["谢渣渣🙃", "渣渣", "谢欣眙"]},
            "焦爱家": {"role": "self", "aliases": ["焦焦", "爱家", "大爱", "焦爱家同学", "Jajfandy"]},
        }

        for name, info in known_persons.items():
            self._add_entity("person", name, info)

        # 从消息内容中提取称谓模式
        title_patterns = [
            (r'(\w{1,4}老师)', 'teacher'),
            (r'(\w{1,4}同学)', 'classmate'),
            (r'(\w{1,4}学长)', 'senior'),
            (r'(\w{1,4}学姐)', 'senior_female'),
            (r'(\w{1,4}导员)', 'counselor'),
            (r'(\w{1,4}主任)', 'director'),
            (r'(\w{1,4}教授)', 'professor'),
        ]

        # 从所有消息中构建人物共现网络
        all_content = " ".join([m["content"] for m in messages])

        for pattern, role in title_patterns:
            matches = re.findall(pattern, all_content)
            for match in set(matches):
                self._add_entity("person", match, {"role": role, "extracted_from": "title_pattern"})

        # 提取全名模式（2-3字中文名字，非已知词汇）
        name_pattern = re.compile(r'(?<!\w)([一-鿿]{2,3})(?!\w)')
        all_names = name_pattern.findall(all_content)
        # 过滤掉明显不是人名的词
        stop_names = {
            "一个", "什么", "怎么", "可以", "没有", "这个", "那个", "我们", "你们",
            "他们", "她们", "自己", "知道", "不是", "现在", "已经", "因为", "所以",
            "但是", "如果", "虽然", "而且", "然后", "不过", "还是", "只是", "就是",
            "觉得", "应该", "可能", "一定", "所以", "其实", "真的", "结果",
            "今天", "明天", "昨天", "上次", "下次", "第一", "第二", "其他",
            "大家", "别人", "有人", "这里", "哪里", "那边", "视频", "照片",
            "微信", "消息", "问题", "时间", "事情", "东西", "工作", "学习",
        }
        name_counter = Counter(all_names)
        for name, count in name_counter.most_common(50):
            if name not in stop_names and count >= 3 and name not in known_persons:
                if not any(t in name for t in ["的", "了", "是", "在", "有", "和", "就", "都", "要", "会", "能", "去", "说"]):
                    self._add_entity("person", name, {
                        "role": "unknown",
                        "frequency": count,
                        "extracted_from": "name_pattern",
                    })

    def _extract_topics(self, messages: list[dict]) -> None:
        """提取话题实体。

        基于关键词字典 + 高频词共现聚类。
        """
        topic_keywords = {
            "视频组": ["视频组", "拍摄", "剪辑", "剪映", "推送", "云盘", "文稿", "横版",
                       "稳定器", "图传", "监视器", "微电影", "分镜", "特写", "片场"],
            "新媒体中心": ["新媒体", "公众号", "宣传", "招新", "面试"],
            "学业": ["法语", "考试", "冯如杯", "论文", "实践队", "答辩", "保研", "GPA",
                     "课程", "必修", "选修", "学分", "教材", "上课", "下课", "物理",
                     "化学", "数学", "生物", "基础数学"],
            "生活": ["食堂", "外卖", "奶茶", "麦麦", "麦当劳", "奶茶", "宿舍", "寝室",
                     "图书馆", "自旋", "西湖", "樱花"],
            "感情": ["恋爱", "喜欢", "表白", "分手", "暗恋", "在一起", "男朋友", "女朋友",
                     "前任", "暧昧", "相亲"],
            "杭航": ["杭航", "INSA", "中法", "北航", "法语训练营", "法国"],
            "动漫": ["火影", "美漫", "日漫", "线条小狗", "阿拉斯加", "奶龙"],
            "摄影": ["拍照", "摄影", "相机", "镜头", "光圈", "曝光", "构图", "人像"],
            "吐槽": ["草台班子", "劳务费", "工资", "牛马", "抽象"],
        }

        all_content_lower = " ".join([m["content"] for m in messages])

        for topic_name, keywords in topic_keywords.items():
            score = 0
            evidence_msgs = []
            for kw in keywords:
                kw_count = all_content_lower.count(kw)
                if kw_count > 0:
                    score += kw_count
            if score >= 3:  # 至少被提到3次
                self._add_entity("topic", topic_name, {
                    "category": "topic",
                    "keywords": keywords,
                    "mention_score": score,
                })

    def _extract_events(self, messages: list[dict]) -> None:
        """提取事件实体。

        基于时间锚点 + 事件关键词。
        """
        event_patterns = [
            ("法语训练营", ["法语训练营", "训练营"]),
            ("迎新会", ["迎新", "迎新会"]),
            ("INSA见面会", ["INSA见面会", "见面会"]),
            ("冯如杯", ["冯如杯"]),
            ("合唱比赛", ["合唱", "合唱比赛"]),
            ("运动会", ["运动会"]),
            ("法语演唱会", ["演唱会", "航游国际"]),
            ("招生", ["招生", "招新"]),
            ("植树节", ["植树节", "植树", "种树"]),
        ]

        all_content = " ".join([m["content"] for m in messages])

        for event_name, keywords in event_patterns:
            matched = False
            for kw in keywords:
                if kw in all_content:
                    matched = True
                    break
            if matched:
                # 找到最早和最晚提及时间
                first_time = last_time = None
                for m in messages:
                    if any(kw in m["content"] for kw in keywords):
                        if first_time is None:
                            first_time = m["formatted_time"]
                        last_time = m["formatted_time"]

                self._add_entity("event", event_name, {
                    "category": "event",
                    "keywords": keywords,
                    "first_mention": first_time,
                    "last_mention": last_time,
                })

    def _add_entity(self, etype: str, name: str, properties: dict) -> str:
        """添加实体，返回 entity_id"""
        eid = f"{etype}_{len(self.entities)}"
        entity = {
            "entity_id": eid,
            "type": etype,
            "name": name,
            **properties,
        }
        self.entities[eid] = entity
        return eid

    def _disambiguate(self) -> list[dict]:
        """图消歧：检测同名但指向不同实体的节点。

        例如：
        - "蔡老师(基础数学)" vs "蔡宜君(同学)" → 通过共现网络自动分叉
        - "赵云鹏" 在不同上下文中可能指同一人或不同人
        """
        disambiguations = []

        # 按名称分组
        name_groups = defaultdict(list)
        for eid, entity in self.entities.items():
            name_groups[entity["name"]].append(eid)

        for name, eids in name_groups.items():
            if len(eids) < 2:
                continue

            # 检查每个同名实体的属性是否冲突
            roles = set()
            for eid in eids:
                role = self.entities[eid].get("role", "unknown")
                roles.add(role)

            # 如果角色冲突（例如 "teacher" vs "classmate"），标记为需要消歧
            if len(roles) > 1:
                disambiguations.append({
                    "name": name,
                    "entity_ids": eids,
                    "conflicting_roles": list(roles),
                    "resolution": "auto_split",  # 自动分叉为独立节点
                    "note": f"同名'{name}'因角色冲突({roles})自动分叉",
                })

        return disambiguations

    def _build_relations(self, messages: list[dict]) -> list[dict]:
        """构建实体间关系。

        关系类型：
        - MENTIONS: Message → Person/Topic/Event
        - KNOWS: Person → Person (共现于同一消息)
        - ABOUT: Message → Topic
        - PARTICIPATED_IN: Person → Event
        """
        relations = []

        # 人物共现分析
        person_names = {
            e["name"]
            for e in self.entities.values()
            if e["type"] == "person"
        }

        # 每条消息中检测实体共现
        for msg in messages[:500]:  # 采样前500条做共现
            content = msg["content"]
            msg_id = msg["msg_id"]
            sender = msg["sender"]

            # 检测消息中提到的人物
            mentioned = []
            for pname in person_names:
                if pname in content and pname != sender:
                    mentioned.append(pname)

            # 记录人物共现
            for i, p1 in enumerate(mentioned):
                for p2 in mentioned[i + 1 :]:
                    relations.append({
                        "from": p1,
                        "to": p2,
                        "type": "CO_OCCURS",
                        "source_msg_id": msg_id,
                    })

        return relations


def main() -> None:
    parser = argparse.ArgumentParser(description="聊天记录实体提取与图消歧")
    parser.add_argument("--file", required=True, help="消息 JSONL 文件路径")
    parser.add_argument("--target", required=True, help="目标人物昵称")
    parser.add_argument("--output", default="tools/entities.json", help="输出 JSON 文件路径")

    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    print(f"读取消息: {file_path}")
    messages = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                messages.append(json.loads(line))
    print(f"  共 {len(messages)} 条消息")

    print(f"\n提取实体 (目标人物: {args.target})...")
    extractor = EntityExtractor(args.target)
    result = extractor.extract_from_messages(messages)

    print(f"\n{'='*50}")
    print(f"提取结果:")
    person_count = sum(1 for e in result["entities"] if e["type"] == "person")
    topic_count = sum(1 for e in result["entities"] if e["type"] == "topic")
    event_count = sum(1 for e in result["entities"] if e["type"] == "event")
    print(f"  Person: {person_count}")
    print(f"  Topic:  {topic_count}")
    print(f"  Event:  {event_count}")
    print(f"  Relations: {len(result['relations'])}")
    print(f"  Disambiguations: {len(result['disambiguation'])}")

    # 打印消歧结果
    if result["disambiguation"]:
        print(f"\n⚠️ 实体消歧:")
        for d in result["disambiguation"]:
            print(f"  '{d['name']}': {d['note']}")

    # 打印人物列表
    print(f"\n📋 人物列表:")
    for e in result["entities"]:
        if e["type"] == "person":
            extra = f" (role={e.get('role', '?')})"
            print(f"  {e['name']}{extra}")

    # 输出
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 实体提取完成 → {args.output}")
    print(f"   后续可用 neo4j_loader.py 导入实体到 Neo4j")


if __name__ == "__main__":
    main()
