# Sends one of the sample transcripts to the running API (POST /evaluate) and prints the result.
# Usage (from the repo root, with the Docker stack running):
#   .\scripts\try-evaluate.ps1 -Sample strong        # strong | borderline | weak
#   .\scripts\try-evaluate.ps1 -Sample borderline -CandidateId C002
# For a borderline result, approve/reject it with:
#   .\scripts\try-evaluate.ps1 -Review <thread_id> -Decision APPROVE
param(
    [ValidateSet("strong", "borderline", "weak")] [string]$Sample = "strong",
    [string]$CandidateId = "DEMO-$(Get-Random -Maximum 9999)",
    [string]$Review,
    [ValidateSet("APPROVE", "REJECT")] [string]$Decision = "APPROVE",
    [string]$BaseUrl = "http://localhost:8000"
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$key = (Select-String (Join-Path $root ".env") -Pattern '^SCREENER_API_KEY=(.+)').Matches.Groups[1].Value
$headers = @{ "X-API-Key" = $key }

if ($Review) {
    $body = @{ decision = $Decision; reviewer = "hr@example.com" } | ConvertTo-Json
    $result = Invoke-RestMethod "$BaseUrl/review/$Review" -Method Post -Headers $headers -ContentType "application/json" -Body $body
} else {
    $names = @{ strong = "Sara Khan"; borderline = "Bilal Ahmed"; weak = "Kamran Ali" }
    # [IO.File] instead of Get-Content: PowerShell 5.1 attaches hidden file properties to
    # Get-Content strings, and ConvertTo-Json would send them along.
    $transcript = [IO.File]::ReadAllText((Join-Path $root "screener-api\tests\fixtures\transcripts\$Sample.txt"))
    $body = @{
        candidate_id          = $CandidateId
        candidate_name        = $names[$Sample]
        candidate_email       = "candidate@example.com"
        role                  = "Python Developer Intern"
        transcript            = $transcript
        call_duration_seconds = 280
    } | ConvertTo-Json
    Write-Host "Evaluating '$Sample' as $CandidateId (LLM calls take 10-60 s)..." -ForegroundColor Cyan
    $result = Invoke-RestMethod "$BaseUrl/evaluate" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 300
}

$s = $result.scores
Write-Host "`nthread_id : $($result.thread_id)"
Write-Host "status    : $($result.status)   decision: $($result.decision)   sheet: $($result.sheet_status)" -ForegroundColor Green
if ($s) { Write-Host "score     : $($s.total)/40  (C$($s.communication) T$($s.technical) P$($s.problem_solving) M$($s.motivation))  available=$($s.available)  confidence=$($s.confidence)" }
if ($result.interview) { Write-Host "interview : $($result.interview.start)" }
if ($result.flags) { Write-Host "flags     : $($result.flags -join ', ')" }
Write-Host "`n--- message to candidate ---`n$($result.messages.candidate)"
Write-Host "`n--- message to HR ---`n$($result.messages.hr)"
if ($result.status -eq "pending_review") {
    Write-Host "`nWaiting for HR. Resume with:`n  .\scripts\try-evaluate.ps1 -Review $($result.thread_id) -Decision APPROVE" -ForegroundColor Yellow
}
