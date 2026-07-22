#!/usr/bin/env python3
"""补全未标注消息 — 单条 DeepSeek API 推理"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deepseek_client import enrich_one
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j","neo4j123"))
with driver.session() as s:
    r = s.run("MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) WHERE m.emotion IS NULL RETURN m.msg_id AS mid, m.content AS c")
    msgs = [(rec["mid"],rec["c"]) for rec in r]
print(f"未标注: {len(msgs)} 条")

start = time.time()
buf = []
for i, (mid, content) in enumerate(msgs, 1):
    e = enrich_one(content)
    em = e.get("emotion","unknown"); pns = e.get("persons",[]); role = e.get("conversation_role","response")
    buf.append((mid, em, e.get("intensity",0), role, pns))
    if len(buf) >= 20 or i == len(msgs):
        with driver.session() as s:
            for mid2, em2, i2, r2, pns2 in buf:
                s.run("MATCH (m:Message {msg_id:$mid}) SET m.emotion=$e, m.intensity=$i, m.conversation_role=$r", mid=mid2, e=em2, i=i2, r=r2)
                for pn in pns2:
                    if pn and len(pn)>=2: s.run("MERGE (pe:Person_Entity {name:$n}) SET pe.entity_type='person'",n=pn); s.run("MATCH (m:Message {msg_id:$mid}) MATCH (pe:Person_Entity {name:$n}) MERGE (m)-[:MENTIONS]->(pe)",mid=mid2,n=pn)
        buf = []
    elapsed = time.time()-start
    rate = i/elapsed if elapsed>0 else 0
    eta = (len(msgs)-i)/rate/60 if rate>0 else 0
    print(f"[{i}/{len(msgs)}] {rate:.1f}/s ETA:{eta:.0f}min | {em} {pns} | {content[:30]}...", flush=True)

elapsed = time.time()-start
with driver.session() as s:
    labeled = s.run("MATCH (m:Message) WHERE m.emotion IS NOT NULL RETURN count(m) AS c").single()["c"]
    total = s.run("MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) RETURN count(m) AS c").single()["c"]
print(f"\n✅ {labeled}/{total} ({elapsed/60:.1f}min)", flush=True)
driver.close()
