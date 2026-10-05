$ErrorActionPreference = 'Stop'
# 从项目根目录运行；固定已验证第一方构建，验证SHA256后再解包。
$projectRoot = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $projectRoot '.lify/tools/mpv'
$archive = Join-Path $projectRoot '.lify/tools/mpv.zip'
$url = 'https://github.com/mpv-player/mpv/releases/download/git-release/mpv-v0.41.0-dev-g413ff0b1c-37085543729-x86_64-w64-mingw32-full.zip'
$expected = '3E492E47BFEA0761924B7C6CC5509320FDB73A26F6C9E5590BAC13DD6CD53997'
New-Item -ItemType Directory -Force $destination | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
    throw '下载文件校验不一致，未执行或解包。请重新核验mpv官方构建。'
}
Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
Write-Host ('mpv 已安装：' + (Join-Path $destination 'mpv.exe'))
