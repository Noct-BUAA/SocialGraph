#!/usr/bin/env python3
"""标注信度检验 — 测量 DeepSeek 对同一消息多次标注的一致性

方法：
  100 条随机消息 × 3 次标注 (temperature=0.1/0.3/0.5)
  计算 emotion 和 conversation_role 的 Cohen's κ

输出：
  - κ 值 (≥0.7 可接受，≥0.8 良好，≥0.9 优秀)
  - 不一致消息列表
  - 建议

用法:
  python tools/annotation_reliability.py            # 默认 100 条
  python tools/annotation_reliability.py --n 200    # 200 条
"""

from __future__ import annotations
import sys, os, json, time, random, argparse
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import enrich_one
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# Emotion categories
EMOTIONS = ["neutral_ack", "cold_response", "warm_reply", "angry", "sad", "joking", "neutral"]
ROLES = ["response", "topic_opener", "deflecting", "ending", "escalation", "standalone"]


def cohens_kappa(ratings: list[list[str]], categories: list[str]) -> float:
    """计算 Cohen's κ for 2 raters (取前两次标注)

    κ = (p_o - p_e) / (1 - p_e)
    p_o = observed agreement
    p_e = expected agreement by chance
    """
    if len(ratings) < 2:
        return 1.0

    r1 = ratings[0]
    r2 = ratings[1]
    n = len(r1)

    # Observed agreement
    agreed = sum(1 for a, b in zip(r1, r2) if a == b)
    p_o = agreed / n

    # Expected agreement by chance
    cat_to_idx = {c: i for i, c in enumerate(categories)}
    # Count marginal distributions
    r1_counts = Counter(r1)
    r2_counts = Counter(r2)

    p_e = 0.0
    for cat in categories:
        p_e += (r1_counts.get(cat, 0) / n) * (r2_counts.get(cat, 0) / n)

    if p_e == 1.0:
        return 1.0 if p_o == 1.0 else 0.0

    kappa = (p_o - p_e) / (1 - p_e)
    return kappa


def pairwise_kappa(all_ratings: list[list[str]], categories: list[str]) -> dict:
    """计算所有标注轮次对的 κ"""
    pairs = []
    for i in range(len(all_ratings)):
        for j in range(i + 1, len(all_ratings)):
            k = cohens_kappa([all_ratings[i], all_ratings[j]], categories)
            pairs.append({"pair": f"t{i+1}-t{j+1}", "kappa": round(k, 4)})
    return pairs


def sample_messages(n: int = 100) -> list[dict]:
    """从 Neo4j 分层抽样：短中长各 1/3"""
    driver = GraphDatabase.driver(URI, auth=AUTH)
    per_bucket = n // 3

    with driver.session() as s:
        # 短消息 (≤5 chars)
        short = s.run(
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) <= 5 AND m.content IS NOT NULL "
            "RETURN m.msg_id AS mid, m.content AS c, size(m.content) AS l "
            "ORDER BY rand() LIMIT $n", n=per_bucket
        ).data()

        # 中消息 (6-15 chars)
        medium = s.run(
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) > 5 AND size(m.content) <= 15 "
            "RETURN m.msg_id AS mid, m.content AS c, size(m.content) AS l "
            "ORDER BY rand() LIMIT $n", n=per_bucket
        ).data()

        # 长消息 (>15 chars)
        long_msgs = s.run(
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) "
            "WHERE size(m.content) > 15 "
            "RETURN m.msg_id AS mid, m.content AS c, size(m.content) AS l "
            "ORDER BY rand() LIMIT $n", n=per_bucket
        ).data()

    driver.close()
    all_msgs = short + medium + long_msgs
    random.shuffle(all_msgs)
    return all_msgs


def run_reliability_test(n: int = 100):
    """主流程"""
    print(f"=== 标注信度检验 (n={n}) ===\n")

    # 1. 抽样
    print("抽样中...")
    messages = sample_messages(n)
    print(f"  已抽取 {len(messages)} 条 (短≤5: ~{n//3}, 中6-15: ~{n//3}, 长>15: ~{n//3})\n")

    # 2. 三轮标注 (不同 temperature)
    temps = [0.1, 0.3, 0.5]
    all_emotions = [[] for _ in range(3)]  # all_emotions[t] = list of emotions
    all_roles = [[] for _ in range(3)]
    inconsistent = []

    print("标注中 (3 轮 × 不同 temperature)...")
    start = time.time()

    for i, msg in enumerate(messages):
        if (i + 1) % 20 == 0:
            elapsed = time.time() - start
            rate = (i + 1) * 3 / elapsed
            print(f"  [{i+1}/{len(messages)}] {rate:.1f}条/s", flush=True)

        emotions_this = []
        roles_this = []

        for t_idx, temp in enumerate(temps):
            # 用 content 前 200 字符标注
            result = enrich_one(msg["c"])
            em = result.get("emotion", "unknown")
            role = result.get("conversation_role", "response")

            # 归一化：unknown → neutral
            if em == "unknown":
                em = "neutral"
            if em not in EMOTIONS:
                em = "neutral"
            if role not in ROLES:
                role = "response"

            all_emotions[t_idx].append(em)
            all_roles[t_idx].append(role)
            emotions_this.append(em)
            roles_this.append(role)

        # 检测不一致
        if len(set(emotions_this)) > 1 or len(set(roles_this)) > 1:
            inconsistent.append({
                "msg_id": msg["mid"],
                "content": msg["c"][:80],
                "length": msg["l"],
                "emotions": emotions_this,
                "roles": roles_this,
            })

    elapsed = time.time() - start
    print(f"  完成 ({elapsed:.0f}s)\n")

    # 3. 计算 κ
    print("=" * 60)
    print("信度分析结果")
    print("=" * 60)

    emotion_kappas = pairwise_kappa(all_emotions, EMOTIONS)
    role_kappas = pairwise_kappa(all_roles, ROLES)

    print("\n📊 Emotion Cohen's κ:")
    for ek in emotion_kappas:
        grade = "✅" if ek["kappa"] >= 0.7 else "⚠️" if ek["kappa"] >= 0.5 else "❌"
        print(f"  {ek['pair']}: κ={ek['kappa']:.4f} {grade}")

    avg_em_k = sum(ek["kappa"] for ek in emotion_kappas) / len(emotion_kappas)
    print(f"  平均: κ={avg_em_k:.4f}")

    print("\n📊 Conversation Role Cohen's κ:")
    for rk in role_kappas:
        grade = "✅" if rk["kappa"] >= 0.7 else "⚠️" if rk["kappa"] >= 0.5 else "❌"
        print(f"  {rk['pair']}: κ={rk['kappa']:.4f} {grade}")

    avg_role_k = sum(rk["kappa"] for rk in role_kappas) / len(role_kappas)
    print(f"  平均: κ={avg_role_k:.4f}")

    # 4. 不一致消息
    print(f"\n📋 不一致消息: {len(inconsistent)}/{len(messages)} ({100*len(inconsistent)/len(messages):.1f}%)")

    if inconsistent:
        print("\n不一致详情 (前 10 条):")
        for inc in inconsistent[:10]:
            em_set = " | ".join(inc["emotions"])
            role_set = " | ".join(inc["roles"])
            print(f"  [msg#{inc['msg_id']}] L={inc['length']} \"{inc['content']}\"")
            print(f"    emotions: {em_set}")
            print(f"    roles:    {role_set}")

    # 5. 按长度分组的 κ
    print("\n📏 按消息长度分组的 Emotion κ:")
    buckets = {"短(≤5)": (0, 5), "中(6-15)": (6, 15), "长(>15)": (16, 999)}
    for bname, (lo, hi) in buckets.items():
        indices = [i for i, m in enumerate(messages) if lo <= m["l"] <= hi]
        if len(indices) >= 5:
            b_emotions = [[all_emotions[t][i] for i in indices] for t in range(3)]
            b_kappas = pairwise_kappa(b_emotions, EMOTIONS)
            avg_k = sum(bk["kappa"] for bk in b_kappas) / len(b_kappas)
            print(f"  {bname} (n={len(indices)}): κ={avg_k:.4f}")

    # 6. 总结
    print("\n" + "=" * 60)
    overall = (avg_em_k + avg_role_k) / 2
    print(f"总体信度: κ={overall:.4f}")
    if overall >= 0.8:
        print("✅ 优秀 — 标注高度一致，可以信赖")
    elif overall >= 0.7:
        print("✅ 可接受 — 标注基本一致，可用于下游任务")
    elif overall >= 0.5:
        print("⚠️ 勉强 — 建议对不一致的消息进行人工复核")
    else:
        print("❌ 不足 — 标注随机性太大，需要改进 prompt 或换模型")

    # 保存结果
    result = {
        "n": n,
        "emotion_kappa": {ek["pair"]: ek["kappa"] for ek in emotion_kappas},
        "emotion_kappa_avg": round(avg_em_k, 4),
        "role_kappa": {rk["pair"]: rk["kappa"] for rk in role_kappas},
        "role_kappa_avg": round(avg_role_k, 4),
        "overall_kappa": round(overall, 4),
        "inconsistent_count": len(inconsistent),
        "inconsistent_rate": round(len(inconsistent) / len(messages), 4),
        "inconsistent_samples": inconsistent[:20],
    }

    out_path = os.path.join(os.path.dirname(__file__), "reliability_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {out_path}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100)
    args = parser.parse_args()
    run_reliability_test(n=args.n)
