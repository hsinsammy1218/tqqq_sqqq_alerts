# Creates first commit and branch main in THIS folder (fixes empty repo / orphaned HEAD).
# Run: powershell -ExecutionPolicy Bypass -File .\init-git-here.ps1
$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot
$Log = Join-Path $RepoRoot "_init_git_result.txt"

function W([string]$m) { $m | Tee-Object -FilePath $Log -Append }

Remove-Item $Log -ErrorAction SilentlyContinue
W "RepoRoot=$RepoRoot"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    W "ERROR: git not found"
    exit 1
}

if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    git init 2>&1 | ForEach-Object { W $_ }
}

git add README.md .gitignore scripts init-git-here.ps1 2>&1 | ForEach-Object { W $_ }

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
git rev-parse --verify HEAD *> $null
$needsCommit = ($LASTEXITCODE -ne 0)
$ErrorActionPreference = $prevEap
if ($needsCommit) {
    W "Creating initial commit..."
    $commitOut = git -c user.name="Local" -c user.email="local@localhost" commit -m "Initial commit" 2>&1
    $commitOut | ForEach-Object { W $_ }
    if ($LASTEXITCODE -ne 0) {
        W "ERROR: commit failed. Set identity then re-run:"
        W '  git config --global user.name "Your Name"'
        W '  git config --global user.email "you@example.com"'
        exit 1
    }
}

git branch -M main 2>&1 | ForEach-Object { W $_ }
git log -1 --oneline 2>&1 | ForEach-Object { W $_ }
git status -sb 2>&1 | ForEach-Object { W $_ }
W "OK - repo ready on branch main"
