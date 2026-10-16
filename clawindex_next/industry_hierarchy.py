"""Resolve SW2021 parent links using exact directory identifiers, never name guesses."""


def enrich_rows(rows, directory):
    parents, by_code = {}, {item['code']:item for item in directory}
    for item in directory:
        metadata = item['metadata']
        if metadata.get('level') != 'L1' or metadata.get('source_version') not in (None,'SW2021') or not metadata.get('identity_verified'):
            continue
        parent = {'code':item['code'], 'name':item['name']}
        for key in (item['code'], metadata.get('industry_code')):
            if not isinstance(key,str) or not key.strip():
                continue
            key = key.strip()
            if key in parents and parents[key] != parent:
                parents[key] = None # Ambiguous directory identifiers must not guess a parent.
            else:
                parents[key] = parent
    result = []
    for row in rows:
        if 'parent_index' in row:
            result.append(dict(row)) # Preserve the hierarchy captured with this evidence version.
            continue
        item = by_code.get(row['code'], {})
        raw_parent = row.get('parent') or item.get('metadata',{}).get('parent_code')
        parent = parents.get(raw_parent.strip()) if row.get('level') == 'L2' and isinstance(raw_parent,str) else None
        result.append({**row, 'parent_index':parent})
    return result


def parent_label(row):
    if row.get('level') != 'L2':
        return ''
    parent = row.get('parent_index')
    return '所属一级行业：'+(parent['name']+'（'+parent['code']+'）' if parent else '目录暂未确认，后台更新后补充')
