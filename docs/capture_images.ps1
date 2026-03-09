$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$htmlDir = Join-Path $root 'docs\snapshots\html'
$imageDir = Join-Path $root 'docs\snapshots\images'
New-Item -ItemType Directory -Force -Path $imageDir | Out-Null

$edgeCandidates = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Google\Chrome\Application\chrome.exe'
)
$browser = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) {
    throw 'No supported browser found. Install Microsoft Edge or Google Chrome.'
}

$heightMap = @{
    dashboard = 1700
    pos = 1850
    products = 1700
    stock_control = 1700
    purchases = 1750
    expenses = 1850
    money_desk = 1850
    parties = 1700
    customers = 1700
    suppliers = 1700
    accounts = 1750
    money_breakdown = 1750
    reports = 1700
    daily_sales_report = 1850
    customer_detail = 1750
    supplier_detail = 1750
    product_detail = 1700
    purchase_detail = 1700
    invoice = 1700
}

Get-ChildItem -Path $htmlDir -Filter *.html | Sort-Object Name | ForEach-Object {
    $name = $_.BaseName
    $height = if ($heightMap.ContainsKey($name)) { $heightMap[$name] } else { 1700 }
    $output = Join-Path $imageDir ($name + '.png')
    $uri = 'file:///' + ($_.FullName -replace '\\','/')
    & $browser --headless=new --disable-gpu --hide-scrollbars --virtual-time-budget=8000 --window-size=1440,$height --screenshot=$output $uri | Out-Null
    if (-not (Test-Path $output)) {
        throw "Failed to capture $name"
    }
    Write-Output "Captured: $output"
}
