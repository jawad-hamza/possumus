<#
.SYNOPSIS
    Builds the ZehnMS Windows desktop package: ZehnMS.exe (PyInstaller) and, when Inno Setup
    is installed, ZehnMS-Setup-<version>.exe.

.DESCRIPTION
    The installed program opens in its own window on the Microsoft Edge WebView2 runtime, which
    Windows 11 and nearly every Windows 10 PC already have. For the few that do not, run this
    script once with -FetchWebView2: it downloads Microsoft's small "Evergreen Bootstrapper"
    (about 2 MB, git-ignored) to desktop\redist\MicrosoftEdgeWebview2Setup.exe, and the installer
    then carries it and runs it only on PCs where the runtime is missing. Without it the installer
    just tells the shop, and ZehnMS opens in a Microsoft Edge app window instead.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File desktop\build.ps1
    powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -Version 1.2.0 -SkipInstaller
    powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -FetchWebView2
#>
param(
    [string]$Version = "",
    [switch]$SkipInstaller,
    [switch]$SkipSmokeTest,
    [switch]$FetchWebView2
)

$ErrorActionPreference = "Stop"

$DesktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $DesktopDir
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DistDir = Join-Path $DesktopDir "dist"
$AppDir = Join-Path $DistDir "ZehnMS"
$WebView2Bootstrapper = Join-Path $DesktopDir "redist\MicrosoftEdgeWebview2Setup.exe"
# Microsoft's permanent link to the Evergreen Bootstrapper (developer.microsoft.com/microsoft-edge/webview2).
$WebView2BootstrapperUrl = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

if (-not $Version) {
    $Version = (Get-Content (Join-Path $DesktopDir "VERSION") -TotalCount 1).Trim()
}

function Invoke-Step {
    param([string]$Title, [scriptblock]$Action)
    Write-Host ""
    Write-Host "== $Title" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
        throw "$Title failed (exit code $LASTEXITCODE)."
    }
}

Set-Location $ProjectRoot

if (-not (Test-Path $VenvPython)) {
    Invoke-Step "Creating virtual environment" { py -3 -m venv (Join-Path $ProjectRoot ".venv") }
}

Invoke-Step "Installing build requirements" {
    & $VenvPython -m pip install --disable-pip-version-check -q -r (Join-Path $ProjectRoot "requirements-desktop.txt")
}

# The window needs pywebview and pythonnet. Both have wheels for Python 3.10 to 3.14; if a future
# Python has none yet, build with an older interpreter (py -3.13 -m venv .venv) rather than without them.
Invoke-Step "Checking the application window (pywebview on WebView2)" {
    & $VenvPython -c "import sys, webview, clr_loader, importlib.metadata as m; print('pywebview', m.version('pywebview'), '+ pythonnet', m.version('pythonnet'), 'on Python', sys.version.split()[0])"
}

if (-not (Test-Path (Join-Path $DesktopDir "zehnms.ico"))) {
    Invoke-Step "Drawing the placeholder icon" { & $VenvPython (Join-Path $DesktopDir "make_icon.py") }
}

if ($FetchWebView2 -and -not (Test-Path $WebView2Bootstrapper)) {
    Invoke-Step "Downloading the WebView2 Evergreen Bootstrapper from Microsoft" {
        New-Item -ItemType Directory -Force (Split-Path -Parent $WebView2Bootstrapper) | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri $WebView2BootstrapperUrl -OutFile $WebView2Bootstrapper
        $Signature = Get-AuthenticodeSignature $WebView2Bootstrapper
        if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notlike "*Microsoft Corporation*") {
            Remove-Item -Force $WebView2Bootstrapper
            throw "The downloaded WebView2 bootstrapper is not signed by Microsoft; it was deleted."
        }
    }
}

# collectstatic runs with the same settings the installed copy uses, so the manifest matches.
$env:DEPLOYMENT_MODE = "desktop"
$env:DJANGO_DEBUG = "false"
$env:DJANGO_SECRET_KEY = "build-time-only-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:DJANGO_FILE_LOGGING = "false"
$env:ZEHNMS_DATA_DIR = Join-Path $ProjectRoot "build\desktop-build-data"
New-Item -ItemType Directory -Force $env:ZEHNMS_DATA_DIR | Out-Null

Invoke-Step "Collecting static files" {
    & $VenvPython (Join-Path $ProjectRoot "manage.py") collectstatic --noinput --clear -v 0
}

Invoke-Step "Building ZehnMS.exe with PyInstaller" {
    & $VenvPython -m PyInstaller (Join-Path $DesktopDir "zehnms.spec") --noconfirm `
        --distpath $DistDir --workpath (Join-Path $ProjectRoot "build\pyinstaller")
}

Remove-Item Env:DEPLOYMENT_MODE, Env:DJANGO_DEBUG, Env:DJANGO_SECRET_KEY, Env:DJANGO_FILE_LOGGING, Env:ZEHNMS_DATA_DIR

if (-not $SkipSmokeTest) {
    Write-Host ""
    Write-Host "== Smoke test 1: start ZehnMS.exe as the shortcut does, check the server and the window" -ForegroundColor Cyan
    $SmokeData = Join-Path $ProjectRoot "build\desktop-smoke-data"
    if (Test-Path $SmokeData) { Remove-Item -Recurse -Force $SmokeData }
    $SmokePort = 8799
    $App = Join-Path $AppDir "ZehnMS.exe"
    $Cli = Join-Path $AppDir "zehnms-cli.exe"
    $Clock = [System.Diagnostics.Stopwatch]::StartNew()
    $Server = Start-Process -FilePath $App -PassThru `
        -ArgumentList @("--port", "$SmokePort", "--data-dir", "`"$SmokeData`"")
    $Healthy = $false
    $HealthyAfter = $null
    $WindowAfter = $null
    while ($Clock.Elapsed.TotalSeconds -lt 120 -and -not $Server.HasExited) {
        Start-Sleep -Milliseconds 100
        if (-not $WindowAfter) {
            $Server.Refresh()
            if ($Server.MainWindowTitle -eq "ZehnMS") { $WindowAfter = $Clock.Elapsed.TotalSeconds }
        }
        if (-not $Healthy) {
            try {
                $Response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$SmokePort/healthz/"
                if ($Response.StatusCode -eq 200) { $Healthy = $true; $HealthyAfter = $Clock.Elapsed.TotalSeconds }
            } catch { }
        }
        if ($Healthy -and ($WindowAfter -or $Clock.Elapsed.TotalSeconds -gt ($HealthyAfter + 10))) { break }
    }
    $LoginOk = $false
    if ($Healthy) {
        $Login = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 "http://127.0.0.1:$SmokePort/login/"
        $LoginOk = ($Login.StatusCode -eq 200 -and $Login.Content -like "*ZehnMS*")
    }
    & $Cli --stop --data-dir $SmokeData | Out-Null
    if (-not $Server.HasExited) { $Server.WaitForExit(20000) | Out-Null }
    if (-not $Healthy) {
        throw "Smoke test failed: the packaged app did not answer /healthz/. See $SmokeData\logs."
    }
    if (-not $LoginOk) { throw "Smoke test failed: the sign-in page did not render." }
    if (-not $Server.HasExited) { throw "Smoke test failed: ZehnMS.exe was still running 20 s after --stop." }
    if (Test-Path (Join-Path $SmokeData "zehnms.lock")) { throw "Smoke test failed: the lock file was left behind." }
    Write-Host ("First run (creates the database): /healthz/ 200 after {0:N1} s; sign-in page rendered; clean exit." -f $HealthyAfter) -ForegroundColor Green
    if ($WindowAfter) {
        Write-Host ("A window titled 'ZehnMS' appeared after {0:N1} s." -f $WindowAfter) -ForegroundColor Green
    } else {
        Write-Warning "No window titled 'ZehnMS' was seen (normal on a build machine without a desktop session, or when WebView2 is missing)."
    }

    Write-Host ""
    Write-Host "== Smoke test 2: let the app inspect its own window (--self-test)" -ForegroundColor Cyan
    & $Cli --self-test --port $SmokePort --data-dir $SmokeData
    $SelfTestFile = Join-Path $SmokeData "selftest.json"
    if (-not (Test-Path $SelfTestFile)) { throw "Smoke test failed: --self-test wrote no report. See $SmokeData\logs." }
    $SelfTest = Get-Content $SelfTestFile -Raw | ConvertFrom-Json
    Write-Host "Window strategy used on this PC: $($SelfTest.strategy)" -ForegroundColor Green
    if ($SelfTest.strategy -eq "webview") {
        Write-Host ("WebView2 {0}: window after {1} s, first page '{2}' after {3} s." -f `
            $SelfTest.webview2_runtime, $SelfTest.window_shown_after_s, $SelfTest.page.title, $SelfTest.first_page_after_s)
        if (-not $SelfTest.loaded) { throw "Smoke test failed: the window never finished loading the first page." }
        if ($SelfTest.page.resources_outside_this_pc.Count -gt 0) {
            throw "Smoke test failed: the page asked the internet for: $($SelfTest.page.resources_outside_this_pc -join ', ')"
        }
    }
}

if ($SkipInstaller) {
    Write-Host ""
    Write-Host "Done. Portable build: $AppDir" -ForegroundColor Green
    exit 0
}

$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
if (-not $Iscc) {
    $Candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $Iscc = $Candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

if (-not $Iscc) {
    Write-Host ""
    Write-Warning "Inno Setup 6 (ISCC.exe) was not found, so no Setup.exe was made."
    Write-Host "Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup) and run this script again."
    Write-Host "Portable build: $AppDir" -ForegroundColor Green
    exit 0
}

if (Test-Path $WebView2Bootstrapper) {
    Write-Host "The installer will carry the WebView2 bootstrapper for PCs that lack the runtime."
} else {
    Write-Host "No desktop\redist\MicrosoftEdgeWebview2Setup.exe: PCs without WebView2 get a notice and an Edge app window (see -FetchWebView2)."
}

Invoke-Step "Building the installer with Inno Setup" {
    & $Iscc "/DAppVersion=$Version" (Join-Path $DesktopDir "installer.iss")
}

Write-Host ""
Write-Host "Done. Installer: $(Join-Path $DistDir "installer\ZehnMS-Setup-$Version.exe")" -ForegroundColor Green
