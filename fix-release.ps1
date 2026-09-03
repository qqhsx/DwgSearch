$ReleaseDir = 'G:\Script\dwg_search_project\V2.19.0\release_dist'
$setupSourceDir = Join-Path $ReleaseDir 'DWG_Search_Setup_x64'
$setupZipPath = Join-Path $ReleaseDir 'DWG_Search_Setup_x64_2.19.0.zip'
$portableSourceDir = 'G:\Script\dwg_search_project\V2.19.0\dist\DwgSearchApp'

# Fix the setup zip - rename exe inside (actual exe name has space)
if (Test-Path $setupSourceDir) { Remove-Item -Recurse -Force $setupSourceDir }
Copy-Item $portableSourceDir $setupSourceDir -Recurse
# Actual exe name from build.spec: name='DwgSearch V2.18.0'
Rename-Item (Join-Path $setupSourceDir 'DwgSearch V2.18.0.exe') 'DWG_Search.exe' -Force
Compress-Archive -Path "$setupSourceDir\*" -DestinationPath $setupZipPath -Force
Write-Host ("Fixed setup ZIP: {0} MB" -f [math]::Round((Get-Item $setupZipPath).Length/1MB,1))

# Re-upload
$uploadResult = gh release upload v2.19.0 $setupZipPath --repo qqhsx/dwg-search --clobber 2>&1
Write-Host $uploadResult
Write-Host 'Re-uploaded setup ZIP'

# Also update SHA256
$hash = (Get-FileHash -Algorithm SHA256 -Path $setupZipPath).Hash.ToLower()
$name = Split-Path $setupZipPath -Leaf
$portableZipPath = Join-Path $ReleaseDir 'DWG_Search_Portable_x64_2.19.0.zip'
$portableHash = (Get-FileHash -Algorithm SHA256 -Path $portableZipPath).Hash.ToLower()
$portableName = Split-Path $portableZipPath -Leaf
$sha256Lines = @("{0}  {1}" -f $hash, $name, "{0}  {1}" -f $portableHash, $portableName)
$sha256Path = Join-Path $ReleaseDir 'SHA256SUMS.txt'
$sha256Lines -join "`n" | Set-Content -Path $sha256Path -Encoding UTF8
gh release upload v2.19.0 $sha256Path --repo qqhsx/dwg-search --clobber 2>&1
Write-Host 'Updated SHA256SUMS.txt'
