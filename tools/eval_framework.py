#!/usr/bin/env python3
"""量化 Evaluation 框架 v2 — 五个维度打分，替代"感觉像不像"

维度：
  1. 风格合规 (style_compliance) — forbidden_rate: 禁用词/[旺柴]/归属错误
  2. 长度自然度 (length_naturalness) — 回复长度 vs 真实分布 (6.5±31.2) 的 KL 散度
  3. 情绪匹配 (emotion_fit) — AI 回复情绪 vs 同一上下文下真实回复的情绪
  4. 上下文一致性 (context_coherence) — 连续对话的 mood/intimacy 轨迹平滑度
  5. 上下文相关性 (context_relevance) — AI 回复是否合理回应了对方说的话

输出：0-100 综合分数 + 各维度明细

用法:
  python tools/eval_framework.py --sample 20          # 20 组对话评估
  python tools/eval_framework.py --history             # 查看历史趋势
"""

from __future__ import annotations
import sys, os, json, time, math, argparse
from collections import Counter
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from style_scorer import score as style_score
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# 真实分布（来自 behavior_stats.json）
REAL_LENGTH_MEAN = 6.5
REAL_LENGTH_STD = 31.2
REAL_LENGTH_MEDIAN = 5
REAL_EMOJI_RATE = 0.056
REAL_COLD_RATE = 0.066


def kl_divergence_normal(mu1: float, sigma1: float, mu2: float, sigma2: float) -> float:
    """两个正态分布的 KL 散度 D_KL(P||Q)"""
    if sigma1 <= 0 or sigma2 <= 0:
        return float('inf')
    return math.log(sigma2 / sigma1) + (sigma1**2 + (mu1 - mu2)**2) / (2 * sigma2**2) - 0.5


def sample_conversation_segments(driver, n: int = 20) -> list[dict]:
    """从 Neo4j 抽取对话片段：焦爱家说了 X → 谢渣渣真实回了 Y"""
    segments = []
    with driver.session() as s:
        # 找有 REPLY_TO 链的对话对
        results = s.run(
            "MATCH (her:Message)-[:REPLY_TO]->(his:Message) "
            "WHERE her.sender_role = 'target' AND his.sender_role = 'self' "
            "  AND size(her.content) > 3 AND size(his.content) > 3 "
            "RETURN his.content AS his_msg, her.content AS her_real_reply, "
            "  her.msg_id AS her_mid, his.msg_id AS his_mid, "
            "  her.emotion AS her_emotion, her.conversation_role AS her_role "
            "ORDER BY rand() LIMIT $n",
            n=n
        ).data()
        segments.extend(results)
    return segments


def evaluate_style_compliance(replies: list[str]) -> dict:
    """维度 1: 风格合规 — forbidden 率"""
    total = len(replies)
    violations = 0
    issues_list = []

    for reply in replies:
        r = style_score(reply)
        if not r.get("passed", True):
            violations += 1
            issues_list.extend(r.get("issues", []))

    forbidden_rate = violations / total if total > 0 else 0
    score_val = max(0, 100 * (1 - forbidden_rate * 2))  # 每次违规扣 50 分

    return {
        "score": round(score_val, 1),
        "forbidden_rate": round(forbidden_rate, 3),
        "violations": violations,
        "total": total,
        "sample_issues": issues_list[:5],
    }


def evaluate_length_naturalness(replies: list[str]) -> dict:
    """维度 2: 长度自然度 — KL 散度"""
    lengths = [len(r) for r in replies]
    if not lengths:
        return {"score": 0, "kl_divergence": float('inf')}

    sample_mean = sum(lengths) / len(lengths)
    # 使用 biased std (匹配真实数据的计算方式)
    sample_std = (sum((l - sample_mean) ** 2 for l in lengths) / len(lengths)) ** 0.5
    if sample_std == 0:
        sample_std = 0.5  # 避免除零

    kl = kl_divergence_normal(sample_mean, sample_std, REAL_LENGTH_MEAN, REAL_LENGTH_STD)
    # KL 散度 → 分数：KL=0 完美(100)，KL>2 很差(0)
    score_val = max(0, 100 * math.exp(-kl))

    # Emoji 率检查
    emoji_count = sum(1 for r in replies if "[" in r and "]" in r)
    emoji_rate = emoji_count / len(replies)
    emoji_penalty = abs(emoji_rate - REAL_EMOJI_RATE) * 100

    return {
        "score": round(score_val, 1),
        "kl_divergence": round(kl, 4),
        "sample_mean": round(sample_mean, 1),
        "sample_std": round(sample_std, 1),
        "real_mean": REAL_LENGTH_MEAN,
        "real_std": REAL_LENGTH_STD,
        "emoji_rate": round(emoji_rate, 3),
        "emoji_penalty": round(emoji_penalty, 1),
    }


def evaluate_emotion_fit(ai_replies: list[str], real_replies: list[str]) -> dict:
    """维度 3: 情绪匹配 — AI 回复情绪 vs 真实回复情绪"""
    # 用 emotion_classifier 标注 AI 回复的情绪
    from emotion_classifier import classify

    matches = 0
    pairs = []
    for ai, real in zip(ai_replies, real_replies):
        ai_emotion = classify(ai)
        real_emotion = classify(real)
        # 比较 cold_signal（最敏感的指标）
        ai_cold = ai_emotion.get("cold_signal", False)
        real_cold = real_emotion.get("cold_signal", False)
        pairs.append({
            "ai_reply": ai[:40],
            "real_reply": real[:40],
            "ai_cold": ai_cold,
            "real_cold": real_cold,
            "match": ai_cold == real_cold,
        })
        if ai_cold == real_cold:
            matches += 1

    accuracy = matches / len(pairs) if pairs else 0
    score_val = round(accuracy * 100, 1)

    return {
        "score": score_val,
        "cold_signal_accuracy": round(accuracy, 3),
        "matches": matches,
        "total": len(pairs),
        "sample_pairs": pairs[:5],
    }


def evaluate_context_relevance(his_contexts: list[str], ai_replies: list[str],
                                real_replies: list[str]) -> dict:
    """维度 5: 上下文相关性 — AI 回复是否合理回应了对方的话

    用 DeepSeek 标注每条 AI 回复的 context_relevance，
    与真实回复的 context_relevance 对比。
    relevance 类别: direct_answer / deflecting / topic_shift / emotional_response / acknowledgment
    """
    from deepseek_client import enrich_one

    CONTEXT_RELEVANCE_PROMPT = (
        '分析回复是否合理回应了上文。输出JSON:'
        '{"relevance":"direct_answer|deflecting|topic_shift|emotional_response|acknowledgment"}'
        'direct_answer=直接回答, deflecting=回避/敷衍, topic_shift=转移话题, '
        'emotional_response=情绪回应(不答问题但表达感受), acknowledgment=收到/确认。只输出JSON。'
    )

    def get_relevance(context: str, reply: str) -> str:
        """用 DeepSeek 标注单条回复的 context_relevance"""
        user_text = f'上文: "{context[:100]}"\n回复: "{reply[:100]}"'
        raw = generate(CONTEXT_RELEVANCE_PROMPT, user_text, max_tokens=30, temperature=0.1)
        try:
            return json.loads(raw).get("relevance", "unknown")
        except:
            return "unknown"

    matches = 0
    relevant_matches = 0  # 只算 substantive 类别的匹配
    pairs = []
    substantive = {"direct_answer", "emotional_response", "acknowledgment", "topic_shift"}
    avoidant = {"deflecting"}

    for ctx, ai, real in zip(his_contexts, ai_replies, real_replies):
        ai_rel = get_relevance(ctx, ai)
        real_rel = get_relevance(ctx, real)
        is_match = ai_rel == real_rel
        if is_match:
            matches += 1
        # 实质性回应 vs 回避——更重要的维度
        ai_is_substantive = ai_rel in substantive
        real_is_substantive = real_rel in substantive
        if ai_is_substantive == real_is_substantive:
            relevant_matches += 1
        pairs.append({
            "context": ctx[:40],
            "ai_reply": ai[:40],
            "real_reply": real[:40],
            "ai_relevance": ai_rel,
            "real_relevance": real_rel,
            "match": is_match,
        })

    n = len(pairs)
    exact_accuracy = matches / n if n else 0
    substantive_accuracy = relevant_matches / n if n else 0
    # 综合分：exact match 占 60%，substantive 占 40%
    score_val = round((exact_accuracy * 0.6 + substantive_accuracy * 0.4) * 100, 1)

    return {
        "score": score_val,
        "exact_match_accuracy": round(exact_accuracy, 3),
        "substantive_accuracy": round(substantive_accuracy, 3),
        "matches": matches,
        "total": n,
        "sample_pairs": pairs[:5],
    }


def evaluate_context_coherence(turns: list[dict]) -> dict:
    """维度 4: 上下文一致性 — mood/intimacy 轨迹平滑度

    turns: [{"mood": 0.1, "intimacy": 0.5}, ...]
    检查相邻 turn 之间的变化是否平滑
    """
    if len(turns) < 2:
        return {"score": 100, "smoothness": 1.0, "jumps": 0}

    mood_jumps = 0
    intimacy_jumps = 0
    max_mood_jump = 0

    for i in range(1, len(turns)):
        mood_delta = abs(turns[i].get("mood", 0) - turns[i-1].get("mood", 0))
        intimacy_delta = abs(turns[i].get("intimacy", 0.5) - turns[i-1].get("intimacy", 0.5))

        if mood_delta > 0.3:  # 情绪突变阈值
            mood_jumps += 1
        if intimacy_delta > 0.1:
            intimacy_jumps += 1
        if mood_delta > max_mood_jump:
            max_mood_jump = mood_delta

    total_transitions = len(turns) - 1
    smoothness = 1 - (mood_jumps + intimacy_jumps) / (2 * total_transitions)
    score_val = max(0, round(smoothness * 100, 1))

    return {
        "score": score_val,
        "smoothness": round(smoothness, 3),
        "mood_jumps": mood_jumps,
        "intimacy_jumps": intimacy_jumps,
        "max_mood_jump": round(max_mood_jump, 2),
        "transitions": total_transitions,
    }


def run_evaluation(n: int = 20, save_history: bool = True):
    """主评估流程"""
    driver = GraphDatabase.driver(URI, auth=AUTH)

    print(f"=== 量化 Evaluation (n={n}) ===\n")

    # 1. 抽取对话片段
    print("抽取对话片段...")
    segments = sample_conversation_segments(driver, n)
    print(f"  已抽取 {len(segments)} 组对话\n")

    if len(segments) < 5:
        print("⚠️ 对话片段不足，请检查 Neo4j 数据")
        driver.close()
        return

    # 2. 模拟 AI 回复（用 DeepSeek 生成替代回复）
    print("生成 AI 替代回复...")
    ai_replies = []
    real_replies = []
    his_contexts = []

    SIMULATE_PROMPT = (
        "你是谢渣渣，一个北航中法未来科技学院的大学女生。"
        "对方（焦爱家）给你发了一条消息。请用谢渣渣的口吻回复。\n"
        "要求：极短（5-8字），口语化，不加[旺柴]。\n"
    )

    for i, seg in enumerate(segments):
        his_msg = seg["his_msg"]
        her_real = seg["her_real_reply"]

        user_text = f"焦爱家: \"{his_msg[:100]}\"\n谢渣渣回复:"
        ai_reply = generate(SIMULATE_PROMPT, user_text, max_tokens=30, temperature=0.7)

        # 清理
        ai_reply = ai_reply.strip().strip('"').strip()
        if ai_reply.startswith("谢渣渣:"):
            ai_reply = ai_reply[4:].strip()
        if ai_reply.startswith("回复:"):
            ai_reply = ai_reply[3:].strip()

        ai_replies.append(ai_reply)
        real_replies.append(her_real)
        his_contexts.append(his_msg)

        if (i + 1) % 5 == 0:
            print(f"  [{i+1}/{len(segments)}]", flush=True)

    print()

    # 3. 四个维度评估
    print("=" * 60)
    print("评估结果")
    print("=" * 60)

    # 维度 1: 风格合规
    r1 = evaluate_style_compliance(ai_replies)
    print(f"\n📋 风格合规: {r1['score']:.0f}/100")
    print(f"   违规率: {r1['forbidden_rate']:.1%} ({r1['violations']}/{r1['total']})")
    if r1["sample_issues"]:
        print(f"   问题示例: {r1['sample_issues'][:2]}")

    # 维度 2: 长度自然度
    r2 = evaluate_length_naturalness(ai_replies)
    print(f"\n📏 长度自然度: {r2['score']:.0f}/100")
    print(f"   AI均值={r2['sample_mean']:.1f}±{r2['sample_std']:.1f} vs 真实={r2['real_mean']:.1f}±{r2['real_std']:.1f}")
    print(f"   KL散度={r2['kl_divergence']:.4f} Emoji率={r2['emoji_rate']:.3f}")

    # 维度 3: 情绪匹配
    r3 = evaluate_emotion_fit(ai_replies, real_replies)
    print(f"\n🎭 情绪匹配: {r3['score']:.0f}/100")
    print(f"   cold_signal 一致率: {r3['cold_signal_accuracy']:.1%} ({r3['matches']}/{r3['total']})")

    # 维度 4: 上下文一致性 — 模拟 mood 轨迹
    fake_turns = []
    base_mood = 0.0
    for i, ai_reply in enumerate(ai_replies):
        from emotion_classifier import classify
        em = classify(ai_reply)
        from emotion_classifier import mood_delta
        base_mood += mood_delta(em)
        base_mood = max(-1, min(1, base_mood))  # clamp
        fake_turns.append({"mood": base_mood, "intimacy": 0.5 + i * 0.002})

    r4 = evaluate_context_coherence(fake_turns)
    print(f"\n🔗 上下文一致性: {r4['score']:.0f}/100")
    print(f"   情绪跳变: {r4['mood_jumps']}/{r4['transitions']} 最大跳变={r4['max_mood_jump']:.2f}")

    # 维度 5: 上下文相关性
    r5 = evaluate_context_relevance(his_contexts, ai_replies, real_replies)
    print(f"\n💬 上下文相关性: {r5['score']:.0f}/100")
    print(f"   relevance 一致率: {r5['exact_match_accuracy']:.1%} ({r5['matches']}/{r5['total']})")
    print(f"   substantive 一致率: {r5['substantive_accuracy']:.1%}")
    if r5["sample_pairs"]:
        sp = r5["sample_pairs"][0]
        print(f"   示例: AI({sp['ai_relevance']}) vs 真实({sp['real_relevance']}) \"{sp['context']}\"")

    # 4. 综合分数 (5 维各 0.2)
    weights = {"style": 0.2, "length": 0.2, "emotion": 0.2, "coherence": 0.2, "relevance": 0.2}
    composite = (
        r1["score"] * weights["style"] +
        r2["score"] * weights["length"] +
        r3["score"] * weights["emotion"] +
        r4["score"] * weights["coherence"] +
        r5["score"] * weights["relevance"]
    )

    print(f"\n{'='*60}")
    print(f"🏆 综合分数: {composite:.0f}/100")
    print(f"   风格: {r1['score']:.0f} + 长度: {r2['score']:.0f} + "
          f"情绪: {r3['score']:.0f} + 上下文: {r4['score']:.0f} + "
          f"相关性: {r5['score']:.0f}")

    # 5. 保存历史记录
    result = {
        "timestamp": datetime.now().isoformat(),
        "n": n,
        "composite_score": round(composite, 1),
        "dimensions": {
            "style_compliance": r1,
            "length_naturalness": r2,
            "emotion_fit": r3,
            "context_coherence": r4,
            "context_relevance": r5,
        },
        "weights": weights,
        "sample_replies": [
            {"context": his_contexts[i][:60], "ai": ai_replies[i], "real": real_replies[i][:60]}
            for i in range(min(5, len(ai_replies)))
        ],
    }

    if save_history:
        history_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
        history = []
        if os.path.exists(history_path):
            try:
                with open(history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except:
                pass
        history.append({
            "timestamp": result["timestamp"],
            "score": result["composite_score"],
            "n": n,
        })
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        print(f"\n📊 历史记录已保存: {history_path}")
        if len(history) > 1:
            trend = [h["score"] for h in history[-10:]]
            print(f"   最近 {len(trend)} 次趋势: {' → '.join(str(s) for s in trend)}")

    driver.close()
    return result


def show_history():
    """查看历史评估趋势"""
    history_path = os.path.join(os.path.dirname(__file__), "eval_results.json")
    if not os.path.exists(history_path):
        print("暂无历史记录")
        return

    with open(history_path, "r", encoding="utf-8") as f:
        history = json.load(f)

    print(f"=== 评估历史 ({len(history)} 次) ===\n")
    print(f"{'时间':20s} {'分数':6s} {'样本':6s} {'变化':8s}")
    print("-" * 45)
    prev = None
    for h in history:
        ts = h["timestamp"][:19]
        score = h["score"]
        n = h.get("n", "?")
        delta = ""
        if prev is not None:
            d = score - prev
            delta = f"{d:+.1f}" if d != 0 else "="
        print(f"{ts:20s} {score:<6.1f} {str(n):6s} {delta:8s}")
        prev = score


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()

    if args.history:
        show_history()
    else:
        run_evaluation(n=args.sample)
