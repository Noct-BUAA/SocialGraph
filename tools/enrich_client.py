#!/usr/bin/env python3
"""通过 graph_api /api/enrich 端点处理剩余消息（复用已加载Qwen，不额外占显存）"""
import urllib.request, json, time, sys
from neo4j import GraphDatabase

API = "http://127.0.0.1:5002/api/enrich"
driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", "neo4j123"))

# 找未标注
with driver.session() as s:
    r = s.run("MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) WHERE m.emotion IS NULL "
              "RETURN m.msg_id AS mid, m.content AS c")
    msgs = [(rec["mid"], rec["c"]) for rec in r]
print(f"未标注: {len(msgs)}")

buf = []
start = time.time()
for i, (mid, content) in enumerate(msgs, 1):
    try:
        req = urllib.request.Request(API, data=json.dumps({"content": content}).encode(),
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        e = json.loads(resp.read())
    except Exception as ex:
        e = {"emotion": "unknown", "intensity": 0, "persons": [], "conversation_role": "response"}

    em = e.get("emotion", "unknown")
    pns = e.get("persons", [])
    role = e.get("conversation_role", "response")
    buf.append((mid, em, e.get("intensity", 0), role, pns))

    if len(buf) >= 20 or i == len(msgs):
        with driver.session() as s:
            for mid2, em2, i2, r2, pns2 in buf:
                s.run("MATCH (m:Message {msg_id:$mid}) SET m.emotion=$e, m.intensity=$i, m.conversation_role=$r",
                      mid=mid2, e=em2, i=i2, r=r2)
                for pn in pns2:
                    if pn and len(pn) >= 2:
                        s.run("MERGE (pe:Person_Entity {name:$n}) SET pe.entity_type='person'", n=pn)
                        s.run("MATCH (m:Message {msg_id:$mid}) MATCH (pe:Person_Entity {name:$n}) MERGE (m)-[:MENTIONS]->(pe)",
                              mid=mid2, n=pn)
        buf = []

    elapsed = time.time() - start
    rate = i / elapsed if elapsed > 0 else 0
    eta = (len(msgs) - i) / rate / 60 if rate > 0 else 0
    print(f"[{i}/{len(msgs)}] {rate:.1f}/s ETA:{eta:.0f}min | {em} | {content[:40]}...", flush=True)

elapsed = time.time() - start
print(f"\n✅ {len(msgs)}条 {elapsed/60:.1f}min", flush=True)
driver.close()
