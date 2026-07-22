#!/usr/bin/env python3
"""语义质量 E2E 测试：10轮真实对话 → 路由+mood+评分器全追踪"""
import urllib.request, urllib.parse, json, time, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tools.emotion_classifier import classify, mood_delta
from tools.style_scorer import score as style_score, should_retry, auto_fix

BASE = "http://127.0.0.1:5002"

# 模拟 10 轮对话（谢渣渣的典型回复，含边界用例）
CONVERSATION = [
    # (用户消息, 模拟的谢渣渣回复)
    ("在干嘛", "没干嘛 刷手机 [捂脸]"),
    ("赵云朋呢", "傻逼zyp 又找我要视频 [msg#23387]"),
    ("哦", "哦"),
    ("周末去哪", "天天想着出去玩 没想好去哪 [捂脸]"),
    ("嗯", "嗯"),
    ("你体育课怎么样", "牛魔体育课 惨绝人寰 [msg#10436][msg#21077]"),
    ("知道了", "哦哦 知道了"),
    ("哈哈哈哈", "笑死 你又搞什么抽象 [捂脸]"),
    ("浙工大视频组十一人", "浙工大十一个人有图传 比我们强多了"),  # 归属错误！
    ("太神了", "牛逼 [旺柴] 太他妈好看了"),  # 禁用emoji！
]


def run():
    # 重置 session
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/session/reset", method="POST"))

    results = []
    for i, (user_msg, her_reply) in enumerate(CONVERSATION, 1):
        # Step 0: Smart query
        url = f"{BASE}/api/graph/smart?query={urllib.parse.quote(user_msg)}"
        smart = json.loads(urllib.request.urlopen(url, timeout=10).read())
        routes = smart.get("routes_used", [])

        # Step 1: Session
        session = json.loads(urllib.request.urlopen(f"{BASE}/api/session/status").read())
        mood_before = session.get("mood", 0)
        intimacy_before = session.get("intimacy", 0.5)

        # Step 2: 情绪分类 + 风格评分
        emotion = classify(her_reply)
        qwen_cold = emotion.get("cold_signal", False)
        qwen_emotion = emotion.get("emotion", "?")
        style = style_score(her_reply)
        style_pass = style.get("passed", False)
        style_issues = style.get("issues", [])
        fixed = auto_fix(her_reply, style_issues) if style_issues else her_reply

        # 更新 session
        delta = mood_delta(emotion)
        req = urllib.request.Request(
            f"{BASE}/api/session/update",
            data=json.dumps({"reply": fixed, "user_message": user_msg}).encode(),
            headers={"Content-Type": "application/json"},
        )
        updated = json.loads(urllib.request.urlopen(req).read())
        mood_after = updated.get("mood", 0)

        results.append({
            "turn": i,
            "user": user_msg,
            "reply_raw": her_reply[:60],
            "reply_fixed": fixed[:60] if fixed != her_reply else "",
            "routes": routes,
            "mood_before": mood_before,
            "mood_after": mood_after,
            "mood_delta": delta,
            "emotion_qwen": qwen_emotion,
            "cold_signal": qwen_cold,
            "style_passed": style_pass,
            "style_issues": style_issues,
        })
        time.sleep(0.1)

    return results


def print_report(results):
    print(f"{'#':3s} {'用户':14s} {'回复':22s} {'修正':18s} {'路由':22s} {'moodΔ':6s} {'情绪':10s} {'冷':4s} {'style':5s}")
    print("-" * 130)
    for r in results:
        routes_str = "+".join(r["routes"][:2]) if r["routes"] else "context"
        fixed_str = r["reply_fixed"][:16] if r["reply_fixed"] else "-"
        issues_str = "❌" if r["style_issues"] else "✅"
        print(f"{r['turn']:3d} {r['user'][:12]:14s} {r['reply_raw'][:20]:22s} {fixed_str:18s} {routes_str:22s} {r['mood_delta']:+.2f}  {r['emotion_qwen'][:8]:10s} {str(r['cold_signal'])[:4]:4s} {issues_str:5s}")

    # 统计
    style_fails = [r for r in results if r["style_issues"]]
    cold_turns = [r for r in results if r["cold_signal"]]
    mood_range = (min(r["mood_after"] for r in results), max(r["mood_after"] for r in results))

    print(f"\n{'='*60}")
    print(f"评分器拦截: {len(style_fails)} 次")
    for r in style_fails:
        print(f"  T{r['turn']}: {r['style_issues']}")
    print(f"冷信号触发: {len(cold_turns)} 次")
    print(f"Mood 终值范围: {mood_range[0]:.2f} ~ {mood_range[1]:.2f}")
    print(f"预期: 冷回复递减, 暖回复递增")

    # 关键验证
    checks = []
    # T3 "哦" → cold_signal=true
    t3 = results[2]
    checks.append(("T3 '哦'→cold", t3["cold_signal"] == True))
    # T5 "嗯" → cold
    t5 = results[4]
    checks.append(("T5 '嗯'→cold", t5["cold_signal"] == True))
    # T7 "哦哦 知道了" → NOT cold (关键)
    t7 = results[6]
    checks.append(("T7 '知道了'→NOT cold", t7["cold_signal"] == False))
    # T9 归属错误→被拦截
    t9 = results[8]
    checks.append(("T9 归属错误→拦截", len(t9["style_issues"]) > 0))
    # T10 [旺柴]→被拦截
    t10 = results[9]
    checks.append(("T10 [旺柴]→拦截", len(t10["style_issues"]) > 0))

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n关键验证: {passed}/{len(checks)}")
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")


if __name__ == "__main__":
    results = run()
    print_report(results)
