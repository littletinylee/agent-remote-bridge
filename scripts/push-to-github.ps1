# Push the finished history to GitHub in one step.
#
#   powershell -ExecutionPolicy Bypass -File scripts\push-to-github.ps1
#
# Preconditions: the empty repo already exists on GitHub (create it in the web
# UI - the fine-grained PAT used during development may lack repo-creation
# rights), and your SSH key is authorised (git@github.com works).
#
# It refuses to do anything destructive: no force push, no rewriting.

$ErrorActionPreference = "Stop"
$owner = "littletinylee"
$repo = "agent-remote-bridge"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $remote = "git@github.com:$owner/$repo.git"

    Write-Host "==> checking ssh access to $remote"
    $env:GIT_SSH_COMMAND = "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    git ls-remote $remote 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "远程仓库不可访问。请先在 GitHub 创建空仓库："
        Write-Host "  https://github.com/new?name=$repo"
        exit 1
    }

    $existing = git remote
    if ($existing -contains "origin") {
        git remote set-url origin $remote
    } else {
        git remote add origin $remote
    }
    Write-Host "==> origin = $(git remote get-url origin)"

    Write-Host "==> pushing main"
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { throw "git push main failed" }

    Write-Host "==> pushing tags"
    git push origin --tags
    if ($LASTEXITCODE -ne 0) { throw "git push --tags failed" }

    Write-Host ""
    Write-Host "==> verifying remote refs"
    git ls-remote --heads --tags $remote
    Write-Host ""
    Write-Host "完成: https://github.com/$owner/$repo"
    git log --oneline -1
}
finally {
    Pop-Location
}
