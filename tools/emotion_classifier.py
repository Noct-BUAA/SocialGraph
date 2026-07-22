#!/usr/bin/env python3
"""DeepSeek 情绪分类器 — 替代正则 mood 检测

输入: 谢渣渣的回复文本
输出: {"emotion": "neutral_ack|cold_response|warm_reply|angry|sad|joking", "intensity": 0.0-1.0, "cold_signal": true/false}

用法:
  python tools/emotion_classifier.py "哦哦 知道了"
  python tools/emotion_classifier.py --batch
"""

import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate

SYSTEM_PROMPT = """你是一个中文对话情绪分类器。分析谢渣渣（一个大学女生）的回复，输出JSON。

情绪类型: neutral_ack（中性回应）, cold_response（冷淡/敷衍）, warm_reply（热情/友好）, angry（生气）, sad（难过）, joking（开玩笑）。
intensity: 0.0-1.0，情绪强烈程度。
cold_signal: 回复是否表达冷淡/敷衍/不想聊？（true/false）

规则：
- "哦"/"嗯"/"哦哦"/"行吧"/"随便" 单字/双字 → cold_response, cold_signal=true
- "哦哦 知道了"/"嗯嗯好的"/"okok" → neutral_ack, cold_signal=false
- 包含[捂脸][呲牙][大哭]"哈哈""笑死""没招了" → warm_reply 或 joking
- "滚""够了""别烦""无语" → angry 或 cold_response
- 回复极短（≤5字）+ 无emoji → 大概率 cold_signal=true
- 回复包含"爱家""焦焦" → warm_reply

只输出JSON，不要其他内容。"""


def classify(reply_text: str) -> dict:
    raw = generate(SYSTEM_PROMPT, reply_text, max_tokens=60, temperature=0.05)
    # 优先用增强规则（确定性，覆盖核心用例）
    result = rule_based_classify(reply_text)
    # API 作为补充（语义判断）
    try:
        api_result = json.loads(raw)
        if "emotion" not in api_result:
            api_result["emotion"] = api_result.get("emotion_type", api_result.get("type", ""))
        # 只在 API 给出不同判断时覆盖（规则优先保证不漏判）
        if api_result.get("cold_signal") and not result["cold_signal"]:
            result["cold_signal"] = True
            result["emotion"] = api_result.get("emotion", result["emotion"])
    except (json.JSONDecodeError, KeyError):
        pass
    return result


def rule_based_classify(text: str) -> dict:
    """增强规则分类器——覆盖正则的盲区"""
    t = text.strip()
    # 冷信号：极短 + 敷衍词 + 无emoji
    cold_words = ["哦","嗯","行吧","随便","算了","呵","懒得","不想说","滚","别烦","够了"]
    warm_indicators = ["[捂脸]","[呲牙]","[偷笑]","[大哭]","[心碎]","[白眼]","[强]",
                       "[破涕为笑]","[抱拳]","[玫瑰]","哈哈","笑死","没招","牛逼","草"]
    neutral_indicators = ["好的","知道了","ok","收到","了解","明白","好叭","okok"]

    has_emoji = any(w in t for w in warm_indicators)
    has_cold = any(w in t for w in cold_words)
    has_neutral = any(w in t for w in neutral_indicators)

    # 知道了/好的/ok → 不是冷暴力
    if has_neutral and not has_emoji:
        return {"emotion":"neutral_ack","intensity":0.2,"cold_signal":False}
    # 有热信号 → 不是冷
    if has_emoji:
        return {"emotion":"warm_reply","intensity":0.5,"cold_signal":False}
    # 极短 + 冷词 + 无热/中性信号
    if len(t) <= 5 and has_cold and not has_emoji:
        return {"emotion":"cold_response","intensity":0.8,"cold_signal":True}
    # 较短的冷词
    if len(t) <= 8 and has_cold:
        return {"emotion":"cold_response","intensity":0.5,"cold_signal":True}
    # 默认
    return {"emotion":"neutral","intensity":0.0,"cold_signal":False}


def mood_delta(classification: dict) -> float:
    """将分类结果转为 mood 变化量"""
    e = classification.get("emotion","")
    i = classification.get("intensity",0.5)
    if classification.get("cold_signal"):
        return -0.25 * i
    if e in ("warm_reply","joking"):
        return 0.15 * i
    if e == "angry":
        return -0.3 * i
    if e == "sad":
        return -0.1 * i
    return 0.0  # neutral_ack


if __name__ == "__main__":
    if "--batch" in sys.argv:
        tests = [
            "哦哦 知道了",
            "哦",
            "嗯嗯好的",
            "行吧",
            "没干嘛 刷手机 [捂脸]",
            "滚呐",
            "哈哈哈哈笑死",
            "好叭",
            "okok",
            "不想说了",
        ]
        print(f"{'回复':30s} {'情绪':15s} {'强度':6s} {'冷信号':6s} {'moodΔ':6s}")
        print("-"*70)
        for t in tests:
            r = classify(t)
            d = mood_delta(r)
            print(f"{t:30s} {r.get('emotion','?'):15s} {r.get('intensity',0):.2f}   {str(r.get('cold_signal','?')):6s} {d:+.2f}")
    else:
        text = sys.argv[1] if len(sys.argv) > 1 else "哦哦 知道了"
        r = classify(text)
        print(json.dumps(r, ensure_ascii=False))
