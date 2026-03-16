# Hot-reload server: restarts on any .py change
# Usage: .\serve.ps1
$dir = $PSScriptRoot
$python = "$dir\.venv\Scripts\python.exe"
while ($true) {
    $proc = Start-Process -FilePath $python -ArgumentList "-m server" -WorkingDirectory $dir -PassThru -NoNewWindow
    Write-Host "[serve] Started PID=$($proc.Id). Watching for .py changes..."
    $watcher = [System.IO.FileSystemWatcher]::new($dir, "*.py")
    $watcher.IncludeSubdirectories = $true
    $watcher.EnableRaisingEvents = $true
    $changed = $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::All)
    Write-Host "[serve] Changed: $($changed.Name). Restarting..."
    $watcher.Dispose()
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}
