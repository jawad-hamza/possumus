# Upload ZehnMS from this Windows PC to the VPS and deploy it.
#
#   First time:  powershell -ExecutionPolicy Bypass -File deploy\upload.ps1 -Domain shop.example.com
#   Updates:     powershell -ExecutionPolicy Bypass -File deploy\upload.ps1
#
# Ships the committed code of -Ref (default HEAD) with `git archive`, so .env files, the local
# database, media, the virtualenv and the licence private key can never be uploaded.
# Commit first: uncommitted changes are not included.
#
# Needs: OpenSSH (ssh, scp) and an ~/.ssh/config entry for the server (-SshHost), whose user is
# in the docker group. On the server, /srv/zehnms must exist and belong to that user once:
#   sudo install -d -o $USER -g $USER -m 750 /srv/zehnms
param(
    [string]$SshHost = "hostinger-vps",
    [string]$Ref = "HEAD",
    [string]$Domain = "",
    [string]$RemoteHome = "/srv/zehnms"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$dirty = git status --porcelain --untracked-files=no
if ($dirty -and $Ref -eq "HEAD") {
    Write-Warning "Uncommitted changes are NOT uploaded (git archive ships the last commit):"
    $dirty | ForEach-Object { Write-Host "  $_" }
}
$commit = (git rev-parse --short $Ref).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unknown git ref: $Ref" }

$archive = Join-Path $env:TEMP "zehnms-$commit.tar.gz"
Write-Host "==> Packing $Ref ($commit)"
git archive --format=tar.gz -o $archive $Ref
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

Write-Host "==> Uploading to ${SshHost}"
$remoteArchive = "/tmp/zehnms-$commit.tar.gz"
scp -q $archive "${SshHost}:$remoteArchive"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
Remove-Item $archive

$domainArg = ""
if ($Domain) { $domainArg = "--domain $Domain" }

# The release is unpacked next to the live code and swapped in with rsync, so the app folder
# never holds a half-extracted tree. Data lives in Docker volumes and $RemoteHome, not in app/.
$remoteScript = @"
set -euo pipefail
test -d $RemoteHome && test -w $RemoteHome || { echo 'Run once on the server: sudo install -d -o `$USER -g `$USER -m 750 $RemoteHome' >&2; exit 1; }
stage=`$(mktemp -d)
tar -xzf $remoteArchive -C "`$stage"
rm -f $remoteArchive
mkdir -p $RemoteHome/app
rsync -a --delete "`$stage/" $RemoteHome/app/
rm -rf "`$stage"
echo '$commit' > $RemoteHome/app/RELEASE
bash $RemoteHome/app/deploy/vps-deploy.sh $domainArg
"@

Write-Host "==> Deploying on the server"
# Sent base64-encoded with LF line endings: piping text from Windows PowerShell to ssh would add
# CR characters, which bash rejects.
$remoteScript = ($remoteScript -replace "`r`n", "`n") + "`n"
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
# No double quotes in the remote command: Windows PowerShell 5.1 mangles them for native programs.
ssh $SshHost "f=`$(mktemp) && echo $encoded | base64 -d > `$f && bash `$f < /dev/null; rc=`$?; rm -f `$f; exit `$rc"
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed (exit $LASTEXITCODE)" }
Write-Host "==> Done: $commit is live"
