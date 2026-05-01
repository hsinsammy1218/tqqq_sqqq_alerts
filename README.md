# TQQQ / SQQQ swing alert system

QQQ-driven technical alerts for leveraged Nasdaq ETFs (alert-only; no broker execution).

Private repository — add application code per project plan.

## Local Git repository (first-time)

From this folder:

```powershell
cd C:\Users\hsins\projects\tqqq-sqqq-alerts
powershell -ExecutionPolicy Bypass -File .\init-git-here.ps1
```

That creates **`main`**, the **initial commit**, and **`_init_git_result.txt`**. The commit uses placeholder author `Local <local@localhost>` so it works without global `git config`. Change later with `git commit --amend --reset-author` after setting your real name and email.

If `.git` was half-initialized (errors about `HEAD`), delete the `.git` folder once, then run the script again.

## Create the private GitHub repository

### Option A — Script (Git required; `gh` optional)

From this folder:

```powershell
cd C:\Users\hsins\projects\tqqq-sqqq-alerts
powershell -ExecutionPolicy Bypass -File .\scripts\setup-private-repo.ps1
```

- Writes details to **`git-gh-setup.log`**.
- If **`gh` is not installed**, the script still creates **`main`** and the first commit, then prints how to add `origin` and push manually.
- Install GitHub CLI when you want one-shot repo creation: `winget install --id GitHub.cli`, then `gh auth login`, then run the script again.

**If commit fails with “tell me who you are”**, set your identity once:

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Then re-run the script.

### Option B — No `gh` (website + HTTPS)

1. On GitHub: **New repository** → name it (e.g. `tqqq-sqqq-alerts`) → **Private** → create **without** README (you already have files locally).
2. In PowerShell:

```powershell
cd C:\Users\hsins\projects\tqqq-sqqq-alerts
git add README.md .gitignore scripts
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<YOU>/<REPO>.git
git push -u origin main
```

Use the same `git config user.*` lines as above if `git commit` asks for identity.

### Option C — `gh` installed and logged in

```powershell
winget install --id GitHub.cli
gh auth login
powershell -ExecutionPolicy Bypass -File .\scripts\setup-private-repo.ps1
```

The script runs `gh repo create tqqq-sqqq-alerts --private --source=. --remote=origin --push`. If that name exists, it retries **`tqqq-sqqq-alerts-trading`**.
