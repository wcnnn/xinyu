# Create (and install) the local code-signing certificate used by 心屿 releases.
#
# Why: Windows Smart App Control blocks fresh unsigned PyInstaller builds with
#   "blocked by Device Guard policy"
# A signed exe runs even though the certificate is self-signed.
#
# ASCII only on purpose: PowerShell 5.1 reads non-BOM files as ANSI.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup_signing.ps1
#   powershell -ExecutionPolicy Bypass -File setup_signing.ps1 -Remove
#
# What it does:
#   1. creates a self-signed code-signing cert in CurrentUser\My (3 years)
#   2. exports Xinyu-CodeSigning.cer next to this script
#   3. imports it into TrustedPublisher (may need admin for LocalMachine)
#   4. tries the trust root too; without admin it only warns (signing still works
#      for Smart App Control, but other machines will show "unknown publisher")
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$subject = 'CN=Xinyu Local Code Signing, O=Xinyu, C=CN'
$cerPath = Join-Path $PSScriptRoot 'Xinyu-CodeSigning.cer'

if ($Remove) {
  Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq $subject } |
    ForEach-Object { Remove-Item -LiteralPath $_.PSPath -Force; Write-Host "removed: $($_.Thumbprint)" }
  return
}

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq $subject } |
        Sort-Object NotAfter -Descending | Select-Object -First 1

if (-not $cert) {
  $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
    -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
    -CertStoreLocation 'Cert:\CurrentUser\My' -NotAfter (Get-Date).AddYears(3) `
    -KeyUsage DigitalSignature `
    -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3')
  Write-Host "created: $($cert.Thumbprint)"
} else {
  Write-Host "reusing: $($cert.Thumbprint)"
}

Export-Certificate -Cert $cert -FilePath $cerPath -Force | Out-Null
Write-Host "exported: $cerPath"
Write-Host "thumbprint: $($cert.Thumbprint)"
Write-Host "not after : $($cert.NotAfter)"

foreach ($store in @('Cert:\LocalMachine\TrustedPublisher', 'Cert:\CurrentUser\TrustedPublisher',
                     'Cert:\LocalMachine\Root', 'Cert:\CurrentUser\Root')) {
  try {
    Import-Certificate -FilePath $cerPath -CertStoreLocation $store | Out-Null
    Write-Host "trusted  : $store"
  } catch {
    Write-Host "skipped  : $store ($($_.Exception.Message.Split([char]10)[0]))"
  }
}

Write-Host ''
Write-Host 'Done. Rebuild with:  python build.py'
Write-Host 'build.py signs the exe automatically from now on.'
Write-Host ''
Write-Host 'To let OTHER machines trust the signature:'
Write-Host "  1. copy $cerPath to that machine"
Write-Host '  2. right click -> Install Certificate -> Local Machine -> Trusted Root'
Write-Host '     (or Trusted Publishers), confirm the prompt'