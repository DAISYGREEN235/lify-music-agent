"""受控音频标签词表；描述词及词表变更会使标签缓存失效。"""

# namespace 区分维度，code 是稳定程序值，label 是给用户看的中文。
# 这些都是听感候选描述，不包含语言、歌词题材或个人场景事实。
AUDIO_DESCRIPTORS = (
    ('mood', 'calm', '平静', 'Calm gentle relaxing music.'),
    ('mood', 'uplifting', '振奋', 'Uplifting cheerful optimistic music.'),
    ('mood', 'sad', '悲伤', 'Sad melancholic music.'),
    ('mood', 'aggressive', '激烈', 'Loud aggressive intense music.'),
    ('vocal', 'vocal', '人声', 'A song with clear singing vocals.'),
    ('vocal', 'instrumental', '器乐', 'Instrumental music without singing.'),
    ('genre', 'pop', '流行', 'Pop music.'),
    ('genre', 'rock', '摇滚', 'Rock music with electric guitars and drums.'),
    ('genre', 'electronic', '电子', 'Electronic dance music with synthesizers.'),
    ('genre', 'classical', '古典', 'Classical orchestral music.'),
    ('instrument', 'piano', '钢琴', 'Music featuring piano.'),
    ('instrument', 'guitar', '吉他', 'Music featuring guitar.'),
    ('instrument', 'strings', '弦乐', 'Music featuring string instruments.'),
    ('instrument', 'synth', '合成器', 'Music featuring synthesizers.'),
    ('texture', 'acoustic', '原声感', 'Acoustic music with natural instruments.'),
    ('texture', 'electronic', '电子感', 'Music with electronic synthesized sounds.'),
)
