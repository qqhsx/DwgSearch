$ReleaseDir = 'G:\Script\dwg_search_project\V2.19.0\release_dist'
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$portableSourceDir = 'G:\Script\dwg_search_project\V2.19.0\dist\DwgSearchApp'
$portableZipPath = Join-Path $ReleaseDir 'DWG_Search_Portable_x64_2.19.0.zip'
$setupSourceDir = Join-Path $ReleaseDir 'DWG_Search_Setup_x64'
$setupZipPath = Join-Path $ReleaseDir 'DWG_Search_Setup_x64_2.19.0.zip'

Write-Host 'Creating portable ZIP...'
Compress-Archive -Path "$portableSourceDir\*" -DestinationPath $portableZipPath -Force
Write-Host ("Portable: {0} MB" -f [math]::Round((Get-Item $portableZipPath).Length/1MB,1))

Write-Host 'Creating setup ZIP...'
if (Test-Path $setupSourceDir) { Remove-Item -Recurse -Force $setupSourceDir }
Copy-Item $portableSourceDir $setupSourceDir -Recurse
Rename-Item (Join-Path $setupSourceDir 'DwgSearchApp.exe') 'DWG_Search.exe' -Force
Compress-Archive -Path "$setupSourceDir\*" -DestinationPath $setupZipPath -Force
Write-Host ("Setup: {0} MB" -f [math]::Round((Get-Item $setupZipPath).Length/1MB,1))

Write-Host 'Generating SHA256...'
$sha256Lines = @()
foreach ($f in @($setupZipPath, $portableZipPath)) {
    $hash = (Get-FileHash -Algorithm SHA256 -Path $f).Hash.ToLower()
    $name = Split-Path $f -Leaf
    $sha256Lines += "$hash  $name"
    Write-Host "  $name  $hash"
}
$sha256Path = Join-Path $ReleaseDir 'SHA256SUMS.txt'
$sha256Lines -join "`n" | Set-Content -Path $sha256Path -Encoding UTF8

Write-Host 'Uploading to GitHub Release...'
$TagName = 'v2.19.0'
$RepoOwner = 'qqhsx'
$RepoName = 'dwg-search'
$ReleaseTitle = 'DWG Search Tool V2.19.0'

$releaseExists = gh release view $TagName --repo "$RepoOwner/$RepoName" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Creating new Release...'
    $releaseUrl = gh release create $TagName --repo "$RepoOwner/$RepoName" --title $ReleaseTitle --notes-file (Join-Path (Split-Path $ReleaseDir) 'CHANGELOG.md') --generate-notes 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "Create Release failed: $releaseUrl"; exit 1 }
} else {
    Write-Host 'Release exists, uploading assets'
}

$assets = @($setupZipPath, $portableZipPath, $sha256Path)
foreach ($asset in $assets) {
    $name = Split-Path $asset -Leaf
    Write-Host "  Uploading $name ..."
    $result = gh release upload $TagName $asset --repo "$RepoOwner/$RepoName" --clobber 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "Upload $name failed: $result"; exit 1 }
}

Write-Host ("[OK] Release published: https://github.com/{0}/{1}/releases/tag/{2}" -f $RepoOwner, $RepoName, $TagName)
