#!/usr/bin/env python3
"""实体去重引擎 — 合并跨聊天的同名/同人引用

三阶段匹配：
  1. 规则匹配: 拼音/缩写/角色+姓
  2. 图结构匹配: 两个实体和相同的人共现 → 可能是同一人
  3. LLM确认: DeepSeek 判断高置信度候选对

产出: Identity 节点 + SAME_AS 关系，合并 Person_Entity

用法:
  python tools/identity_resolver.py              # 全量去重
  python tools/identity_resolver.py --dry-run    # 只看候选，不写入
  python tools/identity_resolver.py --threshold 0.7  # 调整置信度阈值
"""

from __future__ import annotations
import sys, os, json, re, argparse
from collections import defaultdict, Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import generate
from neo4j import GraphDatabase

URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")

# ===== 规则引擎 =====

# 常见姓
SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
               "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
               "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮下齐康伍余元卜顾孟平黄"
               "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁")

# 拼音映射（简化版，覆盖常见姓氏和名字）
PINYIN_SIMILAR = {
    "yun": ["云", "芸", "允", "运", "韵"],
    "peng": ["朋", "鹏", "蓬", "彭"],
    "zhao": ["赵", "兆", "照", "昭"],
    "yu": ["余", "于", "俞", "雨", "宇", "玉", "语"],
    "ke": ["可", "科", "柯", "克"],
    "yan": ["言", "严", "颜", "燕", "妍", "岩", "延"],
    "xin": ["心", "新", "欣", "鑫", "馨"],
    "wen": ["文", "闻", "温", "雯"],
    "ming": ["明", "名", "铭", "鸣"],
    "jie": ["杰", "洁", "捷", "婕"],
    "jun": ["军", "君", "俊", "均", "峻"],
    "yi": ["一", "宜", "怡", "毅", "艺", "忆"],
    "xuan": ["宣", "轩", "萱", "璇"],
    "meng": ["梦", "猛", "蒙", "萌"],
    "shuo": ["硕", "烁", "朔"],
    "xin": ["心", "新", "欣", "鑫", "馨"],
}


def normalize_name(name: str) -> str:
    """去除称呼前缀后缀: 老师/导/哥/姐/同学"""
    for suffix in ["老师", "导", "哥", "姐", "同学", "学长", "学姐", "学弟", "学妹", "主任", "教授"]:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name


def is_pinyin_abbrev(name: str) -> bool:
    """检查是否是拼音缩写: zyp, zyf, cxy 等"""
    return bool(re.match(r'^[a-z]{2,4}$', name.lower()))


def expand_abbrev(abbrev: str, candidates: list[str]) -> list[str]:
    """将拼音缩写匹配到中文名候选: zyp → 赵云朋/赵yun朋"""
    if not is_pinyin_abbrev(abbrev):
        return []

    letters = list(abbrev.lower())
    matches = []
    for cand in candidates:
        cname = normalize_name(cand)
        # 取每个字的拼音首字母
        if len(cname) >= 2 and len(cname) == len(letters):
            initials = ""
            for ch in cname:
                # 简单映射：常见字的拼音首字母
                ch_pinyin = {
                    "赵": "z", "云": "y", "朋": "p", "鹏": "p", "余": "y", "可": "k",
                    "言": "y", "蔡": "c", "宜": "y", "君": "j", "袁": "y", "孟": "m",
                    "硕": "s", "谢": "x", "渣": "z", "焦": "j", "爱": "a", "家": "j",
                    "李": "l", "佳": "j", "韵": "y", "张": "z", "王": "w", "刘": "l",
                    "陈": "c", "杨": "y", "黄": "h", "周": "z", "吴": "w", "徐": "x",
                    "孙": "s", "马": "m", "朱": "z", "胡": "h", "郭": "g", "何": "h",
                    "高": "g", "林": "l", "罗": "l", "郑": "z", "梁": "l", "叶": "y",
                    "唐": "t", "冯": "f", "于": "y", "董": "d", "萧": "x", "程": "c",
                    "曹": "c", "邓": "d", "许": "x", "傅": "f", "沈": "s", "曾": "z",
                    "彭": "p", "吕": "l", "苏": "s", "蒋": "j", "贾": "j", "丁": "d",
                    "魏": "w", "薛": "x", "范": "f", "方": "f", "石": "s", "姚": "y",
                    "谭": "t", "廖": "l", "邹": "z", "熊": "x", "金": "j", "陆": "l",
                    "郝": "h", "孔": "k", "白": "b", "崔": "c", "康": "k", "毛": "m",
                    "邱": "q", "秦": "q", "江": "j", "史": "s", "顾": "g", "侯": "h",
                    "龙": "l", "万": "w", "段": "d", "雷": "l", "钱": "q", "汤": "t",
                    "尹": "y", "黎": "l", "易": "y", "常": "c", "武": "w", "乔": "q",
                    "贺": "h", "赖": "l", "龚": "g", "文": "w",
                }.get(ch, ch[0].lower())
                initials += ch_pinyin
            if initials == abbrev.lower():
                matches.append(cand)
    return matches


def rule_match(e1: str, e2: str) -> tuple[bool, float, str]:
    """规则匹配两个实体名

    Returns: (is_match, confidence, reason)
    """
    n1 = normalize_name(e1)
    n2 = normalize_name(e2)

    # 完全相同
    if n1 == n2:
        return True, 1.0, "identical"

    # 一个包含另一个：要求短的至少 2 个中文字符且不超长
    if (n1 in n2 or n2 in n1):
        shorter = n1 if len(n1) < len(n2) else n2
        longer = n1 if len(n1) >= len(n2) else n2
        # 短的必须是中文名（至少2个汉字）
        chinese_chars = sum(1 for c in shorter if '一' <= c <= '鿿')
        if chinese_chars >= 2 and len(shorter) >= 2:
            # 长度比不能超过 3x
            if len(longer) <= len(shorter) * 3:
                return True, 0.85, "substring"

    # 一个是另一个+称呼
    # 只在双方都包含中文字符时才匹配
    has_chinese = lambda s: any('一' <= c <= '鿿' for c in s)
    if has_chinese(n1) and has_chinese(n2):
        if n1.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") == \
           n2.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            return True, 0.95, "name+title"

    # 拼音缩写匹配
    if is_pinyin_abbrev(n1) and expand_abbrev(n1, [n2]):
        return True, 0.9, f"abbrev:{n1}→{n2}"
    if is_pinyin_abbrev(n2) and expand_abbrev(n2, [n1]):
        return True, 0.9, f"abbrev:{n2}→{n1}"

    # 同姓+角色（必须包含中文字符）
    has_chinese = lambda s: any('一' <= c <= '鿿' for c in s)
    if has_chinese(n1) and has_chinese(n2) and len(n1) >= 1 and len(n2) >= 1 and n1[0] == n2[0]:
        if ("老师" in e1 or "导" in e1) and len(n2) <= 3:
            return True, 0.7, "surname+title"
        if ("老师" in e2 or "导" in e2) and len(n1) <= 3:
            return True, 0.7, "surname+title"

    return False, 0.0, ""


def graph_structure_match(driver, name1: str, name2: str) -> float:
    """图结构相似度: 两个实体和相同的人共现 → 高概率是同一人"""
    with driver.session() as s:
        # 找 name1 的共现人
        co1 = set()
        for r in s.run(
            "MATCH (m1:Message)-[:MENTIONS]->(pe:Person_Entity {name: $n}) "
            "MATCH (m1)-[:MENTIONS]->(other:Person_Entity) "
            "WHERE other.name <> $n "
            "RETURN other.name AS co",
            n=name1
        ).data():
            co1.add(r["co"])

        # 找 name2 的共现人
        co2 = set()
        for r in s.run(
            "MATCH (m1:Message)-[:MENTIONS]->(pe:Person_Entity {name: $n}) "
            "MATCH (m1)-[:MENTIONS]->(other:Person_Entity) "
            "WHERE other.name <> $n "
            "RETURN other.name AS co",
            n=name2
        ).data():
            co2.add(r["co"])

    if not co1 or not co2:
        return 0.0

    # Jaccard 相似度
    intersection = co1 & co2
    union = co1 | co2
    return len(intersection) / len(union) if union else 0.0


def llm_confirm(name1: str, name2: str, context: str = "") -> tuple[bool, float]:
    """用 DeepSeek 判断两个名字是否指向同一个人"""
    import time
    prompt = (
        "你是中文命名实体消解专家。判断两个名字是否指向同一个人。"
        '输出JSON: {"same_person": true/false, "confidence": 0.0-1.0, "reason": "..."}\n'
        "规则: 拼音缩写=中文名(yes)、同音异字大概率是(yes)、姓+角色=姓+名(看具体情况)、"
        "完全不同的姓=no。只输出JSON。"
    )
    user_text = f"名字1: \"{name1}\"\n名字2: \"{name2}\""
    if context:
        user_text += f"\n上下文: {context}"

    for attempt in range(3):
        try:
            raw = generate(prompt, user_text, max_tokens=60, temperature=0.1)
            r = json.loads(raw)
            return r.get("same_person", False), r.get("confidence", 0.5)
        except Exception as e:
            if attempt < 2:
                time.sleep(3 ** attempt)
            else:
                # 最终 fallback：假设不匹配
                return False, 0.0


def resolve_identities(driver, dry_run: bool = False, threshold: float = 0.7, no_llm: bool = False):
    """主流程: 实体消解"""
    # 1. 获取所有 Person_Entity (带提及次数)
    entity_counts = {}
    with driver.session() as s:
        entities_data = s.run(
            "MATCH (pe:Person_Entity) WHERE pe.name IS NOT NULL AND size(pe.name) >= 2 "
            "RETURN pe.name AS name, count { (m:Message)-[:MENTIONS]->(pe) } AS cnt "
            "ORDER BY cnt DESC"
        ).data()
        for r in entities_data:
            entity_counts[r["name"]] = r["cnt"]
        entities = list(entity_counts.keys())

    print(f"=== 实体消解引擎 ===\n")
    print(f"待处理实体: {len(entities)} 个\n")

    # 过滤: 排除通用词
    generic_words = {"我们", "他们", "大家", "所有人", "有人", "别人", "自己", "一个", "未知", "？",
                     "我", "你", "他", "她", "它", "你们", "他们", "她们", "咱们",
                     # 泛化指代
                     "我朋友", "你朋友", "男生朋友", "女生朋友", "我好朋友", "俺的好朋友",
                     "你室友", "我室友", "你同学", "我同学", "你老师", "我老师",
                     "男的", "女的", "那个男的", "那个女的", "小哥", "小牛马",
                     "学长", "那个学长", "学姐", "学弟", "学妹",
                     "同学", "朋友", "老师", "室友", "舍友", "领导", "班主任",
                     "中国老师", "在的中国老师", "领导压力老师", "你们体育老师",
                     "初中同学", "我初中同学", "拍摄的同学", "动拍摄的同学",
                     "宣委", "大班宣委",
                     "姐姐", "妹妹", "哥哥", "弟弟", "爸爸", "妈妈",
                     # 不是你认识的人
                     "朋导", "朋哥", "朋老师",  # 这些不是同一人
                     "焦导", "焦老师", "焦同学", "焦爱家同学",  # 焦爱家的变体太多了
    }
    entities = [e for e in entities if e not in generic_words]
    print(f"过滤通用词后: {len(entities)} 个\n")

    # 2. 候选对生成: 规则预筛选
    print("Phase 1: 规则匹配...")
    rule_pairs = []
    for i, e1 in enumerate(entities):
        for e2 in entities[i+1:]:
            match, conf, reason = rule_match(e1, e2)
            if match:
                rule_pairs.append((e1, e2, conf, reason))

    print(f"  规则匹配: {len(rule_pairs)} 对候选")
    for e1, e2, conf, reason in rule_pairs[:15]:
        print(f"    [{conf:.2f}] {e1:15s} ↔ {e2:15s}  ({reason})")
    if len(rule_pairs) > 15:
        print(f"    ... (还有 {len(rule_pairs)-15} 对)")

    # 3. 图结构验证
    print(f"\nPhase 2: 图结构验证...")
    verified_pairs = []
    for e1, e2, conf, reason in rule_pairs:
        struct_conf = graph_structure_match(driver, e1, e2)
        combined_conf = conf * 0.7 + struct_conf * 0.3
        if combined_conf >= threshold:
            verified_pairs.append((e1, e2, combined_conf, reason, struct_conf))
        elif struct_conf > 0.3:
            # 图结构高但规则信心低 → 送LLM
            verified_pairs.append((e1, e2, combined_conf, f"{reason}+graph:{struct_conf:.2f}", struct_conf))

    print(f"  图结构验证后: {len(verified_pairs)} 对 (阈值={threshold})")

    # 4. LLM确认低置信度候选
    if no_llm:
        print(f"\nPhase 3: 跳过 LLM (--no-llm)")
        final_pairs = [(e1, e2, conf, reason, "rule+graph")
                       for e1, e2, conf, reason, _ in verified_pairs]
        llm_count = 0
    else:
        print(f"\nPhase 3: LLM确认...")
        final_pairs = []
        llm_count = 0

        for e1, e2, conf, reason, struct_conf in verified_pairs:
            if conf >= 0.85:
                final_pairs.append((e1, e2, conf, reason, "rule+graph"))
            elif conf >= threshold:
                llm_count += 1
                is_same, llm_conf = llm_confirm(e1, e2)
                if is_same and llm_conf >= 0.6:
                    final_conf = (conf + llm_conf) / 2
                    final_pairs.append((e1, e2, final_conf, reason, "rule+graph+llm"))
                if llm_count <= 10:
                    print(f"  LLM: {e1} ↔ {e2} → same={is_same} conf={llm_conf:.2f}")

    print(f"  最终确认: {len(final_pairs)} 个 Identity 组 (LLM 调用: {llm_count} 次)")

    # 5. 并查集合并
    print(f"\nPhase 4: 合并实体...")
    # Union-Find
    parent = {e: e for e in entities}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for e1, e2, _, _, _ in final_pairs:
        union(e1, e2)

    # 分组
    groups = defaultdict(list)
    for e in entities:
        groups[find(e)].append(e)

    # 排除单例组
    merged_groups = {k: v for k, v in groups.items() if len(v) >= 2}

    print(f"  合并组: {len(merged_groups)} 组 ({sum(len(v) for v in merged_groups.values())} 个实体)")
    for root, members in sorted(merged_groups.items(), key=lambda x: -len(x[1]))[:15]:
        # 用提及次数最多的作为规范名
        canonical = max(members, key=lambda m: entity_counts.get(m, 0))
        print(f"  {canonical:15s} ← {', '.join(m for m in members if m != canonical)}")

    # 6. 写入 Neo4j
    if dry_run:
        print(f"\n⚠️ DRY RUN — 未写入 Neo4j")
        return merged_groups

    print(f"\nPhase 5: 写入 Identity 节点 + SAME_AS 关系...")
    identity_count = 0

    with driver.session() as s:
        # 创建约束
        try:
            s.run("CREATE CONSTRAINT identity_name IF NOT EXISTS FOR (i:Identity) REQUIRE i.name IS UNIQUE")
        except:
            pass

        for root, members in merged_groups.items():
            canonical = max(members, key=lambda m: entity_counts.get(m, 0))
            identity_count += 1

            # 创建 Identity 节点
            s.run(
                "MERGE (i:Identity {name: $name}) "
                "SET i.canonical_name = $canonical, "
                "    i.member_count = $cnt, "
                "    i.entity_type = 'person'",
                name=canonical, canonical=canonical, cnt=len(members)
            )

            # 创建 SAME_AS 关系
            for member in members:
                s.run(
                    "MATCH (pe:Person_Entity {name: $member}) "
                    "MATCH (i:Identity {name: $canonical}) "
                    "MERGE (pe)-[:SAME_AS]->(i)",
                    member=member, canonical=canonical
                )

    print(f"  已创建 {identity_count} 个 Identity 节点")
    print(f"  已链接 {sum(len(v) for v in merged_groups.values())} 个 Person_Entity")

    return merged_groups


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只看候选，不写入 Neo4j")
    parser.add_argument("--threshold", type=float, default=0.7, help="匹配置信度阈值 (默认 0.7)")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 确认，只用规则+图结构")
    args = parser.parse_args()

    driver = GraphDatabase.driver(URI, auth=AUTH)
    try:
        resolve_identities(driver, dry_run=args.dry_run, threshold=args.threshold, no_llm=args.no_llm)
    finally:
        driver.close()
