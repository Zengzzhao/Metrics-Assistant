"""固定参数化只读查询；不接受模型生成的 Cypher。"""

import json
from neo4j import GraphDatabase, READ_ACCESS, Query

from ie.models import Evidence

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
            "RETURN i.id AS id,i.name AS name,i.aliases AS aliases ORDER BY id",
            paper=paper_id,
        )
        return paper[0], nodes

    # 根据模型选择的工具及参数，执行对应的图谱只读查询
    def retrieve(self, call, paper_id):
        params = dict(
            paper=paper_id,
            ids=call.indicator_ids,
            predicates=call.predicates,
        )
        # 查询指标详细信息。返回指标名称、定义、发现该指标的原文或视觉证据
        if call.tool == "indicator_details":
            query = """MATCH (i:Indicator {paper_id:$paper,importer:'ie_import'})
            WHERE size($ids)=0 OR i.id IN $ids
            RETURN i.id AS id, 'indicator' AS kind, i.name AS name,
                   i.definitions AS definitions, i.evidence_json AS evidence_json,
                   i.evidence_quotes AS evidence_quotes,
                   i.evidence_observations AS evidence_observations
            ORDER BY id"""
        # 查询无有效关系的原因。
        elif call.tool == "no_relation":
            query = """MATCH (p:Paper {id:$paper})-[r:MENTIONS]->(i:Indicator)
            WHERE r.importer='ie_import' AND r.no_relation_reason IS NOT NULL
              AND (size($ids)=0 OR i.id IN $ids)
            RETURN r.relation_id AS id,'no_relation' AS kind,i.name AS name,
                   r.no_relation_reason AS reason,r.no_relation_detail AS detail,
                   r.evidence_json AS evidence_json,
                   r.evidence_quotes AS evidence_quotes,
                   r.evidence_observations AS evidence_observations
            ORDER BY id"""
        # 查询论文与指标、指标与指标之间的有效语义关系。
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
                   r.status AS status,r.evidence_json AS evidence_json,
                   r.evidence_quotes AS evidence_quotes,
                   r.evidence_observations AS evidence_observations
            ORDER BY id"""
        rows = self.read(query, **params)
        for row in rows:
            quotes = row.pop("evidence_quotes", None)
            observations = row.pop("evidence_observations", None)
            evidence_json = row.pop("evidence_json", None)
            if observations is not None:
                if (not isinstance(quotes, list) or not isinstance(observations, list)
                        or len(quotes) != len(observations)
                        or any(not isinstance(v, str) for v in quotes + observations)):
                    raise ValueError("图谱证据数组不匹配，请重新导入该论文")
                # 抽取约定：文字 observation 为空，视觉 observation 为观察描述。
                row["evidence"] = [
                    {"kind": "visual" if observation else "text",
                     "quote": quote, "observation": observation}
                    for quote, observation in zip(quotes, observations)
                ]
            else:
                # 兼容旧存储格式，但与新格式执行相同的证据校验。
                try:
                    row["evidence"] = json.loads(evidence_json or "[]")
                except (ValueError, TypeError):
                    raise ValueError("图谱 evidence_json 损坏，请重新导入该论文") from None
            evidence = row["evidence"]
            if not isinstance(evidence, list) or not evidence:
                raise ValueError(f"图谱记录 {row['id']} 缺少证据，请重新抽取并导入该论文")
            try:
                for item in evidence:
                    if not isinstance(item, dict):
                        raise ValueError("证据必须是对象")
                    # 旧格式可带定位信息；证据内容统一遵循抽取阶段的 schema。
                    Evidence.model_validate({k: v for k, v in item.items() if k in Evidence.model_fields})
            except ValueError:
                raise ValueError(f"图谱记录 {row['id']} 的证据无效，请重新抽取并导入该论文") from None
        return rows
