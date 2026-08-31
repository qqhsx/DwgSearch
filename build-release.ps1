<#>
.SYNOPSIS
    DWG Search Tool - 自动化构建发布脚本
    用法：在 PowerShell 中以管理员身份运行
    .\build-release.ps1 [-Version "2.19.0"] [-UploadRelease] [-GitHubToken "ghp_xxx"]

.DESCRIPTION
    1. 清理旧构建产物
    2. 使用 PyInstaller 打包（单文件夹模式 + 单文件模式）
    3. 生成便携版 ZIP
    4. 生成 SHA256SUMS.txt
    5. 可选：创建 GitHub Release 并上传资产

.NOTES
    需要预装：Python 3.8+、PyInstaller、GitHub CLI (gh)、7-Zip (可选，用于压缩)
    .NET 子程序需预先编译（DwgTextExtractor、DwgTextReplacer 目录下已有 Release 构建产物）
</#>

param(
    [string]$Version = "2.19.0",
    [switch]$UploadRelease,
    [string]$GitHubToken = "",
    [string]$RepoOwner = "qqhsx",
    [string]$RepoName = "dwg-search",
    [switch]$SkipBuild
)

# ─────────────────────────────────────────────────────────────────────────────
# 配置区域
# ─────────────────────────────────────────────────────────────────────────────
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$DistDir = Join-Path $ProjectRoot "dist"
$BuildDir = Join-Path $ProjectRoot "build"
$ReleaseDir = Join-Path $ProjectRoot "release_dist"
$VenVPython = Join-Path $ProjectRoot "venv38\Scripts\python.exe"
$PyInstaller = Join-Path $ProjectRoot "venv38\Scripts\pyinstaller.exe"
$SevenZip = "7z"  # 需在 PATH 中，或改为完整路径 "C:\Program Files\7-Zip\7z.exe"

# 版本标签（Git tag 格式）
$TagName = "v$Version"
$ReleaseTitle = "DWG Search Tool V$Version"

# 产物文件名
$FolderModeExeName = "DwgSearchApp"           # build.spec 输出文件夹名
$OneFileExeName = "DWG_Search_Tool_V6.6"      # build_onefile.spec 输出 exe 名
$SetupZipName = "DWG_Search_Setup_x64_$Version.zip"
$PortableZipName = "DWG_Search_Portable_x64_$Version.zip"
$Sha256FileName = "SHA256SUMS.txt"

# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "HH:mm:ss"
    $color = switch ($Level) { "ERROR" { "Red" } "WARN" { "Yellow" } "OK" { "Green" } default { "Cyan" } }
    Write-Host "[$timestamp] [$Level] $Message" -ForegroundColor $color
}

function Check-Command {
    param([string]$Name, [string]$Path = "")
    $cmd = if ($Path) { $Path } else { $Name }
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        Write-Log "✔ 找到 $Name: $(Get-Command $cmd).Source" "OK"
        return $true
    } else {
        Write-Log "✘ 缺少 $Name ($cmd)，请先安装并加入 PATH" "ERROR"
        return $false
    }
}

function Invoke-AndCheck {
    param([scriptblock]$Action, [string]$Desc)
    Write-Log "▶ $Desc..."
    try {
        & $Action
        if ($LASTEXITCODE -ne 0) { throw "退出码 $LASTEXITCODE" }
        Write-Log "✔ $Desc 完成" "OK"
        return $true
    } catch {
        Write-Log "✘ $Desc 失败：$_" "ERROR"
        return $false
    }
}

function Get-Sha256 {
    param([string]$FilePath)
    $hash = Get-FileHash -Algorithm SHA256 -Path $FilePath
    return $hash.Hash.ToLower()
}

# ─────────────────────────────────────────────────────────────────────────────
# 主流程开始
# ─────────────────────────────────────────────────────────────────────────────
Write-Log "=== DWG Search Tool 构建发布 v$Version ===" "INFO"
Write-Log "项目根目录：$ProjectRoot"

# 检查必要工具
$ok = $true
$ok = Check-Command "python" $VenVPython -and $ok
$ok = Check-Command "pyinstaller" $PyInstaller -and $ok
$ok = Check-Command "git" -and $ok
if ($UploadRelease) { $ok = Check-Command "gh" -and $ok }
$ok = Check-Command "7z" -and $ok  # 7-Zip 可选，没有则用 PowerShell 压缩
if (-not $ok) { exit 1 }

# 清理旧产物
if (-not $SkipBuild) {
    Write-Log "🧹 清理旧构建目录..."
    Remove-Item -Recurse -Force $DistDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $BuildDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $ReleaseDir -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. 编译 .NET 子程序（如有源码变更）
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Log "🔨 检查 .NET 子程序构建状态..."
    $netProjects = @(
        @{ Path = "DwgTextExtractor"; Config = "Release"; Framework = "net48" },
        @{ Path = "DwgTextReplacer"; Config = "Release"; Framework = "net48" }
    )
    foreach ($proj in $netProjects) {
        $projPath = Join-Path $ProjectRoot $proj.Path
        $csproj = Get-ChildItem $projPath -Filter "*.csproj" | Select-Object -First 1
        if ($csproj) {
            $exePath = Join-Path $projPath "bin\$($proj.Config)\$($proj.Framework)\$($csproj.BaseName).exe"
            if (-not (Test-Path $exePath)) {
                Write-Log "  编译 $($proj.Path)..." 
                $result = dotnet build $csproj.FullName -c $proj.Config -f $proj.Framework --no-restore
                if ($LASTEXITCODE -ne 0) { Write-Log "  ⚠ 编译失败，但继续尝试（可能已有预编译产物）" "WARN" }
            } else {
                Write-Log "  ✔ $($proj.Path) 已有构建产物" "OK"
            }
        }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. PyInstaller 打包：单文件夹模式（用于安装版/便携版基础文件夹）
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Log "📦 打包：单文件夹模式 (build.spec)..."
    $specFile = Join-Path $ProjectRoot "build.spec"
    $result = & $PyInstaller $specFile --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Log "单文件夹模式打包失败" "ERROR"; exit 1 }
    
    # 验证产物
    $folderModeDir = Join-Path $DistDir $FolderModeExeName
    $folderModeExe = Join-Path $folderModeDir "$FolderModeExeName.exe"
    if (-not (Test-Path $folderModeExe)) {
        Write-Log "未找到单文件夹模式产物：$folderModeExe" "ERROR"
        exit 1
    }
    Write-Log "  产物目录：$folderModeDir" "OK"
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. PyInstaller 打包：单文件模式（备用/极简分发）
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Log "📦 打包：单文件模式 (build_onefile.spec)..."
    $specFile = Join-Path $ProjectRoot "build_onefile.spec"
    $result = & $PyInstaller $specFile --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Log "单文件模式打包失败" "ERROR"; exit 1 }
    
    $oneFileExe = Join-Path $DistDir "$OneFileExeName.exe"
    if (-not (Test-Path $oneFileExe)) {
        Write-Log "未找到单文件模式产物：$oneFileExe" "ERROR"
        exit 1
    }
    Write-Log "  产物：$oneFileExe" "OK"
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. 生成发布包
# ─────────────────────────────────────────────────────────────────────────────
Write-Log "📦 生成发布包..."

# 4.1 便携版 = 单文件夹模式产物整个目录打包为 ZIP
$portableSourceDir = Join-Path $DistDir $FolderModeExeName
$portableZipPath = Join-Path $ReleaseDir $PortableZipName
Write-Log "  创建便携版：$PortableZipName"
if (Get-Command 7z -ErrorAction SilentlyContinue) {
    & 7z a -tzip -mx=9 "$portableZipPath" "$portableSourceDir\*" | Out-Null
} else {
    Compress-Archive -Path "$portableSourceDir\*" -DestinationPath $portableZipPath -Force
}
Write-Log "  ✔ 便携版大小：$(("{(0:N1} MB" -f ((Get-Item $portableZipPath).Length / 1MB)))" "OK"

# 4.2 安装版 = 单文件夹模式产物 + 简易安装脚本（这里直接用同一份文件夹打包，实际可替换为 Inno Setup）
# 为区分命名，这里复制一份并重命名内部 exe 为友好名称
$setupSourceDir = Join-Path $ReleaseDir "DWG_Search_Setup_x64"
if (Test-Path $setupSourceDir) { Remove-Item -Recurse -Force $setupSourceDir }
Copy-Item $portableSourceDir $setupSourceDir -Recurse
# 重命名 exe 为更友好的名字
Rename-Item (Join-Path $setupSourceDir "$FolderModeExeName.exe") "DWG_Search.exe" -Force
# 可选：添加卸载脚本、快捷方式创建脚本等
$setupZipPath = Join-Path $ReleaseDir $SetupZipName
Write-Log "  创建安装版：$SetupZipName"
if (Get-Command 7z -ErrorAction SilentlyContinue) {
    & 7z a -tzip -mx=9 "$setupZipPath" "$setupSourceDir\*" | Out-Null
} else {
    Compress-Archive -Path "$setupSourceDir\*" -DestinationPath $setupZipPath -Force
}
Write-Log "  ✔ 安装版大小：$(("{(0:N1} MB" -f ((Get-Item $setupZipPath).Length / 1MB)))" "OK"

# ─────────────────────────────────────────────────────────────────────────────
# 5. 生成 SHA256SUMS.txt
# ─────────────────────────────────────────────────────────────────────────────
Write-Log "🔐 生成 SHA256 校验和..."
$sha256Lines = @()
$filesToHash = @($setupZipPath, $portableZipPath)
foreach ($f in $filesToHash) {
    $hash = Get-Sha256 $f
    $name = Split-Path $f -Leaf
    $sha256Lines += "$hash  $name"
    Write-Log "  $name  $hash"
}
$sha256Path = Join-Path $ReleaseDir $Sha256FileName
$sha256Lines -join "`n" | Set-Content -Path $sha256Path -Encoding UTF8
Write-Log "  ✔ $Sha256FileName 已写入" "OK"

# ─────────────────────────────────────────────────────────────────────────────
# 6. 上传到 GitHub Release（可选）
# ─────────────────────────────────────────────────────────────────────────────
if ($UploadRelease) {
    Write-Log "☁️ 上传到 GitHub Release..."
    
    # 登录检查
    if ($GitHubToken) {
        $env:GH_TOKEN = $GitHubToken
    }
    
    # 检查是否已认证
    $authCheck = gh auth status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "GitHub CLI 未认证，请先运行：gh auth login" "ERROR"
        exit 1
    }
    
    # 创建或获取 Release
    Write-Log "  检查/创建 Release 标签：$TagName"
    $releaseExists = gh release view $TagName --repo "$RepoOwner/$RepoName" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "  创建新 Release..."
        $releaseUrl = gh release create $TagName `
            --repo "$RepoOwner/$RepoName" `
            --title "$ReleaseTitle" `
            --notes-file (Join-Path $ProjectRoot "CHANGELOG.md") `
            --generate-notes `
            2>&1
        if ($LASTEXITCODE -ne 0) { Write-Log "创建 Release 失败：$releaseUrl" "ERROR"; exit 1 }
    } else {
        Write-Log "  Release 已存在，将上传资产" "OK"
    }
    
    # 上传资产
    $assets = @($setupZipPath, $portableZipPath, $sha256Path)
    foreach ($asset in $assets) {
        $name = Split-Path $asset -Leaf
        Write-Log "  上传 $name ..."
        $result = gh release upload $TagName $asset --repo "$RepoOwner/$RepoName" --clobber 2>&1
        if ($LASTEXITCODE -ne 0) { Write-Log "上传 $name 失败：$result" "ERROR"; exit 1 }
    }
    
    Write-Log "✅ Release 发布完成：https://github.com/$RepoOwner/$RepoName/releases/tag/$TagName" "OK"
}

# ─────────────────────────────────────────────────────────────────────────────
# 完成
# ─────────────────────────────────────────────────────────────────────────────
Write-Log ""
Write-Log "=== 构建发布完成 ===" "OK"
Write-Log "发布产物目录：$ReleaseDir"
Get-ChildItem $ReleaseDir | Format-Table Name, @{Name="Size(MB)";Expression={"{0:N1}" -f ($_.Length/1MB)}} -AutoSize | Out-Host
Write-Log ""
Write-Log "下一步："
Write-Log "  1. 检查 $ReleaseDir 下的文件是否正确"
Write-Log "  2. 手动测试安装版/便携版能否正常运行"
if (-not $UploadRelease) {
    Write-Log "  3. 运行 .\build-release.ps1 -Version $Version -UploadRelease 上传到 GitHub"
}