"""MP3/LRC 只读导入。来源字段和未知属性保留，避免文件名猜测音乐事实。"""
import hashlib
import re
from pathlib import Path
from mutagen.mp3 import MP3

from .store import Store

TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str):
    lines, timed = [], []
    for line in text.splitlines():
        stamps = TIMESTAMP.findall(line)
        clean = re.sub(r"\[[^\]]*\]", "", line).strip()
        if not clean:
            continue
        # 原文和译文可以共享时间戳，保留列表而非用字典覆盖。
        for minute, sec, frac in stamps:
            timed.append({"seconds": int(minute) * 60 + int(sec) + float("0." + (frac or "0")), "text": clean})
        if clean not in lines:
            lines.append(clean)
    placeholder = len(lines) <= 3 and any(re.search(r"纯音乐|instrumental", x, re.I) for x in lines)
    return {"text": "\n".join(lines) if not placeholder else "", "timed": timed,
            "status": "instrumental_hint" if placeholder else ("text" if lines else "empty")}


def import_catalog(store: Store, music_dir: Path, lyrics_dir: Path):
    if not music_dir.is_dir():
        raise ValueError(f"曲库目录不存在：{music_dir}")
    files = sorted(music_dir.rglob("*.mp3"))
    seen, errors = set(), []
    for path in files:
        try:
            audio = MP3(path)
            tags = audio.tags

            def tag(name):
                return str(tags[name]) if tags and name in tags else None

            resolved = str(path.resolve())
            track_id = hashlib.sha256(resolved.casefold().encode()).hexdigest()[:16]
            stat = path.stat()
            # 文件内容变化由大小/纳秒时间识别；歌词内容另纳入指纹使文本缓存失效。
            lrc = lyrics_dir / (path.stem + ".lrc")
            lyrics = {"text": "", "timed": [], "status": "missing"}
            if lrc.exists():
                raw = lrc.read_bytes()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("gb18030")
                lyrics = parse_lrc(text)
                lyrics["source"] = str(lrc.resolve())
            duration = float(audio.info.length)
            fingerprint = hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}:{lyrics}".encode()).hexdigest()
            store.put_track({"id": track_id, "path": resolved, "fingerprint": fingerprint,
                             "title": tag("TIT2") or path.stem, "artist": tag("TPE1") or "未知艺人",
                             "album": tag("TALB"), "language": tag("TLAN"), "duration": duration,
                             "language_evidence": {"source": "ID3 TLAN", "method": "metadata", "version": "v1"}
                             if tag("TLAN") else {"source": None, "method": "unknown", "version": "v1"},
                             "title_source": "ID3" if tag("TIT2") else "filename",
                             "vocal": None, "lyrics": lyrics,
                             "unusual_duration": duration < 30 or duration > 900})
            seen.add(track_id)
        except Exception as exc:
            errors.append({"file": path.name, "error": type(exc).__name__})
    # 扫描后标记失效资源；特征表保留历史而不参与检索。
    with store.connect() as db:
        rows = db.execute("SELECT id,path FROM tracks").fetchall()
        for row in rows:
            if Path(row["path"]).is_relative_to(music_dir.resolve()) and row["id"] not in seen:
                db.execute("UPDATE tracks SET available=0 WHERE id=?", (row["id"],))
    report = {"scanned": len(files), "imported": len(seen), "errors": errors}
    store.event("catalog_import", report)
    return report
