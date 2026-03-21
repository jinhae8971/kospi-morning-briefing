[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$GH_USER    = "jinhae8971"
$GH_REPO    = "kospi-morning-briefing"
# 토큰은 환경변수 또는 실행 시 입력
$GH_TOKEN   = if ($env:GH_TOKEN) { $env:GH_TOKEN } else { Read-Host "GitHub Personal Access Token을 입력하세요" }
$REMOTE_URL = "https://$GH_TOKEN@github.com/$GH_USER/$GH_REPO.git"
$API_HDR    = @{
    "Authorization" = "token $GH_TOKEN"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "GitHubActionsDeploy"
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "=== KOSPI 모닝브리핑 GitHub 배포 ===" -ForegroundColor Cyan

# ── [1] Git 초기화 ─────────────────────────────────────────
git config --global --add safe.directory ($ScriptDir -replace '\\', '/') 2>$null
if (-not (Test-Path ".git")) { git init | Out-Null }
$prev = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
git remote remove origin 2>$null | Out-Null
$ErrorActionPreference = $prev
git remote add origin $REMOTE_URL
git config user.name  $GH_USER
git config user.email "jinhae8971@gmail.com"
@"
config.json
*.pyc
__pycache__/
.env
*.log
report_*.html
"@ | Set-Content -Encoding UTF8 ".gitignore"
Write-Host "[1] Git 설정 완료" -ForegroundColor Green

# ── [2] GitHub 레포 생성 ───────────────────────────────────
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO" `
        -Headers $API_HDR | Out-Null
    Write-Host "[2] 레포 이미 존재함" -ForegroundColor Green
} catch {
    try {
        $body = @{
            name        = $GH_REPO
            private     = $false
            auto_init   = $false
            description = "KOSPI 모닝브리핑 멀티에이전트 토론 시스템"
        } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" `
            -Headers $API_HDR -Body $body -ContentType "application/json" | Out-Null
        Write-Host "[2] 레포 생성 완료" -ForegroundColor Green
        Start-Sleep -Seconds 2
    } catch {
        Write-Host "[2] 수동 생성 후 Enter: https://github.com/new  (이름: $GH_REPO)" -ForegroundColor Red
        Read-Host "Press Enter after creating repo"
    }
}

# ── [3] 커밋 & 푸시 ────────────────────────────────────────
$ErrorActionPreference = "SilentlyContinue"
git add .
git commit -m "feat: KOSPI 모닝브리핑 멀티에이전트 토론 시스템 초기 배포" 2>$null
if ($LASTEXITCODE -ne 0) { git commit --allow-empty -m "chore: update" 2>$null }
git branch -M main
git push -u origin main --force 2>$null
$pushCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($pushCode -ne 0) {
    Write-Host "PUSH 실패. 토큰 'repo' 권한 확인: https://github.com/settings/tokens/new" -ForegroundColor Red
    exit 1
}
Write-Host "[3] 코드 푸시 완료" -ForegroundColor Green

# ── [4] Secrets 등록 ──────────────────────────────────────
$ANTHROPIC_KEY = Read-Host "ANTHROPIC_API_KEY를 입력하세요"
# GitHub Actions용 PAT (Gist 쓰기 권한 포함 — 기존 토큰 재사용)
$GITHUB_PAT = $GH_TOKEN

$secrets = @{
    ANTHROPIC_API_KEY = $ANTHROPIC_KEY
    TELEGRAM_TOKEN    = "8481005106:AAESmINZyjDHrbno69EVB6kSMSjWyG_dyCU"
    TELEGRAM_CHAT_ID  = "954137156"
    GITHUB_PAT        = $GITHUB_PAT
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
    $env:GH_TOKEN = $GH_TOKEN
    foreach ($s in $secrets.GetEnumerator()) {
        gh secret set $s.Key --body $s.Value --repo "$GH_USER/$GH_REPO" 2>$null
        Write-Host "  Secret 등록: $($s.Key)" -ForegroundColor Cyan
    }
    Write-Host "[4] Secrets 등록 완료 (gh CLI)" -ForegroundColor Green
} else {
    Write-Host "[4] gh CLI 미설치 — 아래 URL에서 수동 등록하세요:" -ForegroundColor Yellow
    Write-Host "    https://github.com/$GH_USER/$GH_REPO/settings/secrets/actions" -ForegroundColor White
    foreach ($s in $secrets.GetEnumerator()) {
        Write-Host "    $($s.Key) = $($s.Value)" -ForegroundColor Cyan
    }
    Read-Host "Secrets 등록 후 Enter"
}

# ── [5] 워크플로우 수동 트리거 ─────────────────────────────
try {
    Invoke-RestMethod -Method Post `
        -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/actions/workflows/morning_briefing.yml/dispatches" `
        -Headers $API_HDR -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
    Write-Host "[5] 워크플로우 트리거 성공!" -ForegroundColor Green
    Write-Host "    약 3-5분 후 결과 확인: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
    Write-Host "    Telegram으로도 결과가 전송됩니다" -ForegroundColor White
} catch {
    Write-Host "[5] 트리거 실패 — 수동 실행:" -ForegroundColor Yellow
    Write-Host "    https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
}

Write-Host ""
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host " 배포 완료!  약 5분 후 Telegram 알림을 확인하세요" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
