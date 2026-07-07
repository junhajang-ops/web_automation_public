# cs/ 폴더의 스크립트. 루트의 .venv·서비스계정 키를 사용한다.
# 실행: cs_payment\cs 에서  .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$windowStatePath = Join-Path $scriptDir "window_state.json"

# --- 콘솔 창 위치/크기 기억(2026-07-09 사용자 요청) ---
# cs_copilot.py가 같은 파일의 "cs_browser"/"console_browser" 키로 브라우저
# 창 위치/크기를 기억하는 것과 짝을 이룬다. 이 파일은 로컬 화면 배치일 뿐이라
# .gitignore에 등록돼 있다(커밋 금지).
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class CsWinPos {
    [DllImport("kernel32.dll")]
    public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")]
    public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
    [DllImport("user32.dll")]
    public static extern bool IsZoomed(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

function Read-CsWindowState {
    if (-not (Test-Path $windowStatePath)) { return $null }
    try {
        $raw = Get-Content -Path $windowStatePath -Raw -Encoding UTF8
        return $raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Restore-PowerShellWindowBounds {
    $hwnd = [CsWinPos]::GetConsoleWindow()
    if ($hwnd -eq [IntPtr]::Zero) { return }
    $state = Read-CsWindowState
    if (-not $state -or -not $state.powershell) { return }
    $b = $state.powershell
    if ($b.maximized) {
        [CsWinPos]::ShowWindow($hwnd, 3) | Out-Null  # SW_MAXIMIZE
    } elseif ($null -ne $b.left -and $b.width -gt 0 -and $b.height -gt 0) {
        [CsWinPos]::MoveWindow($hwnd, [int]$b.left, [int]$b.top, [int]$b.width, [int]$b.height, $true) | Out-Null
    }
}

function Save-PowerShellWindowBounds {
    # 실패해도(핸들 못 얻음 등) 종료 절차를 막으면 안 되는 부가 기능이라 조용히 건너뛴다.
    try {
        $hwnd = [CsWinPos]::GetConsoleWindow()
        if ($hwnd -eq [IntPtr]::Zero) { return }

        $state = Read-CsWindowState
        if (-not $state) { $state = New-Object PSObject }

        if ([CsWinPos]::IsZoomed($hwnd)) {
            $bounds = [PSCustomObject]@{ maximized = $true }
        } else {
            $rect = New-Object CsWinPos+RECT
            if (-not [CsWinPos]::GetWindowRect($hwnd, [ref]$rect)) { return }
            $bounds = [PSCustomObject]@{
                left   = $rect.Left
                top    = $rect.Top
                width  = ($rect.Right - $rect.Left)
                height = ($rect.Bottom - $rect.Top)
            }
        }

        if ($state.PSObject.Properties.Match("powershell").Count -gt 0) {
            $state.powershell = $bounds
        } else {
            $state | Add-Member -NotePropertyName "powershell" -NotePropertyValue $bounds
        }

        $json = $state | ConvertTo-Json -Depth 5
        # Windows PowerShell 5.1의 Set-Content/Out-File -Encoding UTF8은 BOM을 붙여
        # 저장하는데, Python 쪽 json.loads가 BOM에서 깨질 수 있어 BOM 없이 직접 쓴다.
        [System.IO.File]::WriteAllText($windowStatePath, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-Host "[안내] 콘솔 창 위치/크기 저장 실패: $_"
    }
}

Restore-PowerShellWindowBounds

try {
    # 루트 .env에서 GOOGLE_KEY_FILE 읽기
    $keyFile = ""
    Get-Content "$root\.env" -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*GOOGLE_KEY_FILE\s*=\s*(.+)') {
            $keyFile = $Matches[1].Trim()
        }
    }
    if (-not $keyFile) {
        Write-Error ".env에 GOOGLE_KEY_FILE이 없습니다."
        exit 1
    }

    & "$root\.venv\Scripts\python.exe" "$scriptDir\cs_copilot.py" `
        --key "$root\$keyFile"
}
finally {
    Save-PowerShellWindowBounds
    # 오류로 창이 곧바로 닫혀 원인을 못 보는 것을 막기 위해 종료 전 대기한다.
    Write-Host ""
    Read-Host "종료하려면 Enter"
}
