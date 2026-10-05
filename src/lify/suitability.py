"""题材禁止条件的保守审核：缺证据的候选不凑数，审核只作用于本轮。"""


def audit_topics(candidates, forbidden, providers):
    if not forbidden:
        return candidates, {'required': False}
    allowed, decisions = [], []
    # 有限审核池控制费用。不能把池外歌曲或未知审核视为已通过。
    for start in range(0, min(40, len(candidates)), 10):
        batch = candidates[start:start + 10]
        evidence = []
        for item in batch:
            track = item['track']
            lyrics = track.get('lyrics', {}).get('text', '')
            evidence.append({'id': track['id'], 'lyrics': lyrics[:16000],
                             'complete_lyrics': bool(lyrics) and len(lyrics) <= 16000,
                             'instrumental_verified': track.get('vocal') == 'instrumental'})
        response = providers.check_topics(forbidden, evidence)
        rows = response.get('decisions', [])
        mapping = {row.get('id'): row for row in rows if isinstance(row, dict)}
        for item, source in zip(batch, evidence):
            row = mapping.get(source['id'], {})
            verdict, quote = row.get('verdict', 'unknown'), row.get('quote', '')
            verified = (source['instrumental_verified'] or
                        source['complete_lyrics'] and isinstance(quote, str) and bool(quote.strip()) and
                        quote in source['lyrics'])
            accepted = verdict == 'allowed' and verified
            # 器乐核验只能证明无需歌词审核，不能为模型随附的歌词引用背书。
            if source['instrumental_verified']:
                quote = ''
            decisions.append({'id': source['id'], 'verdict': verdict if verified else 'unknown',
                              'accepted': accepted, 'reason': row.get('reason', ''), 'quote': quote if verified else ''})
            if accepted:
                evidence_source = (item['track'].get('lyrics', {}).get('source') or
                                   item['track'].get('language_evidence', {}).get('source') or
                                   item['track'].get('path'))
                if source['instrumental_verified']:
                    evidence_source = item['track'].get('language_evidence', {}).get('source')
                    evidence_source = evidence_source or item['track'].get('path')
                item = {**item, 'evidence': {**item['evidence'], 'topic_audit': {
                    'source': evidence_source, 'query': '；'.join(forbidden),
                    'similarity': 1., 'version': 'llm-topic-audit-v1', 'quote': quote,
                    'limitation': ('基于已核验器乐类型，仅说明没有演唱歌词，不证明文化或使用背景题材'
                                   if source['instrumental_verified'] else
                                   '基于已提供歌词的模型审核，不是已校准题材分类器')}}}
                allowed.append(item)
    return allowed, {'required': True, 'audited': len(decisions), 'accepted': len(allowed),
                     'decisions': decisions, 'limitation': '只审核前40候选，缺歌词或证据不足不补入'}
