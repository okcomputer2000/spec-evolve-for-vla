param([switch]$DownloadOnly, [switch]$DirectDownload)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = Join-Path $projectRoot 'models\ollama'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
$env:OLLAMA_FLASH_ATTENTION = '1'
if ($DownloadOnly) {
    $env:CUDA_VISIBLE_DEVICES = '-1'
    $env:OLLAMA_VULKAN = '0'
}
if ($DirectDownload) {
    $env:HTTP_PROXY = ''
    $env:HTTPS_PROXY = ''
    $env:ALL_PROXY = ''
    $env:NO_PROXY = '*'
}
& (Join-Path $projectRoot 'tools\ollama\ollama.exe') serve
