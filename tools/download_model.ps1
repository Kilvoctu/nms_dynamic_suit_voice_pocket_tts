[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

param(
    [string]$url,
    [string]$outfile
)

Invoke-WebRequest -Uri $url -OutFile $outfile
