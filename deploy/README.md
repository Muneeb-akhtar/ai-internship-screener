# Deploying to AWS (EC2 + Docker Compose + Caddy HTTPS)

Everything runs on one small Ubuntu server, the same containers as on your PC. Caddy
gets a free HTTPS certificate for your domain automatically.

```
Internet ──443──▶ Caddy ──▶ screener-api   (/interview, /recordings, /hr/review, webhooks, /health)
                        └──▶ n8n           (/webhook/vapi-end-of-call only)
Your PC ──SSH tunnel──▶ n8n editor (127.0.0.1:5678 on the server; never public)
Nightly 02:00 PKT: pg_dump ──▶ S3 bucket (via the server's IAM role, no keys on disk)
```

**Cost:** t3.small ≈ $15/month + public IPv4 ≈ $3.6/month + 30 GB disk ≈ $2.4/month, usually
covered by new-account credits. **Stop the instance when you're not demoing** (Step 12).

---

## Step 0: Budget alert first (2 min)
AWS console → search **Budgets** → **Create budget** → *Use a template* → **Zero spend budget**
(or *Monthly cost budget* = **$5**) → your email → **Create**.
You'll get an email the moment anything costs money.

Use region **Asia Pacific (Mumbai) `ap-south-1`** for everything below (top-right region menu).
It's closest to Pakistan.

## Step 1: Key pair (SSH login)
EC2 → **Key pairs** → **Create key pair**: name `screener-key`, type **ED25519**, format **.pem** → Create.
Move the downloaded file to `C:\Users\Muneeb\.ssh\screener-key.pem`, then lock it down (PowerShell):
```powershell
icacls "$env:USERPROFILE\.ssh\screener-key.pem" /inheritance:r /grant:r "$($env:USERNAME):R"
```

## Step 2: S3 bucket for backups
S3 → **Create bucket**: name e.g. `screener-backups-<yourname>` (must be globally unique), region
Mumbai, **Block all public access ON** → Create.
Open the bucket → **Management** → **Create lifecycle rule**: name `expire-old-backups`, apply to
all objects → *Expire current versions of objects* after **14 days** → Create.

## Step 3: IAM role (lets the server upload backups without storing keys)
IAM → **Roles** → **Create role** → *AWS service* → **EC2** → Next → skip policies → name
`screener-ec2-role` → Create. Open the role → **Add permissions → Create inline policy → JSON**:
```json
{ "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow", "Action": ["s3:PutObject"],
                  "Resource": "arn:aws:s3:::screener-backups-<yourname>/postgres/*" }] }
```
Name it `backup-upload-only` → Create. (It can only *add* backups. It can't read or delete anything.)

## Step 4: Security group (firewall)
EC2 → **Security groups** → **Create**: name `screener-sg`. Inbound rules:
| Type | Port | Source |
|---|---|---|
| SSH | 22 | **My IP** (only your current internet connection) |
| HTTP | 80 | Anywhere-IPv4 (Let's Encrypt needs it) |
| HTTPS | 443 | Anywhere-IPv4 |

If your home IP changes later, edit the SSH rule back to "My IP".

## Step 5: Launch the server
EC2 → **Launch instance**:
- Name `ai-screener`, image **Ubuntu Server 24.04 LTS**, type **t3.small**
- Key pair **screener-key**, security group **screener-sg** (*Select existing*)
- Storage **30 GiB gp3**
- *Advanced details* → **IAM instance profile** = `screener-ec2-role`
- **Launch**

Then **Elastic IPs** → **Allocate** → select it → **Associate** with `ai-screener`.
Note the IP (e.g. `13.233.x.x`): it stays the same across reboots.

## Step 6: Free domain (DuckDNS)
https://www.duckdns.org → sign in with Google → choose a subdomain (e.g. `muneeb-screener`) → put
the **Elastic IP** in *current ip* → **update ip**. Your domain: `muneeb-screener.duckdns.org`.

## Step 7: Prepare the server
```powershell
$ip = "13.233.x.x"   # your Elastic IP
$key = "$env:USERPROFILE\.ssh\screener-key.pem"
scp -i $key deploy\setup-ec2.sh ubuntu@${ip}:/tmp/
ssh -i $key ubuntu@$ip "sudo bash /tmp/setup-ec2.sh"
```
(Answer `yes` to the first-connection fingerprint question.)

## Step 8: Move your system to the server
**8a. On your PC, build the bundle** (code + both databases + secrets + production `.env`):
```powershell
.\deploy\export-local.ps1     # asks for your domain, email and the S3 bucket name
```
Open `deploy\bundle\.env.server` once and check DOMAIN / PUBLIC_BASE_URL look right.

**8b. Stop the local stack.** Otherwise your PC *and* the server would both read the Sheet and
invite candidates twice:
```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile tunnel down
```

**8c. Upload and start**:
```powershell
scp -i $key -r deploy\bundle ubuntu@${ip}:/opt/screener/
ssh -i $key ubuntu@$ip
```
Then on the server:
```bash
cd /opt/screener
tar -xzf bundle/app.tar.gz
cp bundle/.env.server .env && chmod 600 .env
cp -r bundle/secrets ./secrets && chmod -R go-rwx secrets
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C up -d postgres                       # database first (creates n8n + screener DBs)
sleep 15
bash deploy/restore.sh bundle/n8n_local.dump bundle/screener_local.dump   # also starts everything
rm -rf bundle                           # remove the copies of secrets/dumps
```
The first build of the API image takes a few minutes. Watch with `$C ps` until everything is `healthy`.

## Step 9: Check it from anywhere
```powershell
Invoke-RestMethod https://muneeb-screener.duckdns.org/health     # status ok, db ok
```
A green padlock in the browser means Caddy got the HTTPS certificate.

## Step 10: Point Vapi at the server
On the server (the scripts only need Python's standard library):
```bash
cd /opt/screener && python3 scripts/vapi.py sync     # end-of-call webhook -> https://<domain>/webhook/...
```

## Step 11: The n8n editor (private, through SSH)
```powershell
ssh -i $key -L 5678:localhost:5678 ubuntu@$ip
```
Leave that window open and browse **http://localhost:5678**. It's the server's n8n, with your
workflows and Google connections already there (restored with the same encryption key).

## Step 12: Nightly backups + everyday operations
On the server:
```bash
chmod +x /opt/screener/deploy/backup.sh
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/screener/deploy/backup.sh >> /opt/screener/backups/backup.log 2>&1") | crontab -
/opt/screener/deploy/backup.sh      # run once now; check the S3 bucket for the files
```
| Task | Command (on the server, in /opt/screener) |
|---|---|
| Status | `docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` |
| Logs | `… logs -f screener-api` |
| Restart | `… --profile prod restart` |
| Update code | new bundle → `tar -xzf` → `… --profile prod up -d --build` (Phase 7 automates this) |

**Pause to save money:** EC2 → select the instance → **Instance state → Stop**. Start it again
later: the Elastic IP and domain stay the same, and Docker starts everything automatically.
**Delete everything:** terminate the instance, release the Elastic IP, empty and delete the bucket.
