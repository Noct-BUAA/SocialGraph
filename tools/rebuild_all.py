#!/usr/bin/env python3
"""一键重建 — 全流程自动化

清空 → 解析 → 加载 → 标注 → 去重 → 关系分析 → 导出

用法:
  python tools/rebuild_all.py              # 全流程
  python tools/rebuild_all.py --skip-enrich # 跳过API标注（离线可用）
"""

from __future__ import annotations
import sys, os, json, time, argparse, subprocess

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(PROJECT, "tools")
TEXTS = os.path.join(PROJECT, "聊天记录", "texts")


def run(cmd: str, desc: str) -> float:
    """运行命令并计时"""
    print(f"\n{'='*60}")
    print(f"▶ {desc}")
    print(f"{'='*60}")
    t0 = time.time()
    result = subprocess.run(cmd, shell=True, cwd=PROJECT)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"❌ 失败 ({elapsed:.0f}s)")
        raise SystemExit(1)
    print(f"✅ 完成 ({elapsed:.0f}s)")
    return elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-enrich", action="store_true", help="跳过 DeepSeek API 标注")
    parser.add_argument("--skip-txt", action="store_true", help="跳过 TXT 格式群聊")
    args = parser.parse_args()

    total_start = time.time()

    # Step 1: 解析所有聊天
    run(
        f'python "{TOOLS}/chat_parser.py" --all --dir "{TEXTS}" --outdir "{TOOLS}/parsed_chats/"',
        "Step 1/7: 解析聊天记录"
    )

    # Step 2: 过滤私聊 + 合并
    run(
        f'python -c "'
        f'import json; '
        f'with open(\"{TOOLS}/parsed_chats/chat_summary.json\") as f: summary = json.load(f); '
        f'private = {k for k,v in summary.items() if v.get(\"chat_type\") == \"private\"}; '
        f'msgs = [json.loads(l) for l in open(\"{TOOLS}/parsed_chats/all_messages.jsonl\")]; '
        f'private_msgs = [m for m in msgs if m.get(\"source_file\",\"\") in private]; '
        f'with open(\"{TOOLS}/parsed_chats/private_messages.jsonl\",\"w\") as f: '
        f'[f.write(json.dumps(m,ensure_ascii=False)+\"\\n\") for m in private_msgs]; '
        f'print(f\"私聊: {len(private_msgs)} 条\"); '
        f'groups = {k for k,v in summary.items() if v.get(\"chat_type\") != \"private\"}; '
        f'group_msgs = [m for m in msgs if m.get(\"source_file\",\"\") in groups]; '
        f'print(f\"群聊: {len(group_msgs)} 条\")"',
        "Step 2/7: 过滤消息"
    )

    # Step 3: 合并 + 分配全局ID
    run(
        f'python -c "'
        f'import json; '
        f'msgs = []; '
        f'for f in [\"private_messages.jsonl\", \"group_messages.jsonl\"]: '
        f'  with open(f\"{TOOLS}/parsed_chats/{{f}}\") as fh: '
        f'    msgs.extend(json.loads(l) for l in fh); '
        f'for i,m in enumerate(msgs): m[\"msg_id\"] = i+1; '
        f'with open(\"{TOOLS}/parsed_chats/all_merged.jsonl\",\"w\") as f: '
        f'[f.write(json.dumps(m,ensure_ascii=False)+\"\\n\") for m in msgs]; '
        f'print(f\"合并: {len(msgs)} 条\")"',
        "Step 3/7: 合并分配ID"
    )

    # Step 4: 加载 Neo4j
    run(
        f'python "{TOOLS}/neo4j_loader.py" --file "{TOOLS}/parsed_chats/all_merged.jsonl" --password neo4j123',
        "Step 4/7: 加载 Neo4j"
    )

    # Step 5: 情绪标注 (需要API)
    if not args.skip_enrich:
        run(
            f'python "{TOOLS}/batch_enrich_all.py" --all',
            "Step 5/7: 情绪标注 (DeepSeek API)"
        )
    else:
        print("\n⏭️  跳过情绪标注 (--skip-enrich)")

    # Step 6: 实体去重 + 关系分析
    run(
        f'python "{TOOLS}/identity_resolver.py" --no-llm --threshold 0.75',
        "Step 6/7: 实体去重"
    )
    run(
        f'python "{TOOLS}/relationship_analyzer.py"',
        "Step 6/7: 关系强度分析"
    )

    # Step 7: 图分析 + 时序 + 导出
    run(
        f'python "{TOOLS}/graph_analytics.py" --pagerank',
        "Step 7/7: PageRank"
    )
    run(
        f'python "{TOOLS}/temporal_evolution.py" --person "谢渣渣🙃" --export',
        "Step 7/7: 时序分析"
    )

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"🏆 全流程完成 ({total_elapsed/60:.1f}min)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
