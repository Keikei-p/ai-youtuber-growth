# A bounded TCP probe for Windows PowerShell 5.1 scheduled tasks.
# No external side effects on import; testable against localhost only.
function Test-MiraiTcpPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$ComputerName,
        [int]$Port = 443,
        [ValidateRange(1, 60000)][int]$TimeoutMilliseconds = 4000
    )

    $Client = New-Object System.Net.Sockets.TcpClient
    $Async = $null
    try {
        # Test-NetConnection may wait much longer than the intended 90s
        # network recovery window, and shows a console progress screen.
        $Async = $Client.BeginConnect($ComputerName, $Port, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $Client.EndConnect($Async)
        return [bool]$Client.Connected
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $Async) {
            $Async.AsyncWaitHandle.Close()
        }
        $Client.Close()
    }
}
