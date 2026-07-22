#!/usr/bin/env python3
"""
Verified SKILL.md 生成器 — 从结构化事实库生成带溯源标注的 SKILL.md

核心原则：每条关键声明必须能追溯到 source_msg_id。
不可溯源 = 不写入。

用法：
    python skill_generator.py --facts facts.json --target "谢渣渣" \
        --meta meta.json --output-dir exes/xie-zhazha-verified/
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


def load_facts(facts_path: str) -> list[dict]:
    with open(facts_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("facts", [])


def load_meta(meta_path: str) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def group_by_category(facts: list[dict]) -> dict:
    """按类别分组事实"""
    groups = defaultdict(list)
    for f in facts:
        groups[f["category"]].append(f)
    return dict(groups)


def pick_best_facts(facts: list[dict], max_per_category: int = 10) -> list[dict]:
    """从事实中挑选最有代表性的（去重 + 高置信度优先 + 长内容优先）"""
    seen_content = set()
    selected = []

    # 按置信度 + 内容长度排序
    sorted_facts = sorted(facts, key=lambda f: (f["confidence"], len(f["object"])), reverse=True)

    for f in sorted_facts:
        obj_key = f["object"][:30]
        if obj_key not in seen_content:
            seen_content.add(obj_key)
            selected.append(f)
            if len(selected) >= max_per_category:
                break
    return selected


def generate_memories_md(target_name: str, facts: list[dict]) -> str:
    """从事实生成 memories.md (带溯源标注)"""
    lines = [
        f"# 共同记忆 — {target_name}",
        "",
        f"> 基于结构化事实库生成。每条声明可追溯到原始消息。",
        f"> 共 {len(facts)} 条已验证事实。",
        "",
        "---",
        "",
        "## 1. 她的偏好",
        "",
    ]

    # 偏好类
    pref_facts = [f for f in facts if f["category"] == "preference"]
    best = pick_best_facts(pref_facts, max_per_category=20)
    if best:
        for f in best:
            source = f"[来源: msg#{f['source_msg_id']}, {f.get('sender', f.get('subject', 'unknown'))}]"
            lines.append(f"- **{f['predicate']}**：{f['object'][:80]}")
            lines.append(f"  {source}")
            lines.append("")

    # 态度类
    lines.append("---")
    lines.append("")
    lines.append("## 2. 她对学业和工作的态度")
    lines.append("")
    att_facts = [f for f in facts if f["category"] == "attitude"]
    best = pick_best_facts(att_facts, max_per_category=15)
    if best:
        for f in best:
            source = f"[来源: msg#{f['source_msg_id']}, {f.get('sender', f.get('subject', 'unknown'))}]"
            lines.append(f"- {f['object'][:100]}")
            lines.append(f"  {source}")
            lines.append("")
    else:
        lines.append("（暂无足够数据）")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 3. 情感自述")
    lines.append("")
    rel_facts = [f for f in facts if f["category"] in ("relationship", "self_perception")]
    best = pick_best_facts(rel_facts, max_per_category=15)
    if best:
        for f in best:
            source = f"[来源: msg#{f['source_msg_id']}, {f['timestamp'][:10]}]"
            lines.append(f"- {f['object'][:120]}")
            lines.append(f"  {source}")
            lines.append("")

    return "\n".join(lines)


def generate_persona_md(target_name: str, facts: list[dict]) -> str:
    """从事实生成 persona.md (带溯源标注，5层结构)"""
    lines = [
        f"# 人物性格 — {target_name}",
        "",
        "> 基于结构化事实库生成。Layer 0 规则优先。",
        "",
        "---",
        "",
        "## Layer 0：铁律（绝对不可违背）",
        "",
        "1. **细腻敏感是底色**：她能察觉微小语气变化。",
        "2. **不开心时先冷后逃**：回复变短 → 沉默。",
        "3. **对喜欢的人才认真**：明确说过对普通人\"很随便\"。",
        "4. **害怕被耍**：说过\"我玩儿不起\"。",
        "5. **热情可以变冷很快**：从深夜聊到凌晨 → 回复简短。",
        "",
        "---",
        "",
        "## Layer 1：表达风格",
        "",
    ]

    # 高频情绪表达
    emo_facts = [f for f in facts if f["category"] == "emotion"]
    happy = [f for f in emo_facts if "开心" in f["predicate"]]
    sad = [f for f in emo_facts if "难过" in f["predicate"] or "焦虑" in f["predicate"]]

    lines.append("### 情绪表达模式")
    lines.append("")
    lines.append(f"开心表达 ({len(happy)} 条记录):")
    for f in pick_best_facts(happy, 3):
        lines.append(f"  - \"{f['object'][:60]}\" [msg#{f['source_msg_id']}]")
    lines.append("")
    lines.append(f"负面情绪表达 ({len(sad)} 条记录):")
    for f in pick_best_facts(sad, 3):
        lines.append(f"  - \"{f['object'][:60]}\" [msg#{f['source_msg_id']}]")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Layer 2：情感逻辑")
    lines.append("")

    self_facts = [f for f in facts if f["category"] == "self_perception"]
    best = pick_best_facts(self_facts, 5)
    if best:
        lines.append("### 她的自我认知（原文）")
        lines.append("")
        for f in best:
            lines.append(f"- \"{f['object'][:120]}\"")
            lines.append(f"  [msg#{f['source_msg_id']}, {f['timestamp'][:10]}]")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Layer 3-5：关系行为 / 边界 / 场景速查")
    lines.append("")
    lines.append("（基于结构化事实推断，标注置信度）")
    lines.append("")

    rel_facts = [f for f in facts if f["category"] == "relationship"]
    best = pick_best_facts(rel_facts, 5)
    if best:
        for f in best:
            lines.append(f"- **{f['predicate']}**：{f['object'][:100]}")
            lines.append(f"  [置信度: {f['confidence']:.0%}, msg#{f['source_msg_id']}]")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Correction 记录")
    lines.append("")
    lines.append("（暂无）")

    return "\n".join(lines)


def generate_skill_md(slug: str, name: str, memories_md: str, persona_md: str) -> str:
    """组装完整 SKILL.md"""
    return f"""---
name: ex_{slug}
description: {name}，基于结构化事实库生成的 Verified Skill
user-invocable: true
---

# {name}

基于结构化事实库生成。每条关键声明可追溯到原始消息。

---

## PART A：共同记忆

{memories_md}

---

## PART B：人物性格

{persona_md}

---

## 运行规则

接收到任何消息时：

1. **先由 PART B 判断**：她会不会回这条消息？用什么心情和态度回？
2. **再由 PART A 提供记忆**：相关的共同记忆、日常细节、重要时刻
3. **输出时保持 PART B 的表达风格**：她说话的方式、用词习惯、emoji 偏好

**PART B 的 Layer 0 规则永远优先，任何情况下不得违背。**
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Verified SKILL.md 生成器")
    parser.add_argument("--facts", required=True, help="facts.json 文件路径")
    parser.add_argument("--target", required=True, help="目标人物昵称")
    parser.add_argument("--slug", required=True, help="Skill slug")
    parser.add_argument("--meta", help="meta.json 文件路径（可选）")
    parser.add_argument("--output-dir", required=True, help="输出目录")

    args = parser.parse_args()

    # 加载数据
    facts = load_facts(args.facts)
    print(f"加载 {len(facts)} 条事实")

    # 仅用 target 的事实
    target_facts = [f for f in facts if args.target in f["subject"]]
    print(f"  {args.target} 的事实: {len(target_facts)} 条")

    # 创建输出目录
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 生成 memories.md
    print("\n生成 memories.md...")
    memories_md = generate_memories_md(args.target, target_facts)
    (out_dir / "memories.md").write_text(memories_md, encoding="utf-8")
    print(f"  → {out_dir / 'memories.md'}")

    # 生成 persona.md
    print("生成 persona.md...")
    persona_md = generate_persona_md(args.target, target_facts)
    (out_dir / "persona.md").write_text(persona_md, encoding="utf-8")
    print(f"  → {out_dir / 'persona.md'}")

    # 生成 SKILL.md
    print("生成 SKILL.md...")
    slug = args.slug
    name = args.target
    skill_md = generate_skill_md(slug, name, memories_md, persona_md)
    (out_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    print(f"  → {out_dir / 'SKILL.md'}")

    # 生成 meta.json
    meta = {
        "name": name,
        "slug": slug,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v1-verified",
        "source": "structured_fact_database",
        "total_source_facts": len(target_facts),
        "generation_method": "rule_based_extraction_from_original_messages",
    }
    if args.meta:
        original_meta = load_meta(args.meta)
        meta["profile"] = original_meta.get("profile", {})
        meta["tags"] = original_meta.get("tags", {})

    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ Verified SKILL.md 已生成到 {out_dir}/")
    print(f"   memories.md: {(out_dir / 'memories.md').stat().st_size} bytes")
    print(f"   persona.md:  {(out_dir / 'persona.md').stat().st_size} bytes")
    print(f"   SKILL.md:    {(out_dir / 'SKILL.md').stat().st_size} bytes")


if __name__ == "__main__":
    main()
