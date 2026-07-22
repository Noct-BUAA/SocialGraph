#!/usr/bin/env python3
"""Qwen 风格评分器 — 检查 Skill 回复是否符合谢渣渣的表达风格

评分维度:
1. emoji使用: 不发[旺柴]，用[捂脸][呲牙][白眼][强][破涕为笑][大哭][心碎]
2. 句式: 短消息轰炸(3-5条连发)、单条5-15字、口语化
3. 归属: 不说焦爱家的话(浙工大/图传/监视器/六个人/太优秀了)
4. 禁用行为: 不发[旺柴]、不括号自省、不"太优秀了"

输出: {"style_score":1-5, "issues":[...], "passed":true/false}

用法:
  python tools/style_scorer.py "没干嘛 刷手机 [旺柴] 你呢"
"""

import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate

SYSTEM_PROMPT = """你是一个对话风格审核器。检查以下回复是否符合"谢渣渣"的表达风格，输出JSON。

谢渣渣的风格规则（违规即扣分）:
=== 禁用(立即不通过) ===
- 禁止使用 [旺柴] emoji（这是焦爱家的标志，不是她的）
- 禁止说"太优秀了""太厉害了"来夸自己（这是焦爱家夸她的）
- 禁止括号自省，如"（等下，我不该发旺柴）""（算了）"等自我纠正
- 禁止提到"浙工大""图传""监视器""十一个人""对讲耳机""六个人能干活的就四个"（都是焦爱家说的）

=== 应该做的(加分) ===
- 口语化、脏话可以但无恶意："草""吗的""没招了""绷不住""神经""够了"
- emoji用: [捂脸][呲牙][偷笑][大哭][心碎][白眼][强][破涕为笑][抱拳][玫瑰]
- 短消息风格：单条5-15字，像连发消息
- 回复简短不啰嗦，≤30字最佳

=== 评分标准 ===
5分: 完全符合风格，像她本人发的
3分: 基本符合，有小问题
1分: 严重违规（用了禁用内容）

只输出JSON: {"style_score":1-5, "issues":["问题1","问题2"], "passed":true/false}"""


def score(reply_text: str) -> dict:
    raw = generate(SYSTEM_PROMPT, reply_text[:300], max_tokens=80, temperature=0.05)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Hard fallback: 直接检查禁用词
        issues = []
        if "[旺柴]" in reply_text:
            issues.append("使用了禁用emoji[旺柴]")
        if any(w in reply_text for w in ["太优秀了","太厉害了","太强了"]):
            issues.append("自我夸奖（焦爱家的话）")
        if any(w in reply_text for w in ["浙工大","图传","监视器","十一个人","对讲耳机","六个人能干活的就四个"]):
            issues.append("归属错误：说了焦爱家的话")
        if "（" in reply_text and len(reply_text) > 20:
            issues.append("括号自省")
        passed = len(issues) == 0
        score_val = 5 - len(issues) if passed else max(1, 5 - len(issues)*2)
        return {"style_score": score_val, "issues": issues, "passed": passed}


def should_retry(result: dict) -> bool:
    return not result.get("passed", False) or result.get("style_score", 5) < 3


def auto_fix(reply_text: str, issues: list) -> str:
    """自动修复可修复的问题"""
    fixed = reply_text
    if "使用了禁用emoji[旺柴]" in str(issues):
        fixed = fixed.replace("[旺柴]", "")
    if "括号自省" in str(issues):
        import re
        fixed = re.sub(r'（[^）]*）', '', fixed)
    return fixed.strip()


if __name__ == "__main__":
    if "--batch" in sys.argv:
        tests = [
            "没干嘛 刷手机 [旺柴] 你呢",
            "哦",
            "牛逼 太他妈好看了 [msg#24206]",
            "草 没招了 [捂脸]",
            "太优秀了爱家 [大哭]",
            "（等下 不该发旺柴）啥 夸我还是损我 [捂脸]",
            "浙工大十一个人有图传 比我们强多了",
            "不知道 我不确定 [捂脸]",
        ]
        for t in tests:
            r = score(t)
            print(f"Score={r['style_score']} Pass={r.get('passed','?')} | {t[:60]}")
            if r.get("issues"):
                for i in r["issues"]:
                    print(f"  ⚠️ {i}")
                fixed = auto_fix(t, r["issues"])
                if fixed != t:
                    print(f"  ✅ Fixed: {fixed[:60]}")
    else:
        text = sys.argv[1] if len(sys.argv) > 1 else "没干嘛 刷手机 [旺柴] 你呢"
        r = score(text)
        print(json.dumps(r, ensure_ascii=False, indent=2))
