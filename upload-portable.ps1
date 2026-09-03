$ReleaseDir = 'G:\Script\dwg_search_project\V2.19.0\release_dist'
$portableSourceDir = 'G:\Script\dwg_search_project\V2.19.0\dist\DwgSearchApp'
$portableZipPath = Join-Path $ReleaseDir 'DwgSearch_Portable_x64_v2.19.0.zip'
$sha256Path = Join-Path $ReleaseDir 'SHA256SUMS.txt'

# Create portable ZIP with new naming
Write-Host 'Creating portable ZIP...'
if (Get-Command 7z -ErrorAction SilentlyContinue) {
    & 7z a -tzip -mx=9 $portableZipPath ("{0}\*" -f $portableSourceDir) | Out-Null
} else {
    Compress-Archive -Path ("{0}\*" -f $portableSourceDir) -DestinationPath $portableZipPath -Force
}
Write-Host ("Portable: {0} MB" -f [math]::Round((Get-Item $portableZipPath).Length/1MB,1))

# Generate SHA256
$hash = (Get-FileHash -Algorithm SHA256 -Path $portableZipPath).Hash.ToLower()
$name = Split-Path $portableZipPath -Leaf
$sha256Line = "{0}  {1}" -f $hash, $name
Write-Host "  $name  $hash"
$sha256Line | Set-Content -Path $sha256Path -Encoding UTF8

# Upload to GitHub Release
Write-Host 'Uploading to GitHub Release...'
$TagName = 'v2.19.0'
$RepoOwner = 'qqhsx'
$RepoName = 'DwgSearch'

$assets = @($portableZipPath, $sha256Path)
foreach ($asset in $assets) {
    $name = Split-Path $asset -Leaf
    Write-Host "  Uploading $name ..."
    $result = gh release upload $TagName $asset --repo "$RepoOwner/$RepoName" --clobber 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "Upload $name failed: $result"; exit 1 }
}

Write-Host ("[OK] Release published: https://github.com/{0}/{1}/releases/tag/{2}" -f $RepoOwner, $RepoName, $TagName)
