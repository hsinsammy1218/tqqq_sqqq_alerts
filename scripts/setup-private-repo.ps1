# Creates a local git repo and optionally a private GitHub repo via GitHub CLI (`gh`).
# Run from repo root: powershell -ExecutionPolicy Bypass -File .\scripts\setup-private-repo.ps1
#
# Without gh: script still creates the first commit; then add remote + push manually (see README).

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$LogPath = Join-Path $RepoRoot "git-gh-setup.log"
"" | Set-Content -Path $LogPath -Encoding UTF8
function Log([string]$msg) {
    $msg | Tee-Object -FilePath $LogPath -Append
}

Log "Repo root: $RepoRoot"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Log "ERROR: git not found. Install Git for Windows first."
    exit 1
}

$initOut = git init 2>&1
$initOut | ForEach-Object { Log "$_" }
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Log "ERROR: git init did not create .git"
    exit 1
}

$addOut = git add README.md .gitignore 2>&1
$addOut | ForEach-Object { Log "$_" }
if (Test-Path "scripts") {
    $addScripts = git add scripts 2>&1
    $addScripts | ForEach-Object { Log "$_" }
}

git rev-parse --verify HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log "Creating initial commit..."
    $commitOut = git commit -m "Initial commit" 2>&1
    $commitOut | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -ne 0) {
        Log ""
        Log "ERROR: git commit failed (common cause: missing identity)."
        Log "Run once in this repo:"
        Log '  git config user.name "Your Name"'
        Log '  git config user.email "you@example.com"'
        Log 'Or set globally (recommended):'
        Log '  git config --global user.name "Your Name"'
        Log '  git config --global user.email "you@example.com"'
        exit 1
    }
}

$branchOut = git branch -M main 2>&1
$branchOut | ForEach-Object { Log "$_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: could not rename branch to main"
    exit 1
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Log ""
    Log "GitHub CLI (gh) not found — local repo is ready with branch main."
    Log "Install gh (optional): winget install --id GitHub.cli"
    Log "Then: gh auth login"
    Log "Then re-run this script, OR create a private repo on github.com and:"
    Log '  git remote add origin https://github.com/<YOU>/<REPO>.git'
    Log "  git push -u origin main"
    exit 0
}

$authOut = gh auth status 2>&1
$authOut | ForEach-Object { Log "$_" }
if ($LASTEXITCODE -ne 0) {
    Log ""
    Log "gh is installed but not logged in. Run: gh auth login"
    Log "Then re-run this script."
    exit 1
}

$repoName = "tqqq-sqqq-alerts"
Push-Location $RepoRoot
try {
    $createOut = gh repo create $repoName --private --source=. --remote=origin --push 2>&1
    $createOut | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -ne 0) {
        Log "Retry with alternate name..."
        $retryOut = gh repo create "${repoName}-trading" --private --source=. --remote=origin --push 2>&1
        $retryOut | ForEach-Object { Log "$_" }
    }
} finally {
    Pop-Location
}

$remoteOut = git remote -v 2>&1
$remoteOut | ForEach-Object { Log "$_" }
Log "Done. See git-gh-setup.log for details."
