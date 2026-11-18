# Running the checks on a schedule

Every tool exits with a status code, so a scheduler or monitoring system can
alert on failures without parsing output:

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | WARNING (e.g. certificate expires within 30 days, slow service) |
| 2 | CRITICAL (expired or untrusted certificate, failing service) |
| 3 | ERROR (couldn't run the check: DNS, connection or sign-in failure) |

These follow the Nagios plugin convention, so the tools also drop into
Nagios, Icinga, Zabbix or PRTG as custom checks.

## Credentials

Never put passwords in a script or on the command line. Set `GIS_PASSWORD` in
the environment of the account that runs the task (or use your secret store
to set it at run time). Use a dedicated read-only account where possible:
`gis-service-health` without `--admin` and `gis-portal-audit` only read data.

## Windows Task Scheduler

ArcGIS Enterprise usually runs on Windows, so this is the common setup.

1. Install Python and the toolkit on the server or an admin workstation:

   ```powershell
   py -m pip install git+https://github.com/mpashapu/gis-admin-toolkit.git
   ```

2. Create `C:\GISAdmin\daily-checks.ps1`:

   ```powershell
   $ErrorActionPreference = "Continue"
   $date = Get-Date -Format "yyyy-MM-dd"
   $out = "C:\GISAdmin\reports\$date"
   New-Item -ItemType Directory -Force -Path $out | Out-Null

   gis-ssl-check --hosts-file C:\GISAdmin\hosts.txt --csv "$out\certs.csv"
   $ssl = $LASTEXITCODE

   gis-service-health https://gis.example.gov/arcgis --html "$out\services.html" --csv "$out\services.csv"
   $svc = $LASTEXITCODE

   $worst = [Math]::Max($ssl, $svc)
   if ($worst -ge 1) {
       Send-MailMessage -SmtpServer smtp.example.gov -From gis-monitor@example.gov `
           -To gis-team@example.gov -Subject "GIS checks: status $worst" `
           -Body "See reports in $out" -Attachments "$out\services.html"
   }
   exit $worst
   ```

3. Schedule it daily:

   ```powershell
   schtasks /Create /TN "GIS Daily Checks" /SC DAILY /ST 06:30 `
     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\GISAdmin\daily-checks.ps1"
   ```

`Send-MailMessage` is marked obsolete by Microsoft but still works with an
internal relay. Swap in your organization's alerting (Teams webhook, ticket
system) if you have one.

## Linux cron

```cron
# Daily at 6:30: certificates and services
30 6 * * * cd /opt/gis-admin && gis-ssl-check --hosts-file hosts.txt --csv reports/certs-$(date +\%F).csv
35 6 * * * cd /opt/gis-admin && gis-service-health https://gis.example.gov/arcgis --html reports/services-$(date +\%F).html

# Monthly portal content review (1st of the month)
0 7 1 * * cd /opt/gis-admin && GIS_PASSWORD="$(cat /etc/gis-admin/pw)" gis-portal-audit https://gis.example.gov/portal --username svc_audit -o reports/portal-$(date +\%F)
```

## Suggested cadence

| Check | How often | Why |
|---|---|---|
| `gis-ssl-check` | Daily | Gives 30+ days' warning before renewals |
| `gis-service-health` | Every 15–60 min, or daily with `--html` | Catches stopped services and broken data connections |
| `gis-portal-audit` | Monthly | Content cleanup and public-sharing review |
