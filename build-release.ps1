<#>
.SYNOPSIS
    DwgSearch - Build Portable Release Script
    Usage: Run in PowerShell as Administrator on Windows 7 build machine
    .\build-release.ps1 [-Version "2.19.0"] [-UploadRelease] [-GitHubToken "ghp_xxx"]

.DESCRIPTION
    1. Clean old build artifacts
    2. Build with PyInstaller (folder mode only - portable)
    3. Create portable ZIP
    4. Generate SHA256SUMS.txt
    5. Optional: Create GitHub Release and upload assets

.NOTES
    Requires: Python 3.8+, PyInstaller, GitHub CLI (gh), 7-Zip (optional)
    .NET subprojects must be pre-built (DwgTextExtractor, DwgTextReplacer)
    Build on Windows 7 for Windows 7 compatibility
</#>

param(
    [string]$Version = "2.19.0",
    [switch]$UploadRelease,
    [string]$GitHubToken = "",
    [string]$RepoOwner = "qqhsx",
    [string]$RepoName = "DwgSearch",
    [switch]$SkipBuild
)

# Configuration
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$DistDir = Join-Path $ProjectRoot "dist"
$BuildDir = Join-Path $ProjectRoot "build"
$ReleaseDir = Join-Path $ProjectRoot "release_dist"
$VenVPython = Join-Path $ProjectRoot "venv38\Scripts\python.exe"
$PyInstaller = Join-Path $ProjectRoot "venv38\Scripts\pyinstaller.exe"

$TagName = "v$Version"
$ReleaseTitle = "DwgSearch V$Version"

$FolderModeExeName = "DwgSearchApp"
$PortableZipName = "DwgSearch_Portable_x64_v$Version.zip"
$Sha256FileName = "SHA256SUMS.txt"

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
        $cmdSrc = (Get-Command $cmd).Source
        Write-Log ("[OK] Found {0}: {1}" -f $Name, $cmdSrc)
        return $true
    } else {
        Write-Log ("[ERROR] Missing {0} ({1}), please install and add to PATH" -f $Name, $cmd)
        return $false
    }
}

function Get-Sha256 {
    param([string]$FilePath)
    $hash = Get-FileHash -Algorithm SHA256 -Path $FilePath
    return $hash.Hash.ToLower()
}

# Main
Write-Log ("=== DwgSearch Build Portable Release v{0} ===" -f $Version)
Write-Log ("Project root: {0}" -f $ProjectRoot)

$ok = $true
$ok = (Check-Command "python" $VenVPython) -and $ok
$ok = (Check-Command "pyinstaller" $PyInstaller) -and $ok
$ok = (Check-Command "git") -and $ok
if ($UploadRelease) { $ok = (Check-Command "gh") -and $ok }
if (-not $ok) { exit 1 }

if (-not $SkipBuild) {
    Write-Log "Cleaning old build directories..."
    Remove-Item -Recurse -Force $DistDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $BuildDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $ReleaseDir -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
}

# Build .NET subprojects
if (-not $SkipBuild) {
    Write-Log "Checking .NET subprojects..."
    $netProjects = @(
        @{ Path = "DwgTextExtractor"; Config = "Release"; Framework = "net48" },
        @{ Path = "DwgTextReplacer"; Config = "Release"; Framework = "net48" }
    )
    foreach ($proj in $netProjects) {
        $projPath = Join-Path $ProjectRoot $proj.Path
        $csproj = Get-ChildItem $projPath -Filter "*.csproj" | Select-Object -First 1
        if ($csproj) {
            $exeName = $csproj.BaseName + ".exe"
            $exePath = Join-Path $projPath ("bin\{0}\{1}\{2}" -f $proj.Config, $proj.Framework, $exeName)
            if (-not (Test-Path $exePath)) {
                Write-Log ("  Building {0}..." -f $proj.Path)
                $result = dotnet build $csproj.FullName -c $proj.Config -f $proj.Framework --no-restore
                if ($LASTEXITCODE -ne 0) { Write-Log "  [WARN] Build failed, continuing (may have pre-built artifacts)" }
            } else {
                Write-Log ("  [OK] {0} has build artifacts" -f $proj.Path)
            }
        }
    }
}

# PyInstaller folder mode (portable)
if (-not $SkipBuild) {
    Write-Log "Building: Folder mode (build.spec) - Portable..."
    $specFile = Join-Path $ProjectRoot "build.spec"
    $result = & $PyInstaller $specFile --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Log "[ERROR] Folder mode build failed"; exit 1 }
    
    $folderModeDir = Join-Path $DistDir $FolderModeExeName
    $folderModeExe = Join-Path $folderModeDir ("{0}.exe" -f $FolderModeExeName)
    if (-not (Test-Path $folderModeExe)) {
        Write-Log ("[ERROR] Folder mode artifact not found: {0}" -f $folderModeExe)
        exit 1
    }
    Write-Log ("  Artifact dir: {0}" -f $folderModeDir)
}

# Create portable ZIP
Write-Log "Creating portable package..."
$portableSourceDir = Join-Path $DistDir $FolderModeExeName
$portableZipPath = Join-Path $ReleaseDir $PortableZipName
Write-Log ("  Creating portable: {0}" -f $PortableZipName)
if (Get-Command 7z -ErrorAction SilentlyContinue) {
    & 7z a -tzip -mx=9 $portableZipPath ("{0}\*" -f $portableSourceDir) | Out-Null
} else {
    Compress-Archive -Path ("{0}\*" -f $portableSourceDir) -DestinationPath $portableZipPath -Force
}
$portableSizeMB = [math]::Round((Get-Item $portableZipPath).Length / 1MB, 1)
Write-Log ("  [OK] Portable size: {0} MB" -f $portableSizeMB)

# Generate SHA256SUMS.txt
Write-Log "Generating SHA256 checksums..."
$hash = Get-Sha256 $portableZipPath
$name = Split-Path $portableZipPath -Leaf
$sha256Line = ("{0}  {1}" -f $hash, $name)
Write-Log ("  {0}  {1}" -f $name, $hash)
$sha256Path = Join-Path $ReleaseDir $Sha256FileName
$sha256Line | Set-Content -Path $sha256Path -Encoding UTF8
Write-Log ("  [OK] {0} written" -f $Sha256FileName)

# Upload to GitHub Release
if ($UploadRelease) {
    Write-Log "Uploading to GitHub Release..."
    
    if ($GitHubToken) {
        $env:GH_TOKEN = $GitHubToken
    }
    
    $authCheck = gh auth status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "[ERROR] GitHub CLI not authenticated. Run: gh auth login"
        exit 1
    }
    
    Write-Log ("  Checking/creating Release tag: {0}" -f $TagName)
    $releaseExists = gh release view $TagName --repo ("{0}/{1}" -f $RepoOwner, $RepoName) 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "  Creating new Release..."
        $releaseUrl = gh release create $TagName `
            --repo ("{0}/{1}" -f $RepoOwner, $RepoName) `
            --title $ReleaseTitle `
            --notes-file (Join-Path $ProjectRoot "CHANGELOG.md") `
            --generate-notes `
            2>&1
        if ($LASTEXITCODE -ne 0) { Write-Log ("[ERROR] Create Release failed: {0}" -f $releaseUrl); exit 1 }
    } else {
        Write-Log "  Release exists, uploading assets"
    }
    
    $assets = @($portableZipPath, $sha256Path)
    foreach ($asset in $assets) {
        $name = Split-Path $asset -Leaf
        Write-Log ("  Uploading {0} ..." -f $name)
        $result = gh release upload $TagName $asset --repo ("{0}/{1}" -f $RepoOwner, $RepoName) --clobber 2>&1
        if ($LASTEXITCODE -ne 0) { Write-Log ("[ERROR] Upload {0} failed: {1}" -f $name, $result); exit 1 }
    }
    
    Write-Log ("[OK] Release published: https://github.com/{0}/{1}/releases/tag/{2}" -f $RepoOwner, $RepoName, $TagName)
}

Write-Log ""
Write-Log "=== Build Release Complete ==="
Write-Log ("Release directory: {0}" -f $ReleaseDir)
$files = Get-ChildItem $ReleaseDir
foreach ($f in $files) {
    $sizeMB = [math]::Round($f.Length / 1MB, 1)
    Write-Log ("  {0} - {1} MB" -f $f.Name, $sizeMB)
}
Write-Log ""
Write-Log "Next steps:"
Write-Log "  1. Verify files in $ReleaseDir"
Write-Log "  2. Test portable package on Windows 7/10/11"
if (-not $UploadRelease) {
    Write-Log ("  3. Run .\build-release.ps1 -Version {0} -UploadRelease to upload" -f $Version)
}
