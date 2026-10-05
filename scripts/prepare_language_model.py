"""显式下载固定版本的开源语言检测权重；不访问收费API。"""
from huggingface_hub import snapshot_download
from lify.config import Settings


def main():
    settings = Settings.load()
    folder = settings.data_dir/'models'/'whisper-small'
    snapshot_download('Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120',
                      local_dir=folder, allow_patterns=['model.bin','config.json','tokenizer.json','vocabulary.*'])
    print(folder)


if __name__ == '__main__':
    main()
