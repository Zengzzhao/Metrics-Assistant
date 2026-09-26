"""将 IE result.json 原子替换为单篇论文知识图谱；--dry-run 仅转换与检查。"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from neo4j.exceptions import Neo4jError, DriverError

from ie.models import Evidence, IndicatorRelation, PaperRelation

OWNER = "ie_import"
PI_TYPES = ("PROPOSES", "MODIFIES", "APPLIES")
II_TYPES = ("VARIANT_OF", "DERIVED_FROM", "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_payload(result_path: Path, source: Path, paper_id=None, title=None):
    # 基于内容生成一个hash给paper_id
    result_bytes = result_path.read_bytes()
    data = json.loads(result_bytes)
    source_bytes = source.read_bytes()
    raw = source_bytes.decode("utf-8")
    source_hash = digest(source_bytes)
    paper_id = paper_id or "paper:" + source_hash
    if not paper_id.strip():
        raise ValueError("paper_id 不能为空")
    # title，依次使用函数传入的、md的一级标题中提取、文件名
    title = title or next((line[2:].strip() for line in raw.splitlines() if line.startswith("# ")), source.stem)
    
    paper = dict(id=paper_id, title=title, source_file=str(source.resolve()),
                 source_sha256=source_hash, result_file=str(result_path.resolve()),
                 result_sha256=digest(result_bytes), status=data["status"],
                 merge_map_json=encode(data["merge_map"]), chunks_json=encode(data["chunks"]),
                 skipped_sections_json=encode(data["skipped_sections"]))
    indicators, relations, mentions = [], [], []
    known = {}

    def evidence_props(items):
        # 保持证据数组顺序及下标，完整保存文字、视觉和多位置溯源。
        for item in items:
            Evidence.model_validate({k: v for k, v in item.items() if k in Evidence.model_fields})
            if item.get("source_file") and Path(item["source_file"]).resolve() != source.resolve():
                raise ValueError("证据 source_file 与 --source 不一致")
            for span in item.get("source_spans", []):
                a, b = span["start"], span["end"]
                if not 0 <= a < b <= len(raw) or raw[a:b] != span["quote"]:
                    raise ValueError("证据位置与当前 Markdown 不一致，请使用抽取时的原文")
        return {"evidence_json": encode(items),
                "evidence_quotes": [item["quote"] for item in items],
                "evidence_count": len(items)}

    # 遍历指标，整理指标属性放入数组中得到指标节点。
    for item in data["indicators"]:
        iid = item["indicator_id"]
        if not isinstance(iid, str) or not iid.strip() or iid in known:
            raise ValueError(f"指标 ID 为空或重复：{iid}")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError(f"指标名称为空：{iid}")
        known[iid] = paper_id + ":indicator:" + iid
        props = {k: item.get(k, []) for k in ("aliases", "definitions", "source_chunk_ids", "candidate_ids")}
        for key, value in props.items():
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError(f"{iid}.{key} 必须是字符串数组")
        props.update(local_id=iid, name=item["name"], payload_json=encode(item))
        props.update(evidence_props(item["evidence"]))
        indicators.append(dict(id=known[iid], props=props))

    def validate_relation(row, model):
        clean = {**row, "evidence": [{k: v for k, v in e.items() if k in Evidence.model_fields} for e in row["evidence"]]}
        model.model_validate(clean)
        return evidence_props(row["evidence"])

    def relation_props(subject, predicate, obj):
        return {"relation_id": paper_id + ":relation:" + digest(encode([subject, predicate, obj]).encode()),
                "paper_id": paper_id, "importer": OWNER, "status": data["status"]}

    def add_relation(row, subject, obj, kind, evidence):
        props = relation_props(subject, row["predicate"], obj)
        props.update(evidence, assertion_mode=row["assertion_mode"],
                     rationale_summary=row.get("rationale_summary"))
        relations.append(dict(subject=subject, object=obj, kind=kind,
                              predicate=row["predicate"], props=props))

    covered = set()
    # 遍历论文关系，整理关系属性放入数组中得到关系节点。
    for row in data["paper_relations"]:
        iid = row["indicator_id"]
        if iid not in known or iid in covered:
            raise ValueError(f"PI 未知或重复指标：{iid}")
        covered.add(iid)
        evidence = validate_relation(row, PaperRelation)
        props = relation_props(paper_id, "MENTIONS", known[iid])
        # 如果没有语义关系，则在 MENTIONS 关系上添加原因及证据属性。
        if row["predicate"] is None:
            props.update(evidence, no_relation_reason=row["no_relation_reason"],
                         no_relation_detail=row["no_relation_detail"],
                         rationale_summary=row.get("rationale_summary"))
        mentions.append(dict(id=known[iid], props=props))
        # 如果有语义关系，则添加到关系数组中
        if row["predicate"] is not None:
            add_relation(row, paper_id, known[iid], "PI", evidence)
    if covered != set(known):
        raise ValueError(f"PI 未覆盖指标：{sorted(set(known) - covered)}")
    seen = set()
    order = {iid: n for n, iid in enumerate(known)}
    # 遍历指标关系，整理关系属性放入数组中得到关系节点。
    for row in data["indicator_relations"]:
        a, b, predicate = row["subject_id"], row["object_id"], row["predicate"]
        if a not in known or b not in known or a == b:
            raise ValueError(f"II 未知端点或自关系：{a} {predicate} {b}")
        if predicate == "ALTERNATIVE_TO" and order[a] > order[b]:
            a, b = b, a
        key = (a, predicate, b)
        if key in seen:
            raise ValueError(f"II 重复关系：{key}")
        seen.add(key)
        evidence = validate_relation(row, IndicatorRelation)
        add_relation(row, known[a], known[b], "II", evidence)
    for a, predicate, b in seen:
        if predicate == "VARIANT_OF" and (a, "DERIVED_FROM", b) in seen:
            raise ValueError(f"II VARIANT_OF/DERIVED_FROM 冲突：{a} -> {b}")
    return dict(paper=paper, indicators=indicators, mentions=mentions,
                relations=relations)


def import_payload(driver, database, payload):
    with driver.session(database=database) as session:
        for label in ("Paper", "Indicator"):
            session.run(f"CREATE CONSTRAINT ie_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE").consume()
        return session.execute_write(_replace_paper, payload)


def _replace_paper(tx, payload):
    pid = payload["paper"]["id"]
    # Lock the Paper before replacing its owned nodes; competing imports serialize.
    record = tx.run("MERGE (p:Paper {id:$id}) ON CREATE SET p.importer=$owner "
                    "SET p.import_lock=coalesce(p.import_lock,0)+1 RETURN p.importer AS owner",
                    id=pid, owner=OWNER).single()
    if record["owner"] != OWNER:
        raise ValueError("相同 paper_id 已被其他来源占用，拒绝覆盖")
    # 同时清理该论文旧版 Assertion/Evidence 节点，其他论文及其他来源不受影响。
    tx.run("MATCH (n) WHERE n.paper_id=$id AND n.importer=$owner "
           "AND (n:Indicator OR n:Assertion OR n:Evidence) DETACH DELETE n", id=pid, owner=OWNER).consume()
    tx.run("MATCH (p:Paper {id:$id}) SET p += $props, p.imported_at=datetime()",
           id=pid, props=payload["paper"]).consume()
    tx.run("UNWIND $rows AS row CREATE (n:Indicator {id:row.id}) "
           "SET n += row.props, n.paper_id=$id, n.importer=$owner",
           rows=payload["indicators"], id=pid, owner=OWNER).consume()
    tx.run("UNWIND $rows AS row MATCH (p:Paper {id:$id}), (i:Indicator {id:row.id}) "
           "CREATE (p)-[r:MENTIONS]->(i) SET r += row.props", rows=payload["mentions"], id=pid).consume()
    # 类型只取固定白名单，所有数据通过参数传入，不拼接模型输出到 Cypher。
    for kind, label, predicates in (("PI", "Paper", PI_TYPES), ("II", "Indicator", II_TYPES)):
        for predicate in predicates:
            rows = [r for r in payload["relations"] if r["kind"] == kind and r["predicate"] == predicate]
            if rows:
                tx.run(f"UNWIND $rows AS row MATCH (s:{label} {{id:row.subject}}), "
                       f"(o:Indicator {{id:row.object}}) CREATE (s)-[r:{predicate}]->(o) "
                       "SET r += row.props", rows=rows).consume()
    return payload_summary(payload)


def payload_summary(payload):
    return {"paper_id": payload["paper"]["id"], "papers": 1,
            "indicators": len(payload["indicators"]), "mentions": len(payload["mentions"]),
            "pi_relations": sum(r["kind"] == "PI" for r in payload["relations"]),
            "ii_relations": sum(r["kind"] == "II" for r in payload["relations"]),
            "no_relation": sum(r["props"].get("no_relation_reason") is not None for r in payload["mentions"]),
            # 统计各属性数组内的证据条目，跨关系重复引用分别计数，并非证据节点数。
            "evidence_entries": sum(r["props"].get("evidence_count", 0)
                                    for key in ("indicators", "mentions", "relations") for r in payload[key])}


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True, help="IE 流程输出的result.json")
    parser.add_argument("--source", type=Path, required=True, help="抽取时使用的 Markdown")
    parser.add_argument("--paper-id", help="稳定论文 ID；默认使用 Markdown SHA256")
    parser.add_argument("--title", help="默认读取 Markdown 一级标题")
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--dry-run", action="store_true", help="仅校验与转换，不连接数据库")
    parser.add_argument("--payload-output", type=Path, help="可选保存转换后的完整 JSON")
    args = parser.parse_args()
    try:
        # 将 result.json 和原始 Markdown 转换成一个适合写入 Neo4j 的统一数据结构。
        payload = build_payload(args.result, args.source, args.paper_id, args.title)
        # 如果指定了 --payload-output，则保存转换后的 JSON 以便调试或复用。
        if args.payload_output:
            args.payload_output.parent.mkdir(parents=True, exist_ok=True)
            args.payload_output.write_text(encode(payload), encoding="utf-8")
        # 正式导入之前检查数据
        if args.dry_run:
            print(encode({"mode": "dry_run", **payload_summary(payload)}))
            return
        # 导入数据
        from neo4j import GraphDatabase
        uri, user, password = (os.getenv(k) for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"))
        if not all((uri, user, password)):
            raise ValueError("请在 .env 配置 NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD")
        with GraphDatabase.driver(uri, auth=(user, password)) as driver:
            driver.verify_connectivity()
            print(encode(import_payload(driver, args.database, payload)))
    except (ValueError, KeyError, TypeError, OSError, Neo4jError, DriverError) as error:
        parser.exit(1, f"导入失败：{error}\n")


if __name__ == "__main__":
    main()
