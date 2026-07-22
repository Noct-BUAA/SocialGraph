#!/usr/bin/env python3
"""
溯源验证器 — 验证 SKILL.md 中的每一条关键声明是否可追溯到原始消息

输入: SKILL.md 文本 + facts.json (fact_extractor.py 输出)
输出: verification_report.json (每条声明的验证状态)

验证状态：
- ✅ VERIFIED: 声明可追溯到具体 msg_id，sender 匹配
- ⚠️ INFERRED: 有相关事实但无法精确匹配
- ❌ UNVERIFIED: 无法在任何消息中找到支持证据

用法：
    python verifier.py --skill SKILL.md --facts facts.json --target "谢渣渣" --output verification_report.json
"""

from __future__ import annotations

import json
import re
import argparse
import sys
from pathlib import Path
from collections import defaultdict


class AttributionVerifier:
    """溯源性验证器"""

    def __init__(self, facts: list[dict], target_name: str):
        self.facts = facts
        self.target_name = target_name
        # 建立关键词 → facts 的索引
        self._keyword_index = self._build_keyword_index()
        # 按 sender 分组
        self._target_facts = [f for f in facts if self.target_name in f["subject"]]
        self._self_facts = [f for f in facts if self.target_name not in f["subject"]]

    def _build_keyword_index(self) -> defaultdict:
        """构建关键词倒排索引"""
        index = defaultdict(list)
        for f in self.facts:
            content = f.get("source_content", "")
            obj = f.get("object", "")
            # 从内容和 object 中提取关键词 (2-4字)
            text = content + " " + obj
            words = re.findall(r'[一-鿿]{2,4}', text)
            for w in set(words):
                index[w].append(f)
        return index

    def verify_claim(self, claim: str, expected_subject: str = None) -> dict:
        """验证单条声明。

        返回: {status, matched_facts, ...}
        """
        claim_words = re.findall(r'[一-鿿]{2,6}', claim)

        # 在索引中查找匹配的事实
        candidate_facts = []
        for word in claim_words:
            if word in self._keyword_index:
                candidate_facts.extend(self._keyword_index[word])

        if not candidate_facts:
            return {
                "claim": claim[:100],
                "status": "UNVERIFIED",
                "matched_facts": [],
                "reason": "无法在任何消息中找到相关关键词",
            }

        # 去重 + 按频率排序
        from collections import Counter
        fact_scores = Counter()
        for f in candidate_facts:
            fact_scores[f["fact_id"]] += 1

        # 取最佳匹配 (Top 3)
        top_fact_ids = [fid for fid, _ in fact_scores.most_common(3)]
        top_facts = [f for f in self.facts if f["fact_id"] in top_fact_ids]

        # 验证 sender 匹配
        if expected_subject:
            matching_facts = [f for f in top_facts
                              if expected_subject in f["subject"]]
            if matching_facts:
                return {
                    "claim": claim[:100],
                    "status": "VERIFIED",
                    "matched_facts": [
                        {
                            "fact_id": f["fact_id"],
                            "source_msg_id": f["source_msg_id"],
                            "sender": f["subject"],
                            "content_snippet": f["source_content"][:80],
                        }
                        for f in matching_facts[:3]
                    ],
                    "source_count": len(matching_facts),
                }
            else:
                return {
                    "claim": claim[:100],
                    "status": "MISATTRIBUTED",
                    "matched_facts": [
                        {
                            "fact_id": f["fact_id"],
                            "source_msg_id": f["source_msg_id"],
                            "sender": f["subject"],
                            "content_snippet": f["source_content"][:80],
                        }
                        for f in top_facts[:3]
                    ],
                    "reason": f"相关事实的 sender 是 '{top_facts[0]['subject']}'，不是期望的 '{expected_subject}'",
                }

        # 无 expected_subject 时的匹配
        return {
            "claim": claim[:100],
            "status": "PARTIAL",
            "matched_facts": [
                {
                    "fact_id": f["fact_id"],
                    "source_msg_id": f["source_msg_id"],
                    "sender": f["subject"],
                    "content_snippet": f["source_content"][:80],
                }
                for f in top_facts[:3]
            ],
        }

    def verify_document(self, skill_md_path: str) -> dict:
        """验证整个 SKILL.md 文档。

        解析文档中的关键声明，逐条验证。
        """
        with open(skill_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 提取关键声明 (引号内的内容 + 列表项)
        claims = []

        # 提取引用内容 "xxx"
        quoted = re.findall(r'"([^"]{8,})"', content)
        for q in quoted:
            # 判断这条引用应该归属于 target 还是 self
            expected = self.target_name  # 默认期望归属于 target
            claims.append({
                "claim_text": q,
                "expected_subject": expected,
                "type": "quoted",
            })

        # 提取列表项中的偏好声明 (前缀为 "- **xxx**：")
        list_items = re.findall(r'-\s+\*\*(.+?)\*\*[：:]\s*(.+?)(?:\n|$)', content)
        for title, detail in list_items:
            if len(detail) > 5:
                claims.append({
                    "claim_text": f"{title}: {detail[:80]}",
                    "expected_subject": self.target_name,
                    "type": "list_item",
                })

        # 逐条验证
        results = []
        stats = {"VERIFIED": 0, "MISATTRIBUTED": 0, "UNVERIFIED": 0, "PARTIAL": 0}

        for claim in claims:
            result = self.verify_claim(claim["claim_text"], claim["expected_subject"])
            result["type"] = claim["type"]
            results.append(result)
            stats[result["status"]] += 1

        return {
            "target": self.target_name,
            "total_claims": len(results),
            "stats": stats,
            "results": results,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="SKILL.md 溯源验证器")
    parser.add_argument("--skill", required=True, help="SKILL.md 文件路径")
    parser.add_argument("--facts", required=True, help="facts.json 文件路径 (fact_extractor.py 输出)")
    parser.add_argument("--target", required=True, help="目标人物昵称")
    parser.add_argument("--output", default="verification_report.json", help="输出报告路径")

    args = parser.parse_args()

    # 加载事实库
    facts_path = Path(args.facts)
    if not facts_path.exists():
        print(f"❌ 事实库不存在: {facts_path}")
        sys.exit(1)

    with open(facts_path, "r", encoding="utf-8") as f:
        facts_data = json.load(f)
    facts = facts_data.get("facts", [])
    print(f"加载事实库: {len(facts)} 条事实")

    # 创建验证器
    verifier = AttributionVerifier(facts, args.target)

    # 验证文档
    skill_path = Path(args.skill)
    if not skill_path.exists():
        print(f"❌ SKILL.md 不存在: {skill_path}")
        sys.exit(1)

    print(f"验证文档: {skill_path}")
    report = verifier.verify_document(str(skill_path))

    # 打印报告
    print(f"\n{'='*60}")
    print(f"📋 溯源验证报告")
    print(f"  目标人物: {args.target}")
    print(f"  总声明数: {report['total_claims']}")
    print(f"  ✅ VERIFIED:      {report['stats']['VERIFIED']}")
    print(f"  ❌ MISATTRIBUTED:  {report['stats']['MISATTRIBUTED']}")
    print(f"  ⚠️  PARTIAL:        {report['stats']['PARTIAL']}")
    print(f"  ❌ UNVERIFIED:    {report['stats']['UNVERIFIED']}")

    # 重点列出归属错误
    misattributed = [r for r in report["results"] if r["status"] == "MISATTRIBUTED"]
    if misattributed:
        print(f"\n🔴 归属错误 (MISATTRIBUTED):")
        for r in misattributed[:10]:
            print(f"  声明: {r['claim'][:60]}...")
            print(f"  原因: {r.get('reason', 'N/A')}")
            for mf in r.get("matched_facts", [])[:2]:
                print(f"        实际 sender: {mf['sender']} (msg#{mf['source_msg_id']})")

    # 列出无法验证的
    unverified = [r for r in report["results"] if r["status"] == "UNVERIFIED"]
    if unverified:
        print(f"\n🟡 无法验证 (UNVERIFIED): {len(unverified)} 条")
        for r in unverified[:5]:
            print(f"  - {r['claim'][:80]}...")

    # 输出
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 验证报告 → {args.output}")

    # 如果验证分数低，提示需要修正
    total = report["total_claims"]
    good = report["stats"]["VERIFIED"] + report["stats"]["PARTIAL"]
    if total > 0 and good / total < 0.5:
        print(f"\n⚠️  验证通过率低 ({good}/{total})，建议重新生成 SKILL.md")


if __name__ == "__main__":
    main()
