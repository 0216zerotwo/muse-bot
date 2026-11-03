# 用数据线把 Muse Bot 安卓 App 装到手机上（PowerShell），在仓库根目录运行：
#   powershell -ExecutionPolicy Bypass -File android\install.ps1
# 手机先打开「开发者选项 → USB 调试」，连上数据线，手机上弹出「允许 USB 调试吗？」时点允许。
# 安装包：android\dist 里最新的 .apk（bash android/build.sh 打的，或从 GitHub Actions 下载后放进去）。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# adb：tools\android-sdk 里的（见 android\README.md），没有就用 PATH 里的。
$adb = Join-Path $root "tools\android-sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) {
    $found = Get-Command adb -ErrorAction SilentlyContinue
    if ($found) { $adb = $found.Source }
}
$apk = Get-ChildItem (Join-Path $PSScriptRoot "dist\*.apk") -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not (Test-Path $adb)) { Write-Host "找不到 adb：装 Android platform-tools，或放到 tools\android-sdk\platform-tools"; exit 1 }
if ($null -eq $apk) { Write-Host "找不到安装包：先运行 bash android/build.sh，或从 GitHub Actions 下载 APK 放进 android\dist"; exit 1 }

Write-Host "==> 找手机"
& $adb start-server | Out-Null
$state = (& $adb get-state 2>$null)
if ($state -ne "device") {
    & $adb devices
    Write-Host "没找到可用的手机：确认数据线连好、手机上打开了 USB 调试，并在手机弹窗里点了允许（显示 unauthorized 就是还没点）。"
    exit 1
}

Write-Host "==> 安装 $($apk.Name)"
& $adb install -r $apk.FullName
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装失败。如果提示 INSTALL_FAILED_UPDATE_INCOMPATIBLE（旧版用别的密钥签名），先卸载旧版再装："
    Write-Host "  & `"$adb`" uninstall com.personalbot.companion"
    exit 1
}

Write-Host "==> 让手机的 127.0.0.1:8766 连到这台电脑上的 Muse Bot 服务器"
& $adb reverse tcp:8766 tcp:8766
if ($LASTEXITCODE -ne 0) { Write-Host "端口转发失败：App 装好了，但用数据线连不上电脑上的服务器"; exit 1 }

Write-Host ""
Write-Host "装好了。在手机上打开「Muse Bot」，地址填 http://127.0.0.1:8766，点「连接」。"
Write-Host "（电脑上的服务器要在运行；拔掉数据线后再连上，重新运行一次这个脚本即可）"
