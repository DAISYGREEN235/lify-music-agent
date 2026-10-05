"""本地策略检索：只读受控 Markdown，返回原文及内容版本，不执行文档指令。"""
import hashlib
import re
from pathlib import Path


DOCUMENTS = {
    'tag_taxonomy': ('tag_taxonomy.md', ('标签', '语言', '人声', '器乐', '英文', '日文')),
    'recommendation_policy': ('recommendation_policy.md', ('推荐', '数量', '时长', '重复', '反馈', '场景')),
    'explanation_policy': ('explanation_policy.md', ('解释', '来源', '证据', '知识卡', '引用')),
}


def retrieve_policies(query='', root=None):
    """关键词匹配三份白名单文档；空查询列出全部，不命中则明确返回空。

    root 可注入临时文档目录用于测试；Web 用户只能传 query，不能传路径。
    content_hash 绑定本次读取的内容，便于追踪后续文档变更。
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2] / 'docs' / 'policies'
    terms = re.findall(r'[\w]+', query.casefold())
    matches, missing = [], []
    for document_id, (name, aliases) in DOCUMENTS.items():
        path = root / name
        if not path.is_file():
            missing.append(document_id)
            continue
        content = path.read_text(encoding='utf-8')
        score = sum(alias in query for alias in aliases) + sum(term in content.casefold() for term in terms)
        if query.strip() and not score:
            continue
        matches.append({'document_id': document_id, 'source': 'docs/policies/' + name,
                        'title': content.splitlines()[0].lstrip('# ').strip(),
                        'content_hash': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                        'content': content, 'score': score})
    matches.sort(key=lambda item: (-item['score'], item['document_id']))
    return {'query': query, 'documents': matches, 'missing_documents': missing,
            'method': 'controlled-keyword-v1',
            'limitation': '策略原文关键词检索；不代表歌曲具备这些标签，也不自动改变推荐规则'}
