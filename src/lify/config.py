"""配置只在启动时读取；密钥永不进入日志和检查点。"""
from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import dotenv_values


@dataclass
class Settings:
    data_dir: Path = Path(".lify")
    music_dir: Path = Path(r"G:\CloudMusic\music")
    lyrics_dir: Path = Path(r"G:\CloudMusic\Lyrics")
    llm_model: str = "deepseek-flash"
    llm_url: str = "https://api.deepseek.com"
    llm_key: str = field(default="", repr=False)
    embedding_model: str = "embedding-3"
    embedding_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_key: str = field(default="", repr=False)
    dimensions: int = 1024
    batch_size: int = 32
    budget: float = 30.0
    # 单价未知保持 None，防止用旧价格启动有费用的调用。
    llm_input_price: float | None = None
    llm_output_price: float | None = None
    llm_price_model: str = 'deepseek-flash'
    embedding_price_model: str = 'embedding-3'
    embedding_price: float = 0.5
    audio_model: str = "laion/clap-htsat-unfused"
    audio_revision: str = '8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a'
    mpv_path: str = "mpv"

    @classmethod
    def load(cls, root: Path = Path(".")) -> "Settings":
        # 模板提供默认值，.env 和进程环境依次覆盖。示例文件不含真实密钥。
        values = {**dotenv_values(root / ".env.example"), **dotenv_values(root / ".env"), **os.environ}

        def get(key, default=""):
            return values.get(key) or default

        def price(key):
            return float(values[key]) if values.get(key) else None

        return cls(
            data_dir=Path(get("LIFY_DATA_DIR", str(root / ".lify"))).resolve(),
            music_dir=Path(get("MUSIC_DIR", r"G:\CloudMusic\music")),
            lyrics_dir=Path(get("LYRICS_DIR", r"G:\CloudMusic\Lyrics")),
            llm_model=get("LLM_MODEL", "deepseek-flash"), llm_url=get("LLM_BASE_URL", "https://api.deepseek.com"),
            llm_key=get("LLM_API_KEY"), embedding_model=get("EMBEDDING_MODEL", "embedding-3"),
            embedding_url=get("EMBEDDING_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            embedding_key=get("EMBEDDING_API_KEY"), dimensions=int(get("EMBEDDING_DIMENSIONS", 1024)),
            batch_size=min(64, max(1, int(get("EMBEDDING_BATCH_SIZE", 32)))),
            budget=min(30.0, max(0.0, float(get("API_BUDGET_CNY", 30)))),
            llm_input_price=price("LLM_INPUT_CNY_PER_MILLION"),
            llm_output_price=price("LLM_OUTPUT_CNY_PER_MILLION"),
            llm_price_model=get('LLM_PRICE_MODEL', 'deepseek-flash'),
            embedding_price_model=get('EMBEDDING_PRICE_MODEL', 'embedding-3'),
            embedding_price=float(get("EMBEDDING_CNY_PER_MILLION", 0.5)),
            audio_model=get("AUDIO_MODEL", "laion/clap-htsat-unfused"),
            audio_revision=get('AUDIO_REVISION', '8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a'),
            mpv_path=get("MPV_PATH", str((root / '.lify/tools/mpv/mpv.exe').resolve())
                         if (root / '.lify/tools/mpv/mpv.exe').is_file() else 'mpv'),
        )
