# Download portable ffmpeg essentials into tools\ffmpeg\bin
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }

$ToolsDir = Join-Path $Root "tools"
$FfmpegDir = Join-Path $ToolsDir "ffmpeg"
$BinDir = Join-Path $FfmpegDir "bin"
$Marker = Join-Path $BinDir "ffmpeg.exe"

if (Test-Path $Marker) {
    Write-Host "[ok] ffmpeg already present: $Marker"
    exit 0
}

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
$ZipPath = Join-Path $ToolsDir "ffmpeg-essentials.zip"
$ExtractDir = Join-Path $ToolsDir "ffmpeg-extract"

# Prefer pinned GitHub release with known SHA256 (from winget manifest).
# Rolling gyan.dev URL is last-resort and integrity-unverified.
$Sources = @(
    [pscustomobject]@{
        Url = "https://github.com/GyanD/codexffmpeg/releases/download/8.0.1/ffmpeg-8.0.1-essentials_build.zip"
        Sha256 = "E2AAEAA0FDBC397D4794828086424D4AAA2102CEF1FB6874F6FFD29C0B88B673"
    },
    [pscustomobject]@{
        Url = "https://github.com/GyanD/codexffmpeg/releases/download/8.0/ffmpeg-8.0-essentials_build.zip"
        Sha256 = "647E467CAF82B9FA200A562769B5FF4D736AAF725804ED2C64EA9752106FA569"
    },
    [pscustomobject]@{
        Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        Sha256 = $null
    }
)

function Test-Sha256([string]$Path, [string]$Expected) {
    $actual = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    $want = $Expected.ToUpperInvariant()
    if ($actual -ne $want) {
        throw "SHA256 mismatch for $Path`n  expected: $want`n  actual:   $actual"
    }
}

Write-Host "[info] downloading portable ffmpeg ..."
$downloaded = $false
$used = $null
foreach ($src in $Sources) {
    try {
        Write-Host "[info] try $($src.Url)"
        Invoke-WebRequest -Uri $src.Url -OutFile $ZipPath -UseBasicParsing
        $size = (Get-Item $ZipPath).Length
        if ($size -lt 1MB) {
            throw "downloaded file too small ($size bytes); likely not a zip package"
        }
        if ($src.Sha256) {
            Test-Sha256 -Path $ZipPath -Expected $src.Sha256
            Write-Host "[ok] sha256 verified"
        } else {
            Write-Host "[warn] no pinned SHA256 for this URL; skipped integrity check"
            Write-Host "       prefer verifying the zip yourself before use on untrusted networks"
        }
        $downloaded = $true
        $used = $src
        break
    } catch {
        Write-Host "[warn] download/verify failed: $($_.Exception.Message)"
        if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue }
    }
}

if (-not $downloaded) {
    Write-Error "Failed to download ffmpeg. Install manually and put ffmpeg.exe/ffprobe.exe into tools\ffmpeg\bin\"
    exit 1
}

if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

$foundBin = Get-ChildItem -Path $ExtractDir -Recurse -Directory -Filter "bin" |
    Where-Object { Test-Path (Join-Path $_.FullName "ffmpeg.exe") } |
    Select-Object -First 1

if (-not $foundBin) {
    Write-Error "ffmpeg.exe not found inside downloaded zip"
    exit 1
}

if (Test-Path $FfmpegDir) { Remove-Item -Recurse -Force $FfmpegDir }
New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
Copy-Item -Path $foundBin.FullName -Destination $BinDir -Recurse -Force

Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ExtractDir -ErrorAction SilentlyContinue

if (-not (Test-Path $Marker)) {
    Write-Error "ffmpeg install failed"
    exit 1
}

Write-Host "[ok] ffmpeg installed to $BinDir (source=$($used.Url))"
& $Marker -version | Select-Object -First 1
