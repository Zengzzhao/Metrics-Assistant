"""归并计划的结构、引用及名称来源检查。语义判断由模型完成。"""

# 每个候选有且仅有一次出现；不能凭空增加候选。
def validate_merge_plan(groups, candidates, *, review=False):
    assigned = [cid for group in groups for cid in group['candidate_ids']]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(candidates):
        raise ValueError('归并结果必须恰好覆盖本次候选一次，不得遗漏、重复或新增')
    for group in groups:
        ids = set(group['candidate_ids'])
        # 检查归并后的名称是否合法：canonical_name 和 aliases 必须来自组内候选已有的 name 或 aliases，不能让模型自己编新名字
        names = {name for cid in ids for name in [candidates[cid]['name'], *candidates[cid]['aliases']]}
        if not group['canonical_name'].strip() or any(
            name not in names for name in [group['canonical_name'], *group['aliases']]
        ):
            raise ValueError(f'归并名称必须来自组内候选名称或别名：{group["candidate_ids"]}')
        # 确保引用的 candidate_id 属于当前组，evidence_index 也确实存在。
        cited = set()
        for ref in group['evidence_refs']:
            cid, index = ref['candidate_id'], ref['evidence_index']
            if cid not in ids or not 0 <= index < len(candidates[cid]['evidence']):
                raise ValueError(f'归并证据引用越界或不属于本组：{ref}')
            cited.add(cid)
        # 检查证据是否覆盖所有候选：当前组中的每个候选都必须至少有一条证据被引用，不能只拿部分候选的证据就把整组都合并。
        if cited != ids:
            raise ValueError(f'归并依据未覆盖全部候选：{sorted(ids - cited)}')
        # 复核输出最终分组；不足以合并的候选单独保留，也使用 confirmed。
        if review:
            if group['status'] != 'confirmed' or not group['context_evidence']:
                raise ValueError('复核分组必须为 confirmed，并提供补充原文证据')
        elif group['context_evidence']:
            raise ValueError('首轮不能引用尚未提供的补充原文')
