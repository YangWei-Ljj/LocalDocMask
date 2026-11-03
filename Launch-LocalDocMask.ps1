$env:LOCALDOCMASK_HOME = Join-Path $env:USERPROFILE 'LocalDocMaskData'
$exe = Join-Path $PSScriptRoot 'dist\LocalDocMask\LocalDocMask.exe'
Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
