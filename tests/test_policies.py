from lify.policies import retrieve_policies


def test_policy_search_returns_original_source_and_detects_changes(tmp_path):
    file = tmp_path / 'tag_taxonomy.md'
    file.write_text('# 语言标签\n未知不能归入英文。', encoding='utf-8')
    first = retrieve_policies('英文歌曲', tmp_path)
    assert len(first['documents']) == 1
    doc = first['documents'][0]
    assert doc['source'] == 'docs/policies/tag_taxonomy.md'
    assert doc['content'] == file.read_text(encoding='utf-8')
    file.write_text('# 语言标签\n人工试听优先。', encoding='utf-8')
    assert retrieve_policies('语言', tmp_path)['documents'][0]['content_hash'] != doc['content_hash']


def test_policy_unknown_query_and_path_cannot_escape_whitelist(tmp_path):
    (tmp_path / 'secret.md').write_text('private', encoding='utf-8')
    result = retrieve_policies('../secret.md', tmp_path)
    assert result['documents'] == []
    assert len(result['missing_documents']) == 3


def test_repository_policies_are_available():
    result = retrieve_policies()
    assert len(result['documents']) == 3
    assert result['missing_documents'] == []
