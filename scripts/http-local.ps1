# Compatible with Windows PowerShell 5.1 and PowerShell 7; bypass proxies for loopback.
function Invoke-LocalHttp {
    param([string]$Uri, [string]$Method = 'GET', [hashtable]$Headers = @{}, [int]$TimeoutMs = 3000)
    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.Proxy = $null
    $request.Method = $Method
    $request.Timeout = $TimeoutMs
    foreach ($key in $Headers.Keys) { $request.Headers[$key] = $Headers[$key] }
    if ($Method -eq 'POST') { $request.ContentLength = 0 }
    try { $response = $request.GetResponse() } catch [System.Net.WebException] {
        if ($null -eq $_.Exception.Response) { throw }
        $response = $_.Exception.Response
    }
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        try { $body = $reader.ReadToEnd() } finally { $reader.Dispose() }
        return @{ StatusCode = [int]$response.StatusCode; Body = $body }
    } finally { $response.Close() }
}
