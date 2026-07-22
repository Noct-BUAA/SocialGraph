#!/usr/bin/env python3
"""
Neo4j 数据导入器 — 消息解析结果 → Neo4j 图数据库

从 chat_parser.py 的 JSONL 输出读取标准化消息，
批量导入 Neo4j，建立 Person 节点 + Message 节点 + SAID/RECEIVED/REPLY_TO 关系。

用法：
    python neo4j_loader.py --file messages.jsonl --uri bolt://localhost:7687 --user neo4j --password <pw>

首次使用需要先创建数据库密码（Neo4j Desktop 中操作或在浏览器 localhost:7474 设置）。
"""

from __future__ import annotations

import json
import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase, basic_auth


SCHEMA_CYPHER = [
    # 约束 — 保证 msg_id / name 唯一
    "CREATE CONSTRAINT message_id IF NOT EXISTS FOR (m:Message) REQUIRE m.msg_id IS UNIQUE;",
    "CREATE CONSTRAINT person_name IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE;",
    # 索引 — 加速查询
    "CREATE INDEX message_time IF NOT EXISTS FOR (m:Message) ON (m.timestamp);",
    "CREATE INDEX message_type IF NOT EXISTS FOR (m:Message) ON (m.msg_type);",
    "CREATE INDEX person_role IF NOT EXISTS FOR (p:Person) ON (p.role);",
]


class Neo4jLoader:
    """Neo4j 批量数据导入器"""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        self._batch_size = 500

    def close(self):
        self.driver.close()

    def test_connection(self) -> bool:
        """测试数据库连接"""
        try:
            with self.driver.session(database=self.database) as session:
                result = session.run("RETURN 1 AS test")
                record = result.single()
                return record and record["test"] == 1
        except Exception as e:
            print(f"  ❌ 连接失败: {e}")
            return False

    def init_schema(self) -> None:
        """创建约束和索引"""
        print("创建 Schema (约束 + 索引)...")
        with self.driver.session(database=self.database) as session:
            for cypher in SCHEMA_CYPHER:
                try:
                    session.run(cypher)
                    print(f"  ✅ {cypher[:60]}...")
                except Exception as e:
                    # 约束/索引已存在不算错误
                    if "already exists" in str(e) or "EquivalentSchemaRule" in str(e):
                        print(f"  ⏭️  已存在: {cypher[:50]}...")
                    else:
                        print(f"  ⚠️  {cypher[:50]}... → {e}")

    def load_messages(self, messages: list[dict]) -> dict:
        """批量导入消息节点和 Person 节点 + SAID/RECEIVED 关系。

        返回统计: {person_count, message_count, relationship_count}
        """
        stats = {"person_count": 0, "message_count": 0, "relationship_count": 0}

        # Step 1: 收集所有 Person
        persons = set()
        for msg in messages:
            sender = msg.get("sender", "")
            role = msg.get("sender_role", "")
            if sender and role in ("target", "self", "system", "group_member"):
                persons.add((sender, role))

        print(f"\n导入 Person 节点 ({len(persons)} 个)...")
        with self.driver.session(database=self.database) as session:
            for name, role in persons:
                session.run(
                    "MERGE (p:Person {name: $name}) "
                    "SET p.role = $role",
                    name=name, role=role,
                )
            stats["person_count"] = len(persons)
        print(f"  ✅ {len(persons)} Person 节点")

        # Step 2: 批量导入 Message 节点
        print(f"\n导入 Message 节点 ({len(messages)} 条)...")
        batch_size = self._batch_size
        total = len(messages)

        with self.driver.session(database=self.database) as session:
            for batch_start in range(0, total, batch_size):
                batch = messages[batch_start : batch_start + batch_size]
                batch_params = [
                    {
                        "msg_id": m["msg_id"],
                        "content": m["content"],
                        "timestamp": m["timestamp"],
                        "formatted_time": m["formatted_time"],
                        "msg_type": m["msg_type"],
                        "sender": m["sender"],
                        "sender_role": m["sender_role"],
                        "source_file": m.get("source_file", ""),
                    }
                    for m in batch
                ]

                session.run(
                    "UNWIND $batch AS row "
                    "CREATE (m:Message {"
                    "  msg_id: row.msg_id, "
                    "  content: row.content, "
                    "  timestamp: row.timestamp, "
                    "  formatted_time: row.formatted_time, "
                    "  msg_type: row.msg_type, "
                    "  source_file: row.source_file"
                    "}) "
                    "SET m.sender_role = row.sender_role",
                    batch=batch_params,
                )
                stats["message_count"] += len(batch)
                pct = min(100, (batch_start + len(batch)) * 100 // total)
                print(f"  [{pct:3d}%] {batch_start + len(batch)}/{total} 消息节点")

        # Step 3: 创建 SAID 关系
        print(f"\n创建 SAID 关系...")
        with self.driver.session(database=self.database) as session:
            for batch_start in range(0, total, batch_size):
                batch = messages[batch_start : batch_start + batch_size]
                batch_params = [
                    {"msg_id": m["msg_id"], "sender": m["sender"], "sender_role": m["sender_role"]}
                    for m in batch
                    if m["sender_role"] in ("target", "self", "system", "group_member")
                ]

                result = session.run(
                    "UNWIND $batch AS row "
                    "MATCH (p:Person {name: row.sender}) "
                    "MATCH (msg:Message {msg_id: row.msg_id}) "
                    "CREATE (p)-[:SAID]->(msg) "
                    "RETURN count(*) AS cnt",
                    batch=batch_params,
                )
                cnt = result.single()["cnt"]
                stats["relationship_count"] += cnt

        print(f"  ✅ {stats['relationship_count']} 条 SAID 关系")

        # Step 4: 创建 REPLY_TO 关系
        print(f"\n创建 REPLY_TO 关系...")
        reply_count = 0
        with self.driver.session(database=self.database) as session:
            for batch_start in range(0, total, batch_size):
                batch = messages[batch_start : batch_start + batch_size]
                batch_params = [
                    {"msg_id": m["msg_id"], "reply_to": m["reply_to"]}
                    for m in batch
                    if m.get("reply_to") is not None
                ]

                if not batch_params:
                    continue

                result = session.run(
                    "UNWIND $batch AS row "
                    "MATCH (m1:Message {msg_id: row.msg_id}) "
                    "MATCH (m2:Message {msg_id: row.reply_to}) "
                    "CREATE (m1)-[:REPLY_TO]->(m2) "
                    "RETURN count(*) AS cnt",
                    batch=batch_params,
                )
                cnt = result.single()["cnt"]
                reply_count += cnt

        stats["relationship_count"] += reply_count
        print(f"  ✅ {reply_count} 条 REPLY_TO 关系")

        return stats

    def get_stats(self) -> dict:
        """查询图数据库统计信息"""
        stats = {}
        with self.driver.session(database=self.database) as session:
            queries = {
                "person_count": "MATCH (p:Person) RETURN count(p) AS cnt",
                "message_count": "MATCH (m:Message) RETURN count(m) AS cnt",
                "said_count": "MATCH ()-[r:SAID]->() RETURN count(r) AS cnt",
                "reply_to_count": "MATCH ()-[r:REPLY_TO]->() RETURN count(r) AS cnt",
                "target_person": (
                    "MATCH (p:Person {role: 'target'}) RETURN p.name AS name"
                ),
                "self_person": (
                    "MATCH (p:Person {role: 'self'}) RETURN p.name AS name"
                ),
            }
            for key, cypher in queries.items():
                result = session.run(cypher)
                record = result.single()
                if record:
                    val = record.get("cnt") or record.get("name")
                    stats[key] = val

        return stats


def read_messages(file_path: str) -> list[dict]:
    """从 JSONL 文件读取消息列表"""
    messages = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo4j 聊天数据导入器")
    parser.add_argument("--file", required=True, help="消息 JSONL 文件路径 (chat_parser.py 输出)")
    parser.add_argument(
        "--uri", default="bolt://localhost:7687", help="Neo4j Bolt URI (默认: bolt://localhost:7687)"
    )
    parser.add_argument("--user", default="neo4j", help="Neo4j 用户名 (默认: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j 密码")
    parser.add_argument("--database", default="neo4j", help="数据库名 (默认: neo4j)")
    parser.add_argument("--init-only", action="store_true", help="仅创建 Schema，不导入数据")
    parser.add_argument("--stats", action="store_true", help="仅查询统计信息")

    args = parser.parse_args()

    file_path = Path(args.file)
    if not args.init_only and not args.stats and not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        print("   请先运行 chat_parser.py 生成消息 JSONL 文件")
        sys.exit(1)

    loader = Neo4jLoader(args.uri, args.user, args.password, args.database)

    print(f"连接 Neo4j: {args.uri} ...")
    if not loader.test_connection():
        print("\n💡 请确认 Neo4j 已启动:")
        print("   1. 打开 Neo4j Desktop")
        print("   2. 创建/启动一个 Local DBMS")
        print("   3. 确认 Bolt port 为 7687")
        sys.exit(1)
    print("  ✅ 连接成功")

    try:
        if args.stats:
            print("\n📊 图数据库统计:")
            stats = loader.get_stats()
            for key, val in stats.items():
                print(f"  {key}: {val}")
            return

        # 创建 Schema
        loader.init_schema()

        if args.init_only:
            print("\n✅ Schema 初始化完成（未导入数据）")
            return

        # 加载消息
        print(f"\n读取消息文件: {file_path}")
        messages = read_messages(str(file_path))
        print(f"  共 {len(messages)} 条消息")

        # 导入
        print(f"\n开始导入...")
        start = time.time()
        stats = loader.load_messages(messages)
        elapsed = time.time() - start

        print(f"\n{'='*50}")
        print(f"✅ 导入完成 ({elapsed:.1f}s)")
        print(f"  Person 节点:    {stats['person_count']}")
        print(f"  Message 节点:   {stats['message_count']}")
        print(f"  关系 (SAID+REPLY): {stats['relationship_count']}")
        print(f"\n💡 在 Neo4j Browser (http://localhost:7474) 中运行:")
        print(f"   MATCH (n)-[r]->(m) RETURN n, r, m LIMIT 50")
    finally:
        loader.close()


if __name__ == "__main__":
    main()
