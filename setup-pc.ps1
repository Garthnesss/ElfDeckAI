# ElfDeckAI PC setup for Windows: connects your AI client to the Steam Deck
# in one step. Run in PowerShell on the PC (not the Deck) after the Deck
# installer has finished:
#
#   irm https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.ps1 | iex
#
# What it does:
#   1. Creates an SSH key just for ElfDeckAI (%USERPROFILE%\.ssh\elfdeckai_ed25519).
#   2. Copies it to the Deck; you type the Deck password once.
#   3. Checks the connection works without a password.
#   4. Adds ElfDeckAI to Claude Desktop's config (keeping a backup), or
#      prints the config block for other AI clients.
#
# Safe to re-run. Set $env:DECK_IP / $env:DECK_USER to skip the questions.

& {
    # 'Continue', not 'Stop': Windows PowerShell 5.1 turns a native program's
    # stderr into errors, which 'Stop' would make fatal. Exit codes are checked.
    $ErrorActionPreference = 'Continue'
    $Key = Join-Path $env:USERPROFILE '.ssh\elfdeckai_ed25519'
    $DeckUser = if ($env:DECK_USER) { $env:DECK_USER } else { 'deck' }
    $RemoteCmd = '.local/bin/elfdeckai-mcp'

    foreach ($tool in 'ssh', 'ssh-keygen') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Write-Host "$tool was not found. Install it: Settings > System > Optional features > OpenSSH Client."
            return
        }
    }

    Write-Host '==> ElfDeckAI PC setup'
    $DeckIp = $env:DECK_IP
    if (-not $DeckIp) { $DeckIp = Read-Host 'Deck IP address (the Deck installer printed it)' }
    $DeckIp = "$DeckIp".Trim()
    if (-not $DeckIp) { Write-Host 'No IP address given.'; return }
    $Target = "$DeckUser@$DeckIp"

    Write-Host '==> SSH key'
    New-Item -ItemType Directory -Force -Path (Split-Path $Key) | Out-Null
    if (Test-Path $Key) {
        Write-Host "    using existing $Key"
    } else {
        # One raw argument string: Windows PowerShell drops empty arguments
        # such as the -N "" (no passphrase) that ssh-keygen needs.
        $p = Start-Process ssh-keygen -NoNewWindow -Wait -PassThru `
            -ArgumentList "-q -t ed25519 -N `"`" -C elfdeckai -f `"$Key`""
        if ($p.ExitCode -ne 0) { Write-Host 'ssh-keygen failed.'; return }
        Write-Host "    created $Key"
    }

    $common = @('-i', $Key, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=8')

    & ssh @common -o BatchMode=yes $Target true 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '    the Deck already accepts this key'
    } else {
        Write-Host "==> Copying the key to the Deck (enter the Deck's password if asked)"
        $pub = (Get-Content -Raw "$Key.pub").Trim()
        # Single quotes only: Windows PowerShell mangles double quotes in
        # arguments to native programs.
        $remote = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && " +
                  "chmod 600 ~/.ssh/authorized_keys && " +
                  "(grep -qxF '$pub' ~/.ssh/authorized_keys || echo '$pub' >> ~/.ssh/authorized_keys)"
        $remote += " && echo ELFDECKAI-KEY-ADDED"
        # Each attempt only counts when the marker comes back, i.e. the command
        # really ran on the Deck. An agent key can log in yet be limited to one
        # command there (a forced command), which stops ssh before it tries any
        # other key, so this PC's own key files are then tried one by one.
        $attempts = @(, @())                                  # usual keys, agent, password
        # Each key held by the ssh-agent on its own (the agent signs; the
        # public key file only picks which identity is offered).
        $agentDir = Join-Path ([System.IO.Path]::GetTempPath()) ("elfdeckai-" + [guid]::NewGuid())
        $agentKeys = @(& ssh-add -L 2>$null)
        if ($LASTEXITCODE -eq 0) {
            New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
            $n = 0
            foreach ($line in $agentKeys) {
                if (-not "$line".Trim()) { continue }
                $n++
                $file = Join-Path $agentDir "key$n.pub"
                [System.IO.File]::WriteAllText($file, "$line`n", (New-Object System.Text.UTF8Encoding $false))
                $attempts += , @('-i', $file, '-o', 'IdentitiesOnly=yes')
            }
        }
        Get-ChildItem (Split-Path $Key) -Filter 'id_*' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ne '.pub' } |
            ForEach-Object { $attempts += , @('-i', $_.FullName, '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none') }
        $attempts += , @('-o', 'PubkeyAuthentication=no')     # password only
        $log = @()
        $refused = $false
        $added = $false
        foreach ($extra in $attempts) {
            $output = & ssh @extra -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 $Target $remote 2>&1 |
                ForEach-Object { "$_" }
            if ("$output" -match 'ELFDECKAI-KEY-ADDED') { $added = $true; break }
            $log += $output
            if ($LASTEXITCODE -ne 255 -and "$output".Trim()) { $refused = $true }
        }
        Remove-Item -Recurse -Force $agentDir -ErrorAction SilentlyContinue
        if (-not $added) {
            Write-Host ''
            if ($refused -or ("$log" -match 'Permission denied')) {
                Write-Host 'No key on this PC can add the new one to the Deck, and the Deck does not'
                Write-Host 'accept a password. Add this line to ~/.ssh/authorized_keys on the Deck (in'
                Write-Host 'Konsole on the Deck, or from a computer that can already log in), then run'
                Write-Host 'this again:'
                Write-Host ''
                Write-Host "  $pub"
            } else {
                $log | ForEach-Object { Write-Host "    $_" }
                Write-Host "Could not reach $Target. Check that:"
                Write-Host '  - the Deck is on and on the same network, and the IP is right'
                Write-Host "  - SSH is on: on the Deck run 'passwd' once, then 'sudo systemctl enable --now sshd'"
            }
            return
        }
    }

    Write-Host '==> Checking the connection'
    & ssh @common -o BatchMode=yes $Target "test -x $RemoteCmd"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    Connected, but ElfDeckAI is not installed on the Deck.'
        Write-Host '    On the Deck, in Konsole, run:'
        Write-Host '    curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/install-on-deck.sh | sh'
        return
    }
    Write-Host '    OK: passwordless connection works and ElfDeckAI is installed'

    $serverArgs = @('-i', $Key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', $Target, $RemoteCmd)

    # Claude Desktop keeps its config in %APPDATA%\Claude; the Microsoft
    # Store build keeps it inside its package folder instead.
    $dirs = @(Join-Path $env:APPDATA 'Claude')
    $dirs += Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter '*Claude*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' } |
        Where-Object { Test-Path $_ }
    $configs = @($dirs | ForEach-Object { Join-Path $_ 'claude_desktop_config.json' })
    $existing = @($configs | Where-Object { Test-Path $_ })
    if ($existing.Count -eq 0) { $existing = @($configs[0]) }

    Write-Host '==> Configuring your AI client'
    $utf8 = New-Object System.Text.UTF8Encoding $false   # no BOM: Claude cannot parse one
    foreach ($config in $existing) {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path $config) | Out-Null
            $data = New-Object PSObject
            if (Test-Path $config) {
                Copy-Item $config "$config.bak" -Force
                $text = [System.IO.File]::ReadAllText($config)
                if ($text.Trim()) { $data = $text | ConvertFrom-Json -ErrorAction Stop }
            }
            if (-not $data.PSObject.Properties['mcpServers']) {
                $data | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject) -ErrorAction Stop
            }
            $entry = [PSCustomObject]@{ command = 'ssh'; args = $serverArgs }
            $data.mcpServers | Add-Member -NotePropertyName elfdeckai -NotePropertyValue $entry -Force -ErrorAction Stop
            [System.IO.File]::WriteAllText($config, ($data | ConvertTo-Json -Depth 20) + "`n", $utf8)
            Write-Host "    Added ElfDeckAI to Claude Desktop: $config"
            if (Test-Path "$config.bak") { Write-Host "    (previous config saved as $config.bak)" }
        } catch {
            Write-Host "    Could not edit $config automatically: $($_.Exception.Message)"
        }
    }

    $argsJson = ConvertTo-Json -Compress -InputObject $serverArgs
    Write-Host ''
    Write-Host "Done. Restart Claude Desktop (quit it from the system tray), then ask it: `"take a screenshot of my Deck`"."
    Write-Host ''
    Write-Host 'Using a different AI client? Add this MCP server to it:'
    Write-Host ''
    Write-Host "  {`"mcpServers`": {`"elfdeckai`": {`"command`": `"ssh`", `"args`": $argsJson}}}"
    Write-Host ''
    Write-Host 'Claude Code:'
    Write-Host "  claude mcp add elfdeckai -- ssh -i `"$Key`" -o IdentitiesOnly=yes -o BatchMode=yes $Target $RemoteCmd"
}
