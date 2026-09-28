"""固定参数化只读查询；不接受模型生成的 Cypher。"""

import json
from neo4j import GraphDatabase, READ_ACCESS, Query

LIMIT = 40


class GraphStore:
    def __init__(self, uri, user, password, database):
        self.driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=5,
            connection_acquisition_timeout=8,
            max_transaction_retry_time=0,
        )
        self.database = database

    def close(self):
        self.driver.close()

    def read(self, query, **params):
        with self.driver.session(
            database=self.database, default_access_mode=READ_ACCESS
        ) as session:
            return session.run(Query(query, timeout=10), **params).data()

    # 获取可选论文列表
    def papers(self):
        return self.read(
            "MATCH (p:Paper {importer:'ie_import'}) "
            "RETURN p.id AS id,p.title AS title ORDER BY id LIMIT 201"
        )

    # 获取一篇论文及其指标节点列表
    def catalog(self, paper_id):
        paper = self.read(
            "MATCH (p:Paper {id:$paper,importer:'ie_import'}) "
            "RETURN p.id AS id,p.title AS title,p.status AS status",
            paper=paper_id,
        )
        if not paper:
            raise LookupError("所选论文不存在，请刷新论文列表")
        nodes = self.read(
            "MATCH (i:Indicator {paper_id:$paper,importer:'ie_import'}) "
            "RETURN i.id AS id,i.name AS name,i.aliases AS aliases ORDER BY id LIMIT 501",
            paper=paper_id,
        )
        return paper[0], nodes[:500], len(nodes) > 500

    # 根据模型选择的工具及参数，执行对应的图谱只读查询
    def retrieve(self, call, paper_id):
        params = dict(
            paper=paper_id,
            ids=call.indicator_ids,
            predicates=call.predicates,
            limit=LIMIT + 1,
        )
        # 查询指标本身的信息。返回指标名称、定义、发现该指标的原文或视觉证据
        if call.tool == "indicator_details":
            query = """MATCH (i:Indicator {paper_id:$paper,importer:'ie_import'})
            WHERE size($ids)=0 OR i.id IN $ids
            RETURN i.id AS id, 'indicator' AS kind, i.name AS name,
                   i.definitions AS definitions, i.evidence_json AS evidence_json
            ORDER BY id LIMIT $limit"""
        # 查询论文与指标、指标与指标之间的关系。返回关系两端、关系类型、明确／推断标记、判断说明和证据
        elif call.tool == "no_relation":
            query = """MATCH (p:Paper {id:$paper})-[r:MENTIONS]->(i:Indicator)
            WHERE r.importer='ie_import' AND r.no_relation_reason IS NOT NULL
              AND (size($ids)=0 OR i.id IN $ids)
            RETURN r.relation_id AS id,'no_relation' AS kind,i.name AS name,
                   r.no_relation_reason AS reason,r.no_relation_detail AS detail,
                   r.evidence_json AS evidence_json
            ORDER BY id LIMIT $limit"""
        # 查询已发现指标为何没有有效的论文与指标的关系。返回指标名称、无关系原因、详细说明和证据
        else:
            query = """MATCH (s)-[r]->(o:Indicator)
            WHERE r.paper_id=$paper AND r.importer='ie_import'
              AND type(r) IN ['PROPOSES','MODIFIES','APPLIES','VARIANT_OF','DERIVED_FROM',
                              'IMPROVES_ON','ALTERNATIVE_TO','COMPONENT_OF']
              AND (size($ids)=0 OR s.id IN $ids OR o.id IN $ids)
              AND (size($predicates)=0 OR type(r) IN $predicates)
            RETURN r.relation_id AS id,'relation' AS kind,
                   coalesce(s.name,s.title) AS subject,o.name AS object,type(r) AS predicate,
                   r.assertion_mode AS assertion_mode,r.rationale_summary AS rationale_summary,
                   r.status AS status,r.evidence_json AS evidence_json
            ORDER BY id LIMIT $limit"""
        rows = self.read(query, **params)
        for row in rows[:LIMIT]:
            try:
                row["evidence"] = json.loads(row.pop("evidence_json") or "[]")
            except (ValueError, TypeError):
                raise ValueError("图谱 evidence_json 损坏，请重新导入该论文") from None
            if not isinstance(row["evidence"], list) or any(
                not isinstance(e, dict) for e in row["evidence"]
            ):
                raise ValueError("图谱证据结构错误，请重新导入该论文")
        return rows[:LIMIT], len(rows) > LIMIT
