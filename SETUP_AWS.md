# AWS Setup Guide — Control de Calidad

Follow these steps once before your first production deployment.
Everything is done in the AWS Console — no command line needed for setup.

---

## Step 1 — Create the S3 Bucket (photo storage)

1. Go to **AWS Console → S3 → Create bucket**
2. **Bucket name**: `qc-fruto-photos` (or any name you like — write it down)
3. **Region**: choose the same region as your Elastic Beanstalk app (e.g. `us-east-1`)
4. Leave all other settings as default → **Create bucket**

> Photos are served via secure, temporary signed URLs — the bucket stays private.

---

## Step 2 — Create the DynamoDB Tables (database)

You need two tables. Go to **AWS Console → DynamoDB → Tables → Create table** for each.

### Table 1 — Reports
| Setting | Value |
|---|---|
| Table name | `qc_reports` |
| Partition key | `id` (String) |
| Sort key | *(leave empty)* |
| Table settings | Default settings (On-demand capacity) |

### Table 2 — Photos
| Setting | Value |
|---|---|
| Table name | `qc_photos` |
| Partition key | `report_id` (String) |
| Sort key | `id` (String) |
| Table settings | Default settings (On-demand capacity) |

Click **Create table** for each. Wait ~30 seconds until both show status **Active**.

---

## Step 3 — Grant Elastic Beanstalk Permission to Use S3 & DynamoDB

Your EB app runs under an IAM role. You need to add permissions to it.

1. Go to **AWS Console → IAM → Roles**
2. Search for `aws-elasticbeanstalk-ec2-role` → click it
3. Click **Add permissions → Attach policies**
4. Search for and attach:
   - `AmazonS3FullAccess` *(or create a narrower policy — see note below)*
   - `AmazonDynamoDBFullAccess` *(or a narrower policy)*
5. Click **Add permissions**

> **Narrower policy (recommended for production):** Instead of full access, you can create
> a custom policy that only allows access to your specific bucket and two tables.
> Ask Claude to generate this policy when you're ready.

---

## Step 4 — Set Environment Variables in Elastic Beanstalk

1. Go to **AWS Console → Elastic Beanstalk → your environment**
2. Click **Configuration → Updates, monitoring, and logging → Edit**
3. Scroll to **Environment properties** and add:

| Key | Value |
|---|---|
| `S3_BUCKET` | `qc-fruto-photos` *(your bucket name from Step 1)* |
| `DYNAMO_REPORTS_TABLE` | `qc_reports` |
| `DYNAMO_PHOTOS_TABLE` | `qc_photos` |
| `AWS_DEFAULT_REGION` | `us-east-1` *(your region)* |

4. Click **Apply** — EB will restart automatically.

---

## Step 5 — Deploy the App

From your project folder in Terminal (with venv active):

```bash
# Zip everything except local dev files
zip -r qc_app.zip . \
  --exclude "*.db" \
  --exclude "uploads/*" \
  --exclude "venv/*" \
  --exclude "__pycache__/*" \
  --exclude "*.pyc" \
  --exclude ".git/*"
```

Then upload `qc_app.zip` in the EB console:
**Elastic Beanstalk → your environment → Upload and deploy**

---

## Local Development

No AWS setup needed locally. The app detects that `S3_BUCKET` is not set and
automatically uses SQLite + local file storage:

```bash
source venv/bin/activate
python application.py
# Open http://localhost:8080
```

---

## Cost Estimate

For typical quality control usage (a few inspections per day):

| Service | Estimated cost |
|---|---|
| S3 storage (1 GB photos) | ~$0.02/month |
| S3 requests | ~$0.01/month |
| DynamoDB (on-demand) | < $1/month |
| **Total** | **< $2/month** |
