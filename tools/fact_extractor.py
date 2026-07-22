#!/usr/bin/env python3
"""
事实三元组提取器 — 从聊天消息中提取带有 sender 锚定的事实

核心约束：subject 必须是 source message 的 sender。
这确保了事实归属不可篡改——(焦爱家)-[:SAID]->(msg#1902) 的事实
永远不会被误归到谢渣渣。

输出格式：
{
  "fact_id": "F_0001",
  "subject": "谢渣渣",          // 事实的主体（=source_msg的sender）
  "predicate": "喜欢吃",         // 关系
  "object": "麻辣香辣的东西",    // 宾语/值
  "source_msg_id": 567,         // 来源消息ID（可追溯）
  "source_content": "...",      // 原文片段
  "confidence": 0.9,            // 置信度
  "category": "preference",     // 事实类别
  "timestamp": "2025-09-14T..." // 时间戳
}

用法：
    python fact_extractor.py --file messages.jsonl --target "谢渣渣" --output facts.json
"""

from __future__ import annotations

import json
import re
import argparse
import sys
from pathlib import Path
from typing import Optional
from collections import defaultdict


# 事实提取规则：基于关键词匹配 + sender 锚定
# (category, predicate, keyword_patterns, negate_patterns)
EXTRACTION_RULES = [
    # --- 偏好类 ---
    ("preference", "喜欢吃", [
        "爱吃", "喜欢吃", "好吃爱吃", "想吃", "太喜欢吃了", "喜欢吃", "最爱吃",
        "喜欢.*吃", "好想吃", "日思夜想的", "就爱吃", "都爱吃",
    ], ["不爱吃", "不喜欢吃", "讨厌吃"]),
    ("preference", "不喜欢吃", [
        "不爱吃", "不喜欢吃", "讨厌吃", "吃不惯", "没有很喜欢吃",
    ], []),
    ("preference", "喜欢喝", [
        "想喝", "喜欢喝", "爱喝", "想喝.*了",
    ], []),
    ("preference", "喜欢(审美)", [
        "我喜欢.*画风", "我.*喜欢.*风格", "喜欢修勾", "喜欢阿拉斯加",
        "喜欢抹茶", "喜欢逛这种", "太喜欢这个了", "我很喜欢这张",
        "喜欢这种", "我喜欢看", "喜欢.*文学", "喜欢.*文笔",
    ], ["不喜欢"]),
    ("preference", "不喜欢(审美)", [
        "不喜欢", "我讨厌", "我不喜欢", "讨厌",
    ], []),

    # --- 情绪类 ---
    ("emotion", "开心", [
        "开心", "哈哈", "笑死", "好玩", "很幸福", "太幸福", "好高兴",
    ], ["不开心", "难过"]),
    ("emotion", "难过/焦虑", [
        "难过", "伤心", "害怕", "焦虑", "担心", "紧张", "不开心",
        "想哭", "累趴", "崩溃", "[大哭]", "[心碎]", "[难过]",
        "不想干了", "我死了算了",
    ], []),
    ("emotion", "压力大", [
        "累趴", "太累了", "不想干了", "累死了", "忙死了",
        "太肝了", "顶不住", "绷不住",
    ], []),

    # --- 人际关系类 ---
    ("relationship", "喜欢某人", [
        "我喜欢", "暗恋", "对.*有好感", "视奸你", "挺喜欢你",
        "认识你真好", "挺喜欢跟你聊天", "爱你",
    ], []),
    ("relationship", "对关系的态度", [
        "谈恋爱.*会", "爱情", "恋爱", "男朋友", "女朋友",
        "在一起", "分手", "前任", "暧昧", "喜欢.*类型",
        "没有相爱的勇气", "玩儿不起", "事业不能给爱情让步",
    ], []),

    # --- 工作/学业态度类 ---
    ("attitude", "对工作的评价", [
        "视频组", "劳务费", "草台班子", "未来可期", "工资",
        "拍摄", "剪辑", "推送", "云盘", "公众号",
    ], []),
    ("attitude", "对学业的评价", [
        "冯如杯", "考试", "上课", "法语", "GPA", "论文",
        "保研", "答辩", "必修", "选修",
    ], []),

    # --- 自我认知类 ---
    ("self_perception", "自我描述", [
        "我这个人", "我.*是.*的人", "我.*性格", "我.*比较",
        "我.*很", "我.*特别", "我双标", "我除非",
    ], []),
]


class FactExtractor:
    """事实三元组提取器 — Sender-锚定版"""

    def __init__(self, target_name: str, self_name: str = "Jajfandy"):
        self.target_name = target_name
        self.self_name = self_name
        self.facts = []
        self.fact_counter = 0

    def extract(self, messages: list[dict]) -> list[dict]:
        """从消息列表提取所有事实"""
        for msg in messages:
            content = msg.get("content", "")
            sender = msg.get("sender", "")
            sender_role = msg.get("sender_role", "")
            msg_id = msg.get("msg_id", "")
            timestamp = msg.get("timestamp", "")

            if not content:
                continue

            # 只从 target 和 self 的消息中提取事实
            if sender_role not in ("target", "self"):
                continue

            for rule in EXTRACTION_RULES:
                category, predicate, pos_patterns, neg_patterns = rule

                # 先检查否定模式
                is_negated = False
                for neg_pat in neg_patterns:
                    if re.search(neg_pat, content):
                        is_negated = True
                        break

                if is_negated and neg_patterns:
                    continue

                # 检查正向模式
                matched = False
                for pos_pat in pos_patterns:
                    if re.search(pos_pat, content):
                        matched = True
                        break

                if not matched:
                    continue

                # 提取具体的 object
                obj = self._extract_object(content, predicate, pos_patterns)

                self.fact_counter += 1
                self.facts.append({
                    "fact_id": f"F_{self.fact_counter:05d}",
                    "subject": sender,
                    "subject_role": sender_role,
                    "predicate": predicate,
                    "object": obj,
                    "source_msg_id": msg_id,
                    "source_content": content[:200],  # 截断长消息
                    "confidence": 0.85 if is_negated else 0.90,
                    "category": category,
                    "timestamp": timestamp,
                })

        return self.facts

    def _extract_object(self, content: str, predicate: str, patterns: list[str]) -> str:
        """从消息内容中提取宾语/值。

        简化方法：返回匹配关键词附近的文本片段。
        """
        for pat in patterns:
            m = re.search(pat, content)
            if m:
                # 提取匹配位置前后的上下文
                start = max(0, m.start() - 5)
                end = min(len(content), m.end() + 30)
                snippet = content[start:end].strip()
                # 清理多余的标点
                snippet = re.sub(r'^[，。！？、\s]+', '', snippet)
                snippet = re.sub(r'[，。！？、\s]+$', '', snippet)
                return snippet
        return content[:50]

    def get_stats_by_sender(self) -> dict:
        """按 sender 统计事实数量"""
        stats = defaultdict(lambda: {"total": 0, "by_category": defaultdict(int)})
        for f in self.facts:
            sender = f["subject"]
            stats[sender]["total"] += 1
            stats[sender]["by_category"][f["category"]] += 1
        return dict(stats)

    def get_attribution_check(self) -> list[dict]:
        """归属验证：检查是否所有 target 的事实 subject 确实是 target。

        这是关键验证——如果任何 fact 的 subject 不匹配，那就是归属错误。
        """
        errors = []
        for f in self.facts:
            if f["subject_role"] == "target":
                if self.target_name not in f["subject"]:
                    errors.append({
                        "fact_id": f["fact_id"],
                        "expected_subject": self.target_name,
                        "actual_subject": f["subject"],
                        "source_msg_id": f["source_msg_id"],
                        "error": "TARGET_SUBJECT_MISMATCH",
                    })
            elif f["subject_role"] == "self":
                if self.target_name in f["subject"]:
                    errors.append({
                        "fact_id": f["fact_id"],
                        "expected_subject": self.self_name,
                        "actual_subject": f["subject"],
                        "source_msg_id": f["source_msg_id"],
                        "error": "SELF_MISTAKEN_FOR_TARGET",
                    })

        return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="事实三元组提取器 (Sender-锚定)")
    parser.add_argument("--file", required=True, help="消息 JSONL 文件路径")
    parser.add_argument("--target", required=True, help="目标人物昵称")
    parser.add_argument("--self-name", default="Jajfandy", help="自己的昵称（默认: Jajfandy）")
    parser.add_argument("--output", default="tools/facts.json", help="输出 JSON 文件路径")
    parser.add_argument("--check-attribution", action="store_true",
                        help="运行归属验证检查")

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

    # 统计 sender
    target_msgs = [m for m in messages if m["sender_role"] == "target"]
    self_msgs = [m for m in messages if m["sender_role"] == "self"]
    print(f"  Target ({args.target}): {len(target_msgs)} 条")
    print(f"  Self ({args.self_name}): {len(self_msgs)} 条")

    extractor = FactExtractor(args.target, args.self_name)
    facts = extractor.extract(messages)

    print(f"\n{'='*50}")
    print(f"提取结果: {len(facts)} 条事实")

    # 按类别统计
    by_cat = defaultdict(int)
    for f in facts:
        by_cat[f["category"]] += 1
    print(f"\n按类别:")
    for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {cnt}")

    # 按 sender 统计
    sender_stats = extractor.get_stats_by_sender()
    print(f"\n按发送者:")
    for sender, stats in sender_stats.items():
        print(f"  {sender}: {stats['total']} 条事实")

    # 归属验证
    if args.check_attribution:
        errors = extractor.get_attribution_check()
        print(f"\n🔍 归属验证:")
        if errors:
            print(f"  ❌ 发现 {len(errors)} 个归属错误:")
            for err in errors[:10]:
                print(f"    {err['fact_id']}: {err['error']} (msg#{err['source_msg_id']})")
        else:
            print(f"  ✅ 所有事实归属正确 (0 错误)")

    # 展示样例
    print(f"\n📋 事实样例 (前 10 条 Target 事实):")
    target_facts = [f for f in facts if f["subject_role"] == "target"]
    for f in target_facts[:10]:
        print(f"  [{f['fact_id']}] {f['predicate']}: {f['object'][:60]}")
        print(f"           来源: msg#{f['source_msg_id']}, sender={f['subject']}")

    # 输出
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "target": args.target,
            "self_name": args.self_name,
            "total_facts": len(facts),
            "facts": facts,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 事实提取完成 → {args.output}")
    print(f"   后续可用 verifier.py 对 SKILL.md 内容进行溯源验证")


if __name__ == "__main__":
    main()
