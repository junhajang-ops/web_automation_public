# cs/ 폴더의 스크립트. 루트의 .venv·서비스계정 키를 사용한다.
# 실행: cs_payment\cs 에서  .\start_copilot.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$windowStatePath = Join-Path $scriptDir "window_state.json"

# --- 콘솔 창 위치/크기(+글자 크기) 기억(2026-07-09 사용자 요청) ---
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

    // 글자(폰트) 크기 — GetStdHandle(STD_OUTPUT_HANDLE)은 리다이렉트되면 콘솔이 아닌
    // 핸들이 될 수 있어, 항상 실제 콘솔 화면 버퍼를 가리키는 "CONOUT$"를 직접 연다.
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern IntPtr CreateFile(string fileName, uint desiredAccess, uint shareMode,
        IntPtr securityAttributes, uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);
    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr hObject);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetCurrentConsoleFontEx(IntPtr hConsoleOutput, bool bMaximumWindow, ref CONSOLE_FONT_INFOEX lpConsoleCurrentFontEx);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetCurrentConsoleFontEx(IntPtr hConsoleOutput, bool bMaximumWindow, ref CONSOLE_FONT_INFOEX lpConsoleCurrentFontEx);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CONSOLE_FONT_INFOEX {
        public uint cbSize;
        public uint nFont;
        public short FontWidth;
        public short FontHeight;
        public int FontFamily;
        public int FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string FaceName;
    }
}
"@

function Get-CsConsoleOutputHandle {
    # GENERIC_READ|GENERIC_WRITE=0xC0000000(=3221225472), FILE_SHARE_READ|WRITE=3, OPEN_EXISTING=3.
    # 0xC0000000을 그대로 쓰면 PowerShell이 부호있는 Int32로 파싱해 음수가 되어
    # uint 매개변수 변환에 실패한다(라이브 확인) — 10진수 리터럴로 안전하게 넘긴다.
    return [CsWinPos]::CreateFile("CONOUT$", [uint32]3221225472, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero)
}

function Test-CsInvalidHandle {
    param([IntPtr]$Handle)
    # INVALID_HANDLE_VALUE는 (IntPtr)(-1) — 64/32비트 모두 안전하게 Int64로 비교한다.
    return ($Handle -eq [IntPtr]::Zero) -or ($Handle.ToInt64() -eq -1)
}

function Get-CsConsoleFont {
    $handle = Get-CsConsoleOutputHandle
    if (Test-CsInvalidHandle $handle) { return $null }
    try {
        $info = New-Object CsWinPos+CONSOLE_FONT_INFOEX
        $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf([type][CsWinPos+CONSOLE_FONT_INFOEX])
        if (-not [CsWinPos]::GetCurrentConsoleFontEx($handle, $false, [ref]$info)) { return $null }
        return $info
    } finally {
        [CsWinPos]::CloseHandle($handle) | Out-Null
    }
}

function Set-CsConsoleFont {
    param([int]$FontWidth, [int]$FontHeight, [string]$FaceName)
    if ($FontHeight -le 0) { return }
    $handle = Get-CsConsoleOutputHandle
    if (Test-CsInvalidHandle $handle) { return }
    try {
        # 기존 폰트 정보(nFont/FontFamily/FontWeight 등)를 베이스로 시작해, 크기만
        # 바꾸고 나머지는 시스템이 이미 골라둔 값을 그대로 존중한다.
        $info = Get-CsConsoleFont
        if (-not $info) {
            $info = New-Object CsWinPos+CONSOLE_FONT_INFOEX
            $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf([type][CsWinPos+CONSOLE_FONT_INFOEX])
        }
        $info.FontWidth = [int16]$FontWidth
        $info.FontHeight = [int16]$FontHeight
        if ($FaceName) { $info.FaceName = $FaceName }
        [CsWinPos]::SetCurrentConsoleFontEx($handle, $false, [ref]$info) | Out-Null
    } finally {
        [CsWinPos]::CloseHandle($handle) | Out-Null
    }
}

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

    # 글자 크기를 먼저 적용한다 — 콘솔 폰트를 바꾸면 창이 문자 단위 버퍼 크기를
    # 유지하려고 자동으로 다시 리사이즈될 수 있어, 나중에 적용하는 창 크기(픽셀)가
    # 최종적으로 남도록 순서를 이렇게 둔다.
    if ($b.fontHeight -gt 0) {
        Set-CsConsoleFont -FontWidth ([int]$b.fontWidth) -FontHeight ([int]$b.fontHeight) -FaceName $b.fontFace
    }

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

        # 글자 크기도 함께 저장(2026-07-09 추가 요청) — 최대화 여부와 무관하게 항상 담는다.
        $font = Get-CsConsoleFont
        if ($font -and $font.FontHeight -gt 0) {
            $bounds | Add-Member -NotePropertyName "fontWidth" -NotePropertyValue ([int]$font.FontWidth)
            $bounds | Add-Member -NotePropertyName "fontHeight" -NotePropertyValue ([int]$font.FontHeight)
            if ($font.FaceName) {
                $bounds | Add-Member -NotePropertyName "fontFace" -NotePropertyValue $font.FaceName
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
