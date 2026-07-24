#!/usr/bin/env python3
"""
Neo4j 图查询 API v3 (DeepSeek) — Flask 微服务

端点：
  GET  /api/graph/context?query=xxx          Step 0 图语义查询
  GET  /api/graph/stats                      图统计
  GET  /api/graph/path?from=X&to=Y&hops=3    对话路径链
  GET  /api/graph/community                   人物社交圈 (Louvain)
  GET  /api/graph/temporal?topic=xxx          话题时序分析
  GET  /api/graph/related?entity=xxx          图关系展开
  GET  /api/session/status                    会话状态
  POST /api/session/update                    更新状态 (持久化到 Neo4j)
  POST /api/session/reset                     重置会话

启动: python tools/graph_api.py --port 5002
"""

from flask import Flask, request, jsonify
from neo4j import GraphDatabase
import argparse, sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from semantic_query import expand_keywords, TOPIC_EXPANSION, graph_semantic_search, WORD_TO_TOPIC

app = Flask(__name__)
URI = "bolt://127.0.0.1:7687"
AUTH = ("neo4j", "neo4j123")
SESSION_ID = "xie-zhazha-session"


def get_driver():
    return GraphDatabase.driver(URI, auth=AUTH)


def _ensure_session_node(driver):
    """确保 Session 节点存在"""
    with driver.session() as s:
        s.run(
            "MERGE (s:ConversationSession {id: $sid}) "
            "SET s.updated_at = $now",
            sid=SESSION_ID, now=int(time.time()),
        )


# ===== Step 0: 图语义查询 =====

@app.route("/api/graph/context")
def context():
    query = request.args.get("query", "").strip()
    limit = request.args.get("limit", 15, type=int)
    if not query:
        return jsonify({"error": "missing query"}), 400
    results = graph_semantic_search(query, limit)
    l1, _ = expand_keywords(query)
    return jsonify({
        "query": query,
        "expanded_keywords": l1[:10],
        "result_count": len(results),
        "results": [{"msg_id": r["msg_id"], "time": r["time"], "content": r["content"][:200]} for r in results],
    })


# ===== 图统计 =====

@app.route("/api/graph/stats")
def stats():
    driver = get_driver()
    with driver.session() as s:
        result = {}
        for label, cypher in [
            ("nodes", "MATCH (n) RETURN count(n) AS cnt"),
            ("edges", "MATCH ()-[r]->() RETURN count(r) AS cnt"),
            ("target_msgs", "MATCH (p:Person {role:'target'})-[:SAID]->(m) RETURN count(m) AS cnt"),
            ("self_msgs", "MATCH (p:Person {role:'self'})-[:SAID]->(m) RETURN count(m) AS cnt"),
            ("facts", "MATCH (f:Fact) RETURN count(f) AS cnt"),
            ("topics", "MATCH (t:Topic) RETURN t.name AS name, t.mention_score AS score"),
            ("persons", "MATCH (p:Person) RETURN p.name AS name, p.role AS role"),
        ]:
            r = s.run(cypher)
            recs = [dict(rec) for rec in r]
            result[label] = recs[0] if len(recs) == 1 and "cnt" in recs[0] else recs
    driver.close()
    return jsonify(result)


# ===== P1: 对话路径链 =====

@app.route("/api/graph/path")
def path():
    from_name = request.args.get("from", "谢渣渣🙃")
    to_name = request.args.get("to", "Jajfandy")
    limit = request.args.get("limit", 10, type=int)

    driver = get_driver()
    with driver.session() as s:
        # 找最近的交替对话对
        r = s.run(
            "MATCH (p1:Person)-[:SAID]->(m1:Message) "
            "MATCH (p2:Person)-[:SAID]->(m2:Message) "
            "WHERE p1.name CONTAINS $from AND p2.name CONTAINS $to "
            "  AND abs(m1.msg_id - m2.msg_id) < 5 AND m1.msg_id <> m2.msg_id "
            "RETURN m1.content AS from_msg, m2.content AS to_msg, "
            "  m1.msg_id AS mid, m1.formatted_time AS time "
            "ORDER BY m1.msg_id DESC LIMIT $limit",
            from_=from_name, to=to_name, limit=limit,
        )
        paths = [dict(rec) for rec in r]
    driver.close()
    return jsonify({"from": from_name, "to": to_name, "count": len(paths), "paths": paths})


# ===== P1: 人物社交圈 (简化 PageRank) =====

@app.route("/api/graph/community")
def community():
    try:
        driver = get_driver()
        with driver.session() as s:
            r = s.run(
                "MATCH (p1:Person_Entity)<-[:MENTIONS]-(m:Message)-[:MENTIONS]->(p2:Person_Entity) "
                "WHERE p1.name < p2.name "
                "WITH p1, p2, count(m) AS weight WHERE weight >= 2 "
                "RETURN p1.name AS person1, p2.name AS person2, weight "
                "ORDER BY weight DESC LIMIT 30"
            )
            edges = [dict(rec) for rec in r]
        driver.close()
        return jsonify({"edges": edges, "count": len(edges)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== P1: 话题时序分析 =====

@app.route("/api/graph/temporal")
def temporal():
    topic = request.args.get("topic", "视频组")
    driver = get_driver()
    with driver.session() as s:
        # 按月份聚合该话题的消息量 + 情绪（简易版：数 emoji）
        r = s.run(
            "MATCH (m:Message)-[:ABOUT]->(t:Topic {name: $topic}) "
            "MATCH (p:Person {role: 'target'})-[:SAID]->(m) "
            "RETURN substring(m.formatted_time, 0, 7) AS month, "
            "  count(m) AS msg_count, "
            "  sum(CASE WHEN m.content CONTAINS '[大哭]' OR m.content CONTAINS '[心碎]' THEN 1 ELSE 0 END) AS sad_count "
            "ORDER BY month",
            topic=topic,
        )
        months = [dict(rec) for rec in r]
    driver.close()
    return jsonify({"topic": topic, "months": months})


# ===== P1: 图关系展开 =====

@app.route("/api/graph/related")
def related():
    entity = request.args.get("entity", "")
    driver = get_driver()
    with driver.session() as s:
        # 找与实体通过 2-hop 关联的所有节点
        r = s.run(
            "MATCH (start:Person_Entity {name: $name}) "
            "OPTIONAL MATCH (start)<-[:MENTIONS]-(m:Message)-[:ABOUT]->(t:Topic) "
            "OPTIONAL MATCH (m)-[:MENTIONS]->(other:Person_Entity) "
            "WHERE other <> start "
            "RETURN DISTINCT t.name AS topic, other.name AS connected_person, count(m) AS strength "
            "ORDER BY strength DESC LIMIT 20",
            name=entity,
        )
        rels = [dict(rec) for rec in r]
    driver.close()
    return jsonify({"entity": entity, "related": rels})


# ===== P1: 会话状态 (持久化到 Neo4j) =====

@app.route("/api/session/status")
def session_status():
    driver = get_driver()
    _ensure_session_node(driver)
    with driver.session() as s:
        r = s.run(
            "MATCH (s:ConversationSession {id: $sid}) "
            "RETURN s.mood AS mood, s.intimacy AS intimacy, "
            "  s.turn_count AS turn_count, s.last_topic AS last_topic",
            sid=SESSION_ID,
        )
        rec = r.single()
        result = dict(rec) if rec else {"mood": "neutral", "intimacy": 0.5, "turn_count": 0, "last_topic": ""}
    driver.close()
    return jsonify(result)


@app.route("/api/session/update", methods=["POST"])
def session_update():
    data = request.json or {}
    reply = data.get("reply", "")
    user_msg = data.get("user_message", "")

    driver = get_driver()
    _ensure_session_node(driver)

    with driver.session() as s:
        r = s.run("MATCH (s:ConversationSession {id: $sid}) RETURN s", sid=SESSION_ID)
        rec = r.single()
        state = dict(rec["s"]) if rec else {"mood": "neutral", "intimacy": 0.5, "turn_count": 0}

    mood = state.get("mood", 0.0)
    if isinstance(mood, str):
        mood = 0.0
    intimacy = state.get("intimacy", 0.5)
    turn = state.get("turn_count", 0) + 1

    # 用 Qwen 情绪分类器替代正则（fallback: 增强规则）
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        from emotion_classifier import classify, mood_delta
        classification = classify(reply)
        delta = mood_delta(classification)
    except Exception:
        delta = 0.0

    mood = round(max(-1.0, min(1.0, float(mood) + delta)), 3)
    # intimacy 微调
    if delta < -0.1:
        intimacy = round(max(0.0, float(intimacy) - 0.05), 3)
    elif delta > 0.05:
        intimacy = round(min(1.0, float(intimacy) + 0.03), 3)

    with driver.session() as s:
        s.run(
            "MATCH (s:ConversationSession {id: $sid}) "
            "SET s.mood = $mood, s.intimacy = $intimacy, "
            "  s.turn_count = $turn, s.last_topic = $topic, s.updated_at = $now",
            sid=SESSION_ID, mood=mood, intimacy=intimacy,
            turn=turn, topic=user_msg[:50], now=int(time.time()),
        )
    driver.close()
    return jsonify({"mood": mood, "intimacy": intimacy, "turn_count": turn, "mood_delta": delta})


@app.route("/api/session/reset", methods=["POST"])
def session_reset():
    driver = get_driver()
    _ensure_session_node(driver)
    with driver.session() as s:
        s.run(
            "MATCH (s:ConversationSession {id: $sid}) "
            "SET s.mood = 'neutral', s.intimacy = 0.5, s.turn_count = 0, "
            "  s.last_topic = '', s.updated_at = $now",
            sid=SESSION_ID, now=int(time.time()),
        )
    driver.close()
    return jsonify({"status": "reset", "mood": "neutral", "intimacy": 0.5, "turn_count": 0})


# ===== 智能查询路由 =====

TIME_KEYWORDS = ["什么时候", "最近", "变化", "越来越", "趋势", "开学", "放假", "这学期"]


def _entity_lookup(driver, query: str) -> dict:
    """图实体链接：在 Neo4j 中查找 query 匹配的实体（名称+别名双向匹配）"""
    with driver.session() as s:
        # Person_Entity：名称匹配 OR 别名匹配
        r = s.run(
            "MATCH (pe:Person_Entity) "
            "WHERE $q CONTAINS pe.name OR any(alias IN pe.aliases WHERE $q CONTAINS alias) "
            "RETURN pe.name AS name, 'person' AS type, pe.role AS role "
            "ORDER BY size(pe.name) DESC LIMIT 5",
            q=query,
        )
        persons = [dict(rec) for rec in r]
        # Topic：名称匹配 OR 关键词匹配
        r2 = s.run(
            "MATCH (t:Topic) "
            "WHERE $q CONTAINS t.name OR any(kw IN t.keywords WHERE $q CONTAINS kw) "
            "RETURN t.name AS name, 'topic' AS type "
            "ORDER BY size(t.name) DESC LIMIT 5",
            q=query,
        )
        topics = [dict(rec) for rec in r2]
        # 已知 Person
        r3 = s.run(
            "MATCH (p:Person) WHERE $q CONTAINS p.name "
            "RETURN p.name AS name, p.role AS role LIMIT 3",
            q=query,
        )
        known = [dict(rec) for rec in r3]
    return {"persons": persons, "topics": topics, "known_persons": known}


@app.route("/api/graph/smart")
def smart_query():
    """图驱动的智能路由：用 Neo4j 实体链接替代硬编码名单"""
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "missing query"}), 400

    driver = get_driver()
    result = {"query": query, "routes_used": [], "data": {}}

    # 实体链接
    entities = _entity_lookup(driver, query)
    result["data"]["entities_found"] = entities
    matched_person = entities["persons"][0]["name"] if entities["persons"] else None

    # 路由 1: 人物实体 → related + path
    if matched_person:
        result["routes_used"].append("related")
        with driver.session() as s:
            r = s.run(
                "MATCH (p:Person)-[:SAID]->(m:Message) WHERE m.content CONTAINS $name "
                "RETURN p.role AS speaker, count(m) AS cnt, collect(m.content)[0..3] AS samples",
                name=matched_person,
            )
            result["data"]["mentions_by_speaker"] = [dict(rec) for rec in r]

            r2 = s.run(
                "MATCH (m:Message) WHERE m.content CONTAINS $name "
                "MATCH (m)-[:ABOUT]->(t:Topic) "
                "WITH t, count(m) AS cnt RETURN t.name AS topic, cnt ORDER BY cnt DESC LIMIT 5",
                name=matched_person,
            )
            result["data"]["related_topics"] = [dict(rec) for rec in r2]

        result["routes_used"].append("path")
        with driver.session() as s:
            r3 = s.run(
                "MATCH (p1:Person {role:'target'})-[:SAID]->(m1:Message) WHERE m1.content CONTAINS $name "
                "MATCH (p2:Person {role:'self'})-[:SAID]->(m2:Message) WHERE m2.content CONTAINS $name "
                "AND abs(m1.msg_id - m2.msg_id) < 10 "
                "RETURN m1.content AS her_msg, m2.content AS your_msg, m1.msg_id AS mid "
                "ORDER BY m1.msg_id DESC LIMIT 5",
                name=matched_person,
            )
            result["data"]["conversation_pairs"] = [dict(rec) for rec in r3]

    # 路由 2: 时间/趋势查询
    if any(kw in query for kw in TIME_KEYWORDS):
        result["routes_used"].append("temporal")
        topics_to_check = list(set(
            t for word, topics in WORD_TO_TOPIC.items()
            if word in query for t in topics
        ))[:3]
        temporal_data = {}
        for topic in topics_to_check:
            with driver.session() as s:
                r = s.run(
                    "MATCH (m:Message)-[:ABOUT]->(t:Topic {name: $topic}) "
                    "MATCH (p:Person {role: 'target'})-[:SAID]->(m) "
                    "RETURN substring(m.formatted_time, 0, 7) AS month, count(m) AS cnt "
                    "ORDER BY month",
                    topic=topic,
                )
                temporal_data[topic] = [dict(rec) for rec in r]
        result["data"]["temporal"] = temporal_data

    # 路由 3: 语义查询（默认）
    if not result["routes_used"] or "context" not in result["routes_used"]:
        result["routes_used"].append("context")
        results = graph_semantic_search(query, 10)
        result["data"]["semantic_results"] = [
            {"msg_id": r["msg_id"], "time": r["time"], "content": r["content"][:200]}
            for r in results
        ]

    # 附加：当前会话状态（复用最后一个 session 或新开一个）
    with driver.session() as s:
        r = s.run("MATCH (s:ConversationSession {id: $sid}) RETURN s", sid=SESSION_ID)
        rec = r.single()
        if rec:
            state = dict(rec["s"])
            result["session"] = {
                "mood": state.get("mood", "neutral"),
                "intimacy": state.get("intimacy", 0.5),
                "turn_count": state.get("turn_count", 0),
            }

    driver.close()
    return jsonify(result)


# ===== DeepSeek Enrichment =====

@app.route("/api/enrich", methods=["POST"])
def enrich_message():
    """单条消息 DeepSeek API 推理 → 情绪+人物+对话角色"""
    data = request.json or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "missing content"}), 400

    try:
        from deepseek_client import enrich_one
        result = enrich_one(content)
        return jsonify(result)
    except Exception as e:
        return jsonify({"emotion": "unknown", "intensity": 0.0, "persons": [], "conversation_role": "response",
                        "_error": str(e)[:80]})


@app.route("/api/graph/social", methods=["GET"])
def social_context():
    """查询谢渣渣与某人的社会关系上下文"""
    person = request.args.get("person", "").strip()
    if not person:
        return jsonify({"error": "missing person param"}), 400

    driver = get_driver()
    result = {"person": person, "found": False}

    try:
        with driver.session() as s:
            # 查找匹配的 Person
            matched = s.run(
                "MATCH (p:Person) WHERE p.name CONTAINS $name "
                "RETURN p.name AS name LIMIT 1",
                name=person
            ).data()

            if matched:
                pname = matched[0]["name"]
                result["matched_name"] = pname
                result["found"] = True

                # 1. 谢渣渣提到这个人的消息
                mentions = s.run(
                    "MATCH (xzz:Person {name: '谢渣渣🙃'})-[:SAID]->(m:Message) "
                    "WHERE m.content CONTAINS $name "
                    "RETURN m.content AS c, m.emotion AS e, m.formatted_time AS t "
                    "ORDER BY m.msg_id DESC LIMIT 5",
                    name=pname
                ).data()
                result["mentions"] = [
                    {"content": r["c"][:120], "emotion": r["e"] or "?", "time": r["t"]}
                    for r in mentions
                ]

                # 2. 互动强度
                rel = s.run(
                    "MATCH (xzz:Person {name: '谢渣渣🙃'})-[r:INTERACTS_WITH]-(p:Person {name: $name}) "
                    "RETURN r.interaction_strength AS strength, r.relationship_type AS type, r.direct_replies AS replies",
                    name=pname
                ).data()
                if rel:
                    result["relationship"] = {
                        "strength": rel[0]["strength"],
                        "type": rel[0]["type"],
                        "direct_replies": rel[0]["replies"],
                    }

                # 3. 共同群聊
                shared = s.run(
                    "MATCH (xzz:Person {name: '谢渣渣🙃'})-[:SAID]->(m1:Message) "
                    "MATCH (p:Person {name: $name})-[:SAID]->(m2:Message) "
                    "WHERE m1.source_file = m2.source_file AND m1.source_file STARTS WITH '群聊' "
                    "RETURN DISTINCT m1.source_file AS grp, count(DISTINCT m1) AS her, count(DISTINCT m2) AS their "
                    "ORDER BY her DESC",
                    name=pname
                ).data()
                result["shared_groups"] = [
                    {"group": r["grp"].replace("群聊_", "").replace(".json", ""),
                     "her_msgs": r["her"], "their_msgs": r["their"]}
                    for r in shared
                ]

                # 4. Identity
                ident = s.run(
                    "MATCH (pe:Person_Entity)-[:SAME_AS]->(i:Identity) "
                    "WHERE pe.name CONTAINS $name OR i.name CONTAINS $name "
                    "RETURN i.name AS identity, collect(DISTINCT pe.name) AS aliases",
                    name=pname
                ).data()
                result["identity"] = [
                    {"identity": r["identity"], "aliases": r["aliases"]}
                    for r in ident
                ]
    finally:
        driver.close()

    return jsonify(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # 启动时初始化 Session 节点
    driver = get_driver()
    _ensure_session_node(driver)
    with driver.session() as s:
        s.run("CREATE CONSTRAINT session_id IF NOT EXISTS FOR (s:ConversationSession) REQUIRE s.id IS UNIQUE")
    driver.close()

    # 验证 DeepSeek API 可用性
    print("验证 DeepSeek API...")
    try:
        from deepseek_client import get_model
        get_model()
    except Exception as e:
        print(f"  ⚠️ DeepSeek API 验证失败 ({e})，首次调用将自动重试")

    print(f"🚀 Graph API v3 (DeepSeek): http://{args.host}:{args.port}")
    for ep in ["/api/graph/context?query=...", "/api/graph/stats", "/api/graph/path?from=...&to=...",
               "/api/graph/community", "/api/graph/temporal?topic=...", "/api/graph/related?entity=..."]:
        print(f"   {ep}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
