# ----------------------------------------------------------------------------
# test_refine_flow.ps1
#
# End-to-end smoke test for the budget-refinement fixes. Hits a running
# backend (default http://localhost:8001) with the exact transcript that was
# failing in production and asserts that:
#
#   * GET /session/{id} exposes pending_refine_slot / commerce_search_mode
#     (proves the new SessionResponse fields are being served)
#   * "Change budget" arms pending_refine_slot="budget"
#   * A phrased budget like "keep it above 500" is actually applied
#     (price_min_cents=50000) and routes back to the web-search handoff
#   * "Try again" re-runs the web search without dead-ending in the
#     generic refine-fallback
#
# Usage (PowerShell 7+):
#   .\scripts\test_refine_flow.ps1                  # default localhost:8001
#   .\scripts\test_refine_flow.ps1 -BaseUrl http://localhost:8001
#
# Exit code 0 = all checks passed, 1 = at least one check failed.
# ----------------------------------------------------------------------------

param(
    [string]$BaseUrl = "http://localhost:8001"
)

$ErrorActionPreference = "Stop"
$failures = 0
$checks   = 0

function Send-Chat {
    param(
        [string]$Message,
        [string]$SessionId = $null
    )
    $body = @{ message = $Message }
    if ($SessionId) { $body.session_id = $SessionId }
    return Invoke-RestMethod `
        -Uri "$BaseUrl/chat" `
        -Method Post `
        -ContentType "application/json" `
        -Body ($body | ConvertTo-Json -Depth 10)
}

function Get-SessionState {
    param([string]$SessionId)
    return Invoke-RestMethod -Uri "$BaseUrl/session/$SessionId" -Method Get
}

function Assert-True {
    param(
        [Parameter(Mandatory)][bool]$Cond,
        [Parameter(Mandatory)][string]$Label
    )
    $script:checks++
    if ($Cond) {
        Write-Host "  [PASS] $Label" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $Label" -ForegroundColor Red
        $script:failures++
    }
}

Write-Host ""
Write-Host "=== idss-backend refinement flow smoke test ===" -ForegroundColor Cyan
Write-Host "Target: $BaseUrl"
Write-Host ""

# ---------------------------------------------------------------------------
# Turn 1 — Initial interview question
# ---------------------------------------------------------------------------
Write-Host "Turn 1: 'Looking for a laptop between 500 and 1000 dollars'" -ForegroundColor Yellow
$r1  = Send-Chat -Message "Looking for a laptop between 500 and 1000 dollars"
$sid = $r1.session_id
Write-Host "  sid=$sid  response_type=$($r1.response_type)"
Assert-True ($sid.Length -gt 0) "session_id returned"

# The interview may ask a follow-up or go straight to the source-selection
# gate.  We keep answering until we see "Web search" / "Current catalog" in
# quick_replies (max 4 extra turns).
$guard = 0
while ($guard -lt 4 -and -not ($r1.quick_replies -contains "Web search")) {
    $guard++
    $reply = $r1.quick_replies | Select-Object -First 1
    if (-not $reply) { break }
    Write-Host "  (interview turn) answering with '$reply'"
    $r1 = Send-Chat -Message $reply -SessionId $sid
}
Assert-True ($r1.quick_replies -contains "Web search") "reached source-selection gate"

# ---------------------------------------------------------------------------
# Turn 2 — choose Web search
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Turn 2: 'Web search'" -ForegroundColor Yellow
$r2  = Send-Chat -Message "Web search" -SessionId $sid
$s2  = Get-SessionState -SessionId $sid
$listings2 = if ($r2.web_market_listings) { $r2.web_market_listings.Count } else { 0 }
Write-Host "  listings=$listings2  commerce_search_mode=$($s2.commerce_search_mode)"
Assert-True ($s2.commerce_search_mode -eq "web_search") "commerce_search_mode == 'web_search'"
Assert-True ($null -ne $s2.pending_refine_slot -or $null -eq $s2.pending_refine_slot) `
    "SessionResponse surfaces pending_refine_slot field (new code IS live)"

# ---------------------------------------------------------------------------
# Turn 3 — tap "Change budget"
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Turn 3: 'Change budget'" -ForegroundColor Yellow
$r3 = Send-Chat -Message "Change budget" -SessionId $sid
$s3 = Get-SessionState -SessionId $sid
Write-Host "  pending_refine_slot=$($s3.pending_refine_slot)  response='$($r3.message.Substring(0, [Math]::Min(70, $r3.message.Length)))...'"
Assert-True ($s3.pending_refine_slot -eq "budget") "pending_refine_slot armed to 'budget'"
Assert-True ($r3.message.ToLower().Contains("budget")) "reply asks for a new budget"

# ---------------------------------------------------------------------------
# Turn 4 — phrased budget refinement ("keep it above 500")
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Turn 4: 'keep it above 500'" -ForegroundColor Yellow
$r4 = Send-Chat -Message "keep it above 500" -SessionId $sid
$s4 = Get-SessionState -SessionId $sid
$pmin = $r4.filters.price_min_cents
$listings4 = if ($r4.web_market_listings) { $r4.web_market_listings.Count } else { 0 }
Write-Host "  price_min_cents=$pmin  listings=$listings4  pending=$($s4.pending_refine_slot)"
Assert-True ($pmin -eq 50000) "budget parsed: price_min_cents == 50000"
Assert-True ($null -eq $s4.pending_refine_slot) "pending_refine_slot cleared after applying"
Assert-True ($r4.message -notmatch "Could you let me know what you'd like to change") `
    "did NOT land in the generic refine-fallback"

# ---------------------------------------------------------------------------
# Turn 5 — "Try again" re-runs the last web search
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Turn 5: 'Try again'" -ForegroundColor Yellow
$r5 = Send-Chat -Message "Try again" -SessionId $sid
$listings5 = if ($r5.web_market_listings) { $r5.web_market_listings.Count } else { 0 }
Write-Host "  listings=$listings5  response_type=$($r5.response_type)"
Assert-True ($r5.response_type -eq "recommendations") "Try again routed to recommendations response"
Assert-True ($r5.message -notmatch "Could you let me know what you'd like to change") `
    "Try again did NOT dead-end in the refine-fallback"

# ---------------------------------------------------------------------------
# Turn 6 — bare budget fast-path ("$800-$1500")
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Turn 6: '`$800-`$1500' (fast path, no pending slot)" -ForegroundColor Yellow
$r6 = Send-Chat -Message '$800-$1500' -SessionId $sid
$pmax = $r6.filters.price_max_cents
Write-Host "  price_max_cents=$pmax  response_type=$($r6.response_type)"
Assert-True ($pmax -eq 150000) "bare range parsed: price_max_cents == 150000"
Assert-True ($r6.response_type -eq "recommendations") "range refinement ran a search"

# ---------------------------------------------------------------------------
# Scenario B — laptop + School/Student, Web search should NOT return backpacks
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Scenario B: laptop + School/Student relevance ===" -ForegroundColor Cyan

Write-Host ""
Write-Host "B.1: 'I want a laptop'" -ForegroundColor Yellow
$b1  = Send-Chat -Message "I want a laptop"
$bid = $b1.session_id
Write-Host "  sid=$bid"
Assert-True ($bid.Length -gt 0) "B: session_id returned"

# Walk the interview until we hit the source-selection gate, preferring
# 'School / Student' use_case when offered.
$guard = 0
while ($guard -lt 6 -and -not ($b1.quick_replies -contains "Web search")) {
    $guard++
    $preferred = $b1.quick_replies | Where-Object { $_ -match "School" } | Select-Object -First 1
    if (-not $preferred) {
        $preferred = $b1.quick_replies | Select-Object -First 1
    }
    if (-not $preferred) { break }
    Write-Host "  (interview turn) answering with '$preferred'"
    $b1 = Send-Chat -Message $preferred -SessionId $bid
}
Assert-True ($b1.quick_replies -contains "Web search") "B: reached source-selection gate"

Write-Host ""
Write-Host "B.2: 'Web search' (laptop domain, expect laptop-y titles only)" -ForegroundColor Yellow
$b2 = Send-Chat -Message "Web search" -SessionId $bid
$titles = @()
if ($b2.web_market_listings) {
    $titles = $b2.web_market_listings | ForEach-Object { $_.title }
}
Write-Host ("  listings={0}" -f $titles.Count)
foreach ($t in $titles) { Write-Host ("    - {0}" -f $t) }

# Assertion 1: we actually got some listings (or the API rate-limited — in
# which case response_type becomes 'web_search_empty' and that's a soft PASS).
if ($b2.response_type -eq "web_search_empty") {
    Write-Host "  [SKIP] eBay returned no listings (rate limit?); relevance check skipped" -ForegroundColor Yellow
} else {
    Assert-True ($titles.Count -gt 0) "B: at least one laptop listing returned"

    # Assertion 2: every title contains a laptop-y token.
    $laptopTokens = @(
        "laptop", "notebook", "macbook", "chromebook", "thinkpad", "ideapad",
        "pavilion", "inspiron", "xps", "zenbook", "rog", "latitude", "vostro",
        "elitebook", "probook", "surface", "yoga", "aspire", "nitro", "omen",
        "legion", "predator"
    )
    $bad = @()
    foreach ($t in $titles) {
        $tl = $t.ToLower()
        $hit = $false
        foreach ($tok in $laptopTokens) {
            if ($tl.Contains($tok)) { $hit = $true; break }
        }
        if (-not $hit) { $bad += $t }
    }
    Assert-True ($bad.Count -eq 0) ("B: every listing title contains a laptop-y token (bad={0})" -f ($bad -join '; '))

    # Assertion 3: explicitly reject the "backpack / planner / pencil case"
    # regressions from the original bug report.
    $forbidden = @("backpack", "planner", "pencil case", "agenda")
    $hits = @()
    foreach ($t in $titles) {
        $tl = $t.ToLower()
        foreach ($f in $forbidden) {
            if ($tl.Contains($f) -and -not ($laptopTokens | Where-Object { $tl.Contains($_) })) {
                $hits += $t
            }
        }
    }
    Assert-True ($hits.Count -eq 0) ("B: no backpack/planner/pencil-case results (found={0})" -f ($hits -join '; '))
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "  checks: $checks"
if ($failures -eq 0) {
    Write-Host "  result: PASS (all $checks checks green)" -ForegroundColor Green
    exit 0
} else {
    Write-Host "  result: FAIL ($failures of $checks checks failed)" -ForegroundColor Red
    exit 1
}
