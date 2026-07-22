#!/usr/bin/env python3
"""全量批处理 — 短消息规则秒过 + 长消息 DeepSeek API batch"""
import sys, os, json, time, argparse
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emotion_classifier import rule_based_classify
from deepseek_client import generate_batch

def enrich_fast(content):
    r = rule_based_classify(content)
    return {"emotion":r.get("emotion","neutral"),"intensity":r.get("intensity",0),"persons":[],"conversation_role":"response"}

def run():
    p = argparse.ArgumentParser(); p.add_argument("--sample",type=int,default=0); p.add_argument("--all",action="store_true")
    args = p.parse_args()
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j","neo4j123"))
    with driver.session() as s:
        total = s.run("MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) WHERE NOT m.content CONTAINS '我通过了你的朋友验证请求' RETURN count(m) AS c").single()["c"]
    limit = args.sample if args.sample else total
    with driver.session() as s:
        msgs = [{"msg_id":r["mid"],"content":r["c"]} for r in s.run(
            "MATCH (p:Person {role:'target'})-[:SAID]->(m:Message) WHERE NOT m.content CONTAINS '我通过了你的朋友验证请求' RETURN m.msg_id AS mid, m.content AS c ORDER BY m.msg_id LIMIT "+str(limit))]

    short = [m for m in msgs if len(m["content"]) <= 8]
    long = [m for m in msgs if len(m["content"]) > 8]
    print(f"总{len(msgs)} 短{len(short)}(规则) 长{len(long)}(DeepSeek batch10)", flush=True)

    start = time.time()
    all_e = Counter(); all_r = Counter(); all_p = Counter(); all_l = []
    emoji_c = swear_c = 0
    B = 20  # Neo4j write batch

    # Phase 1: 短消息规则秒过
    buf = []
    for m in short:
        e = enrich_fast(m["content"])
        buf.append((m["msg_id"],e["emotion"],e["intensity"],e["conversation_role"],e["persons"]))
        all_e[e["emotion"]]+=1; all_r[e["conversation_role"]]+=1; all_l.append(len(m["content"]))
    with driver.session() as s:
        for mid,em,i,r,pns in buf:
            s.run("MATCH (m:Message {msg_id:$mid}) SET m.emotion=$e, m.intensity=$i, m.conversation_role=$r", mid=mid,e=em,i=i,r=r)
            for pn in pns:
                if pn and len(pn)>=2: s.run("MERGE (pe:Person_Entity {name:$n}) SET pe.entity_type='person'",n=pn); s.run("MATCH (m:Message {msg_id:$mid}) MATCH (pe:Person_Entity {name:$n}) MERGE (m)-[:MENTIONS]->(pe)",mid=mid,n=pn)
    t1 = time.time()-start
    print(f"短消息完成: {len(short)}条 ({t1:.0f}s)", flush=True)
    buf = []

    # Phase 2: 长消息 DeepSeek API batch (10条/批)
    QB = 10
    for bi in range(0, len(long), QB):
        batch = long[bi:bi+QB]
        enrichments = generate_batch([m["content"] for m in batch])
        for m, e in zip(batch, enrichments):
            content = m["content"]; em = e.get("emotion","unknown"); role = e.get("conversation_role","response"); pns = e.get("persons",[])
            buf.append((m["msg_id"],em,e.get("intensity",0),role,pns))
            all_e[em]+=1; all_r[role]+=1; all_l.append(len(content))
            for pn in pns: all_p[pn]+=1
            if "[" in content and "]" in content: emoji_c+=1
            if any(w in content for w in ["草","吗的","傻逼","他妈","我日"]): swear_c+=1
        if len(buf) >= B or bi+QB >= len(long):
            with driver.session() as s:
                for mid,em,i,r,pns in buf:
                    s.run("MATCH (m:Message {msg_id:$mid}) SET m.emotion=$e, m.intensity=$i, m.conversation_role=$r",mid=mid,e=em,i=i,r=r)
                    for pn in pns:
                        if pn and len(pn)>=2: s.run("MERGE (pe:Person_Entity {name:$n}) SET pe.entity_type='person'",n=pn); s.run("MATCH (m:Message {msg_id:$mid}) MATCH (pe:Person_Entity {name:$n}) MERGE (m)-[:MENTIONS]->(pe)",mid=mid,n=pn)
            buf = []
        done = len(short)+bi+len(batch)
        elapsed = time.time()-start
        rate = done/elapsed if elapsed>0 else 0
        eta = (len(msgs)-done)/rate/60 if rate>0 else 0
        print(f"[{done}/{len(msgs)}] {rate:.1f}/s ETA:{eta:.0f}min | 情绪={em} 人物={pns} | {content[:30]}...", flush=True)

    elapsed = time.time()-start
    profile = {
        "_meta":{"total":len(msgs),"time_s":round(elapsed,0),"per_msg_s":round(elapsed/len(msgs),2)},
        "expression":{"avg_len":round(sum(all_l)/len(all_l),1),"emoji_rate":round(emoji_c/len(msgs),3),"swear_rate":round(swear_c/len(msgs),3)},
        "emotion_dist":dict(all_e.most_common()),"role_dist":dict(all_r.most_common()),
        "top_persons":[{"name":n,"count":c} for n,c in all_p.most_common(30)],
    }
    with open("tools/personality_profile_full.json","w",encoding="utf-8") as f: json.dump(profile,f,ensure_ascii=False,indent=2)
    print(f"\n✅ 完成 {len(msgs)}条 {elapsed/60:.1f}min\n情绪:{dict(all_e.most_common(5))}\n报告:tools/personality_profile_full.json", flush=True)
    driver.close()

if __name__ == "__main__": run()
