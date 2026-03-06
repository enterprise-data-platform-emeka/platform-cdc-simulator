# Cloud Setup Guide

This guide walks through everything I need to do to run the CDC (Change Data Capture) simulator against a real AWS (Amazon Web Services) environment instead of a local Docker database.

By the end I'll have:

- A PostgreSQL database running on AWS RDS (Relational Database Service — Amazon's managed database product)
- The simulator writing real e-commerce data into that database from my Mac
- AWS DMS (Database Migration Service) watching every change and forwarding it to S3 (Simple Storage Service) as Parquet files
- Everything torn down and deleted after testing so I'm not paying for idle resources

This guide assumes `local_setup_guide.md` is complete and the simulator already works locally. The AWS infrastructure is defined in the [terraform-platform-infra-live](https://github.com/enterprise-data-platform-emeka/terraform-platform-infra-live) repository.

---

## Read this before starting: the three terminals

This guide uses three separate Terminal windows at the same time. Managing them without clear names is confusing. I'll name each one before using it so I always know where I am.

Here is the full picture of all three terminals and when each one is open:

```
  TERMINAL 1 "Terraform"          TERMINAL 2 "Tunnel"           TERMINAL 3 "Simulator"
  ─────────────────────────       ──────────────────────        ────────────────────────
  Open from the start             Open in Part 4                Open in Part 5

  - install tool checks           - run the SSM tunnel           - make schema ENV=dev
  - terraform init/plan/apply     - leave it running,            - make seed ENV=dev
  - reboot RDS                    do NOT close this              - make simulate ENV=dev
  - start DMS task                terminal until
  - check S3                      teardown
  - terraform destroy
```

I open Terminal 1 right now and keep it open throughout. Terminal 2 and Terminal 3 are opened later — the guide tells me exactly when.

---

## How to name a Terminal tab on Mac

Before I do anything else, I'll open Terminal 1 and give it a name so I can find it easily later.

**If I'm using the built-in macOS Terminal app:**
1. Open Terminal (press `Cmd + Space`, type `Terminal`, press Enter)
2. Press `Cmd + T` to open a new tab (or use the existing window)
3. In the menu bar at the top of the screen, click `Shell` → `Edit Title...`
4. Type `Terraform` and click OK
5. The tab at the top of the Terminal window now shows `Terraform`

**If I'm using iTerm2:**
1. Open iTerm2
2. Press `Cmd + T` to open a new tab
3. Double-click the tab name at the top of the window
4. Type `Terraform` and press Enter

**Alternative that works in both apps** — paste this command into the terminal after opening it:
```bash
printf '\033]0;Terraform\007'
```
This changes the tab title to "Terraform" immediately.

I'll do the same thing for Terminal 2 and Terminal 3 when I open them later. The guide reminds me each time.

---

## What is actually being built in AWS

Before running any commands, it helps to understand what Terraform is going to create. Everything lives inside a VPC (Virtual Private Cloud — a private, isolated section of AWS that only I own and control):

```
  My Mac (running the simulator)
    │
    │  SSM Tunnel — a secure relay through AWS's own network
    │  (my Mac connects to localhost:5433, the tunnel secretly
    │   forwards that to the database inside the VPC)
    │
    ▼
  EC2 Bastion — a tiny server in the public part of the VPC
  (this is the relay point the tunnel goes through)
    │
    │  port 5432 — PostgreSQL's port, only reachable inside the VPC
    │
    ▼
  RDS PostgreSQL — the real database (private, no internet access)
  (the simulator writes customers, orders, shipments here)
    │
    │  WAL stream — PostgreSQL's internal diary of every change
    │
    ▼
  DMS Replication Instance — reads the WAL, converts changes to files
    │
    │  writes Parquet files every few seconds
    │
    ▼
  S3 Bronze Bucket — the raw data landing zone
  (this is where the data pipeline begins)
```

**Why can't I connect directly from my Mac to the database?**

RDS lives in a private subnet — a part of the VPC that has no route to the internet. This is a security decision: a database that the whole internet can see is a target for attack. The only way in from outside is through the SSM tunnel, which goes through AWS's own internal systems. I don't need to open any ports or create any SSH keys.

---

## Estimated cost for this test

This test costs between $2 and $8 USD depending on how long I leave it running:

| Resource | Cost |
|---|---|
| RDS db.t3.micro | ~$0.02/hour |
| DMS dms.t3.medium | ~$0.10/hour |
| EC2 t3.micro bastion | ~$0.01/hour |
| Redshift Serverless | ~$0 if I don't query it during the test |
| S3 storage | Negligible for a test |

The cost of a 2-hour test is roughly $0.50. I keep it low by destroying everything immediately after.

---

## Part 1: One-time tool checks

> **All steps in this part run in: Terminal 1 (Terraform)**

I only do this part once. After these tools are confirmed working, I never need to repeat these checks.

---

### Step 1: Open and name Terminal 1

I open a new terminal window and name it `Terraform` using one of the methods above.

I confirm the name is showing in the tab before moving on.

---

### Step 2: Check that the AWS CLI is installed

**In Terminal 1 (Terraform), run:**
```bash
aws --version
```

The AWS CLI (Command Line Interface) is a tool that lets me control AWS from the terminal — checking database status, listing S3 files, starting tasks — without clicking in a web browser.

**Expected output:**
```
aws-cli/2.x.x Python/3.x.x Darwin/...
```

If I see `command not found: aws`, I install it:
```bash
brew install awscli
```
Then run `aws --version` again to confirm.

---

### Step 3: Check that my AWS credentials are configured

The project uses AWS SSO (Single Sign-On) for authentication. Instead of long-lived access keys, SSO issues short-lived temporary credentials that expire automatically. The named profile `dev-admin` is configured to use SSO and targets the dev AWS account.

**In Terminal 1 (Terraform), log in first:**
```bash
aws sso login --profile dev-admin
```

This opens a browser window asking me to confirm the login. After approving, the CLI has temporary credentials valid for a few hours.

**Then verify it worked:**
```bash
aws sts get-caller-identity --profile dev-admin
```

This asks AWS "who am I?" using the `dev-admin` SSO session. It's harmless — it just confirms the credentials are active.

**Expected output:**
```json
{
    "UserId": "AROA...:my-username",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_dev-admin.../my-username"
}
```

I note down the 12-digit `Account` number — I'll need it in Part 7 to check the S3 bucket.

**If I see** `The config profile (dev-admin) could not be found`, the SSO profile hasn't been configured yet. I set it up with:
```bash
aws configure sso --profile dev-admin
```

It asks:
- **SSO session name** — any name, for example `edp-dev`
- **SSO start URL** — the URL for my organisation's AWS SSO portal (from whoever set up the AWS accounts)
- **SSO region** — `eu-central-1`
- **SSO registration scopes** — press Enter to accept the default
- **AWS account ID** — the dev account ID
- **Role name** — the SSO permission set name (for example `AdministratorAccess`)
- **Default region** — `eu-central-1`
- **Default output format** — `json`

After configuring, I run `aws sso login --profile dev-admin`, approve in the browser, then run `aws sts get-caller-identity --profile dev-admin` to confirm it works.

---

### Step 4: Check that Terraform is installed

Terraform is the tool that reads the infrastructure code (the `.tf` files) and creates the actual AWS resources. It turns code into real cloud infrastructure.

**In Terminal 1 (Terraform), run:**
```bash
terraform --version
```

**Expected output:**
```
Terraform v1.x.x
```

**If I see** `command not found: terraform`, I install it:
```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
```

Then run `terraform --version` again to confirm.

---

### Step 5: Install the SSM Session Manager plugin

SSM (Systems Manager) Session Manager is how I'll connect my Mac to the private RDS database — through a secure tunnel with no SSH keys and no open ports.

The SSM plugin is a small helper tool that the AWS CLI needs to create this tunnel. The AWS CLI alone cannot do it without this plugin.

**In Terminal 1 (Terraform), run:**
```bash
session-manager-plugin --version
```

**Expected output:**
```
1.2.x.0
```

**If I see** `command not found: session-manager-plugin`, I install it:
```bash
brew install --cask session-manager-plugin
```

Then run `session-manager-plugin --version` again to confirm.

---

## Part 2: Apply the AWS infrastructure

> **All steps in this part run in: Terminal 1 (Terraform)**

This part creates everything in AWS. It takes about 15 to 20 minutes because RDS takes time to provision. I do this once per test session. All the `.tf` files being applied are in the [terraform-platform-infra-live](https://github.com/enterprise-data-platform-emeka/terraform-platform-infra-live) repository — I can open it in a browser to read any resource definition while following these steps.

---

### Step 6: Navigate to the Terraform folder

**In Terminal 1 (Terraform), run:**
```bash
cd /Users/chuquemeka/enterprise-data-platform/terraform-platform-infra-live
```

`cd` means "change directory". All Terraform commands must be run from this folder. This is the local clone of the [terraform-platform-infra-live](https://github.com/enterprise-data-platform-emeka/terraform-platform-infra-live) repository.

I verify I'm in the right place:
```bash
pwd
```

**Expected output:**
```
/Users/chuquemeka/enterprise-data-platform/terraform-platform-infra-live
```

---

### Step 7: Set the secret passwords

Two passwords must be provided before Terraform runs. I never put passwords in `.tf` files because those go into git. Instead I set them as temporary environment variables — they exist only in the current Terminal session and disappear when I close the window.

**In Terminal 1 (Terraform), run both lines (one at a time):**
```bash
export TF_VAR_db_password="MyRdsPassword123!"
```
```bash
export TF_VAR_redshift_admin_password="MyRedshiftPass123!"
```

`export` makes a variable available to any command I run in this terminal session. `TF_VAR_` is a prefix that Terraform automatically recognises and maps to its own variables.

Password rules (both passwords must follow these):
- At least 8 characters
- At least one uppercase letter, one lowercase letter, one number
- No `@`, `/`, `"`, or space characters — these break the database connection string

I'll need the `db_password` value again later (in Step 17 when I update `.env`), so I pick something I can remember for this session.

To confirm the variables are set:
```bash
echo $TF_VAR_db_password
```

It should print the password I just typed. If it prints nothing, I run the export commands again.

---

### Step 8: Initialise Terraform

**In Terminal 1 (Terraform), run:**
```bash
make init dev
```

`make init dev` is a shortcut that runs `terraform init` inside the `environments/dev/` folder. Initialising does two things:
1. Downloads the AWS provider plugin — the code that translates Terraform's language into AWS API calls
2. Connects to the remote state backend — an S3 bucket where Terraform stores a record of everything it creates (so it doesn't create duplicates next time)

**Expected output ends with:**
```
Terraform has been successfully initialized!
```

**If I see an error about an S3 bucket not existing**, the bootstrap step hasn't been run yet. The bootstrap is a one-time setup in the `terraform-bootstrap/` folder of the [terraform-platform-infra-live](https://github.com/enterprise-data-platform-emeka/terraform-platform-infra-live) repository that creates the state storage bucket. That's covered separately.

---

### Step 9: Review the plan

**In Terminal 1 (Terraform), run:**
```bash
make plan dev
```

`make plan dev` runs `terraform plan` — a dry run. Terraform figures out exactly what it would create, change, or destroy, and prints the list. Nothing actually happens yet.

The output shows a list of resources with symbols:
- `+` — will be created (I should see mostly these for a fresh environment)
- `~` — will be changed
- `-` — will be destroyed

Key resources I should see marked with `+`:
- `aws_db_instance.source` — the RDS PostgreSQL database
- `aws_dms_replication_instance.this` — the DMS engine
- `aws_dms_replication_task.cdc` — the CDC task
- `aws_instance.bastion` — the EC2 relay server
- `aws_security_group.bastion` — the firewall rules for the bastion
- Various S3 buckets, IAM roles, KMS keys

At the bottom of the plan output I'll see a summary line like:
```
Plan: 47 to add, 0 to change, 0 to destroy.
```

If the plan looks correct, I move to Step 10. If I see any `-` (destroy) lines that don't make sense, I stop and investigate before applying.

---

### Step 10: Apply — create everything in AWS

**In Terminal 1 (Terraform), run:**
```bash
make apply dev
```

Terraform asks for confirmation before doing anything:
```
Do you want to perform these actions?
  Terraform will perform the actions described above.
  Only 'yes' will be accepted to approve.

  Enter a value:
```

I type `yes` and press Enter.

Terraform now creates all the resources. I'll see lines appearing as each resource is created:
```
aws_vpc.this: Creating...
aws_vpc.this: Creation complete after 2s
aws_db_instance.source: Creating...
aws_db_instance.source: Still creating... [1m0s elapsed]
aws_db_instance.source: Still creating... [2m0s elapsed]
...
aws_db_instance.source: Creation complete after 13m42s
```

**I leave Terminal 1 alone and wait.** The apply takes about 15 to 20 minutes. RDS takes the longest.

When it finishes, Terraform prints the outputs I defined. **The most important one to copy is `ssm_tunnel_command` — I'll need it in Step 14 to open the secure tunnel:**

```
Outputs:

bastion_instance_id = "i-0abc123def456789"

rds_endpoint = "edp-dev-source-db.abc123.eu-central-1.rds.amazonaws.com"

simulator_env_block = <<EOT
    DB_HOST=localhost
    DB_PORT=5433
    DB_NAME=ecommerce
    DB_USER=postgres
    DB_PASSWORD=<the password you set in TF_VAR_db_password>
EOT

ssm_tunnel_command = "aws ssm start-session --target i-0abc123def456789 --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters 'host=edp-dev-source-db.abc123.eu-central-1.rds.amazonaws.com,portNumber=5432,localPortNumber=5433' --profile dev-admin"
```

**Note on `simulator_env_block`:** This output is now informational only. I do not need to paste it into `.env`. The Makefile automatically handles the connection settings when I run `make schema ENV=dev` — it fetches the database password from AWS SSM (Systems Manager) Parameter Store and sets all variables at runtime. No file editing is required.

If I missed the outputs, I can print them again:
```bash
cd terraform-platform-infra-live/environments/dev && terraform output
```

---

## Part 3: Activate the database

> **All steps in this part run in: Terminal 1 (Terraform)**

RDS is now running, but one configuration change only takes effect after a reboot. I need to do that before DMS can read the database's change log.

**Why this reboot is needed:** The Terraform configuration sets a PostgreSQL parameter called `rds.logical_replication = 1`. This tells PostgreSQL to write enough detail in its WAL (Write-Ahead Log — the internal diary that records every INSERT, UPDATE, and DELETE) so that DMS can read it. PostgreSQL requires a restart to activate this setting, just like how some computer settings only take effect after a reboot.

---

### Step 11: Reboot the RDS database

**In Terminal 1 (Terraform), run:**
```bash
aws rds reboot-db-instance \
  --db-instance-identifier edp-dev-source-db \
  --profile dev-admin
```

This command asks AWS to restart the PostgreSQL process inside RDS. The command returns immediately, but the actual reboot takes about 2 minutes.

**Expected output:**
```json
{
    "DBInstance": {
        "DBInstanceIdentifier": "edp-dev-source-db",
        "DBInstanceStatus": "rebooting",
        ...
    }
}
```

Seeing `"rebooting"` confirms the reboot started.

---

### Step 12: Wait for the database to come back online

I need to wait until the database is fully back up before connecting to it. I check its status by running this command every 30 seconds:

**In Terminal 1 (Terraform), run:**
```bash
aws rds describe-db-instances \
  --db-instance-identifier edp-dev-source-db \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text \
  --profile dev-admin
```

What this command does:
- `describe-db-instances` — asks AWS for all details about this RDS instance
- `--query 'DBInstances[0].DBInstanceStatus'` — filters the result to show only the status field (without this, it returns pages of JSON)
- `--output text` — shows the status as a plain word, not JSON

During the reboot it prints `rebooting`. When it prints `available`, the database is ready.

**I keep running this command every 30 seconds until I see:**
```
available
```

This usually takes 1 to 2 minutes.

---

## Part 4: Open the SSM tunnel

> **Steps in this part open a new Terminal 2 named "Tunnel"**

The SSM tunnel is the secure bridge between my Mac and the private RDS database. It works like this: my Mac connects to `localhost:5433`, the SSM plugin picks that up, encrypts it, sends it to the bastion server inside the VPC, and the bastion forwards it to RDS on port 5432. To RDS, the connection looks like it came from inside the VPC.

**The tunnel must stay open the entire time the simulator is running.** It blocks the terminal — meaning I can't type other commands in Terminal 2 while the tunnel is running. That's why it gets its own dedicated terminal.

---

### Step 13: Open Terminal 2 and name it "Tunnel"

I open a brand new Terminal window or tab.

**To open a new tab in the current Terminal window:** press `Cmd + T`

**To name it "Tunnel"** — I run this command in the new terminal:
```bash
printf '\033]0;Tunnel\007'
```

I can verify the tab is named "Tunnel" by looking at the tab at the top of the Terminal window.

---

### Step 14: Run the SSM tunnel command

**In Terminal 2 (Tunnel), run the `ssm_tunnel_command` that Terraform printed in Step 10.**

It looks like this — but with my actual values filled in (not the example below):
```bash
aws ssm start-session \
  --target i-0abc123def456789 \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters 'host=edp-dev-source-db.abc123.eu-central-1.rds.amazonaws.com,portNumber=5432,localPortNumber=5433' \
  --profile dev-admin
```

Breaking this command down:
- `ssm start-session` — opens a connection through SSM (no SSH key or open port needed)
- `--target i-0abc...` — the bastion EC2 instance ID (the relay server inside the VPC)
- `--document-name AWS-StartPortForwardingSessionToRemoteHost` — the type of session: forward a port to a remote host (not just open a shell)
- `--parameters 'host=...,portNumber=5432,localPortNumber=5433'` — "take whatever arrives on my Mac's port 5433 and forward it to the RDS address on port 5432"
- `--profile dev-admin` — use my dev AWS credentials

**Expected output:**
```
Starting session with SessionId: dev-admin-0abc123...
Port 5433 opened for sessionId dev-admin-0abc123...
Waiting for connections...
```

`Waiting for connections...` means the tunnel is open and ready. **I leave Terminal 2 exactly as it is and do not touch it.** I switch back to Terminal 1 or open Terminal 3 for the next steps.

**If I see an error like** `An error occurred (TargetNotConnected)`, the bastion EC2 instance hasn't finished registering with SSM yet — it needs a minute or two after first boot. I wait 2 minutes and try the command again.

---

## Part 5: Set up the simulator for AWS

> **Steps in this part open a new Terminal 3 named "Simulator"**

No `.env` editing is needed for AWS environments. The Makefile handles all connection settings automatically when `ENV=dev` is passed.

---

### Step 15: Open Terminal 3 and name it "Simulator"

I open another new Terminal tab.

**Press `Cmd + T`** to open a new tab.

**Name it "Simulator":**
```bash
printf '\033]0;Simulator\007'
```

I now have three named tabs visible:

```
[Terraform]  [Tunnel]  [Simulator]
```

---

### Step 16: Navigate to the simulator folder

**In Terminal 3 (Simulator), run:**
```bash
cd /Users/chuquemeka/enterprise-data-platform/platform-cdc-simulator
```

---

### Step 17: How the simulator connects to AWS (no .env editing needed)

When I run any simulator command with `ENV=dev`, the Makefile does all of this automatically:

1. Sets `DB_HOST=localhost` and `DB_PORT=5433` — the local end of the SSM tunnel in Terminal 2
2. Calls `aws ssm get-parameter --name /edp/dev/rds/db_password --with-decryption --profile dev-admin` to fetch the RDS password from SSM (Systems Manager) Parameter Store. Terraform stored this password there when I ran `make apply dev` in Step 10.
3. Exports all other variables (`DB_NAME=ecommerce`, `DB_USER=postgres`, etc.) inline
4. Runs the simulator with all of those as environment variables

The `.env` file is left completely untouched. I do not edit it. After this test session, `.env` still points at local Docker — ready for local development immediately.

**To verify the SSM parameter exists (optional check):**

**In Terminal 3 (Simulator), run:**
```bash
aws ssm get-parameter \
  --name /edp/dev/rds/db_password \
  --with-decryption \
  --query Parameter.Value \
  --output text \
  --profile dev-admin
```

**Expected output:** the RDS password that was set in Step 7. If this command succeeds, the Makefile can fetch it.

---

## Part 6: Run the simulator

> **All steps in this part run in: Terminal 3 (Simulator)**

---

### Step 18: Create the database schema

This creates the six tables, indexes, triggers, and sets `REPLICA IDENTITY FULL` on every table. `REPLICA IDENTITY FULL` tells PostgreSQL to write the full old row into the WAL on every update — DMS needs this to know what changed.

**In Terminal 3 (Simulator), run:**
```bash
make schema ENV=dev
```

`ENV=dev` tells the Makefile to connect to the AWS dev environment. It sets `DB_PORT=5433` (the SSM tunnel), fetches the password from SSM Parameter Store using the `dev-admin` AWS profile, and runs the command. I do not need to touch `.env`.

**Expected output:**
```
2026-03-06 10:00:00  INFO  __main__  Environment: dev | Max orders: 5000 | ...
2026-03-06 10:00:00  INFO  simulator.db  Connecting to PostgreSQL at localhost:5433/ecommerce
2026-03-06 10:00:01  INFO  simulator.db  Connected to PostgreSQL
2026-03-06 10:00:01  INFO  __main__  Applying schema
2026-03-06 10:00:02  INFO  __main__  Schema applied successfully
```

The log shows `localhost:5433` — that's correct. The SSM tunnel in Terminal 2 is transparently forwarding that connection to RDS. The data is actually going to AWS.

**If I see** `could not connect to server: Connection refused`, the SSM tunnel in Terminal 2 is not running. I check Terminal 2 — if it shows an error or the cursor is back, I go back to Step 14 and reopen the tunnel.

---

### Step 19: Seed historical data

This fills the RDS database with 2 years of historical customers, products, and orders before the live simulation starts.

**In Terminal 3 (Simulator), run:**
```bash
make seed ENV=dev
```

**Expected output:**
```
2026-03-06 10:00:10  INFO  simulator.seed  Seeding: 500 customers, 200 products, 2000 historical orders
2026-03-06 10:00:10  INFO  simulator.seed  Seeding 500 customers
2026-03-06 10:00:11  INFO  simulator.seed  Inserted 500 customers
2026-03-06 10:00:11  INFO  simulator.seed  Seeding 200 products
2026-03-06 10:00:11  INFO  simulator.seed  Inserted 200 products
2026-03-06 10:00:12  INFO  simulator.seed  Seeding 2000 historical orders
2026-03-06 10:00:25  INFO  simulator.seed  Inserted 2000 historical orders (with items, payments, shipments)
2026-03-06 10:00:25  INFO  simulator.seed  Seeding complete
```

---

## Part 7: Start DMS and verify data in S3

> **Steps 20 and 21 run in: Terminal 1 (Terraform)**
> **Step 22 runs in: Terminal 3 (Simulator)**
> **Steps 23 and 24 run in: Terminal 1 (Terraform)**

---

### Step 20: Get the DMS task ARN

I need the DMS task's ARN (Amazon Resource Name — a unique identifier for AWS resources) to start it. I switch to Terminal 1 to run this.

**In Terminal 1 (Terraform), run:**
```bash
aws dms describe-replication-tasks \
  --query 'ReplicationTasks[?ReplicationTaskIdentifier==`edp-dev-cdc-task`].ReplicationTaskArn' \
  --output text \
  --profile dev-admin
```

What this command does:
- `describe-replication-tasks` — lists all DMS tasks in my account
- `--query '...[?ReplicationTaskIdentifier==...]'` — filters the list to find only the task named `edp-dev-cdc-task`
- `.ReplicationTaskArn` — extracts just the ARN from the result

**Expected output:**
```
arn:aws:dms:eu-central-1:123456789012:task:ABCDEF123456
```

I copy this ARN — I need it in the next step.

---

### Step 21: Start the DMS replication task

**In Terminal 1 (Terraform), run — replacing the ARN with the one I just copied:**
```bash
aws dms start-replication-task \
  --replication-task-arn arn:aws:dms:eu-central-1:123456789012:task:ABCDEF123456 \
  --start-replication-task-type start-replication \
  --profile dev-admin
```

What this command does:
- `start-replication-task` — tells DMS to begin
- `--start-replication-task-type start-replication` — start fresh from the beginning

DMS now does two things in sequence:
1. **Full load** — reads all 2,000+ rows I just seeded and writes them as Parquet files to S3
2. **CDC mode** — switches to watching the WAL for new changes in real time

The command returns immediately. DMS runs in the background.

**I check the task status:**
```bash
aws dms describe-replication-tasks \
  --query 'ReplicationTasks[?ReplicationTaskIdentifier==`edp-dev-cdc-task`].Status' \
  --output text \
  --profile dev-admin
```

Status progresses in this order:
- `starting` — DMS is initialising
- `full-load` — DMS is reading all existing rows from RDS (takes a few minutes)
- `sync` — DMS switched to CDC mode, now watching for new changes in real time

I run this check every 30 seconds until I see `sync`. Only then is DMS fully capturing live changes.

---

### Step 22: Start the live simulation

Now I go back to Terminal 3 to start the continuous order loop. DMS will capture every write the simulator makes.

**In Terminal 3 (Simulator), run:**
```bash
make simulate ENV=dev
```

**Expected output (lines appear every 2 seconds):**
```
2026-03-06 10:05:00  INFO  simulator.simulate  Simulator starting — environment limit: 5000 orders...
2026-03-06 10:05:02  INFO  simulator.simulate  New order 2001 placed for customer 147 (total: 89.97)
2026-03-06 10:05:04  INFO  simulator.simulate  Order 1843: processing → shipped
2026-03-06 10:05:20  INFO  simulator.simulate  [tick 5] customers=501  orders=2003  items=6095 ...
```

I let this run for a few minutes to generate some CDC events. I do NOT press Ctrl+C yet — I go to Terminal 1 to check S3 first.

---

### Step 23: Find my AWS account ID

I need the account ID to construct the S3 bucket name. **I switch to Terminal 1 (Terraform) to run this.**

**In Terminal 1 (Terraform), run:**
```bash
aws sts get-caller-identity --query Account --output text --profile dev-admin
```

**Expected output:**
```
123456789012
```

I copy this number.

---

### Step 24: Check that Parquet files have appeared in S3

**In Terminal 1 (Terraform), run — replacing `123456789012` with my actual account ID:**
```bash
aws s3 ls s3://edp-dev-123456789012-bronze/raw/ --recursive --profile dev-admin
```

What this command does:
- `s3 ls` — lists the contents of an S3 bucket
- `--recursive` — goes into all sub-folders and lists everything
- The bucket name is `edp-dev-<account-id>-bronze`

**Expected output:**
```
2026-03-06 10:02:15       4521  raw/ecommerce/customers/20260306/LOAD00000001.parquet
2026-03-06 10:02:18       8234  raw/ecommerce/orders/20260306/LOAD00000001.parquet
2026-03-06 10:02:31       1203  raw/ecommerce/orders/20260306/20260306-100200-0001.parquet
2026-03-06 10:02:32        987  raw/ecommerce/order_items/20260306/20260306-100201-0001.parquet
```

The files named `LOAD00000001.parquet` are from the full load — the initial snapshot of all seeded data. Files with timestamps like `20260306-100200-0001.parquet` are CDC events — each one is a batch of changes the simulator made in real time.

If the folder is empty, DMS is still running the full load. I wait another minute and run the command again.

**Seeing these files means the full pipeline is working:**
```
Simulator → RDS → WAL → DMS → S3 Bronze Bucket
```

---

## Part 8: Tear down

> **This part uses all three terminals. The guide is specific about which step uses which.**

Tearing down is the most important part. Running AWS resources costs money even when idle. I destroy everything immediately after testing.

---

### Step 25: Stop the simulator

**I click on Terminal 3 (Simulator)** where `make simulate ENV=dev` is still running.

**In Terminal 3 (Simulator), press:** `Ctrl + C`

The simulator stops:
```
2026-03-06 10:15:00  INFO  simulator.simulate  Simulator stopped after 150 ticks
```

Terminal 3 (Simulator) now shows a regular prompt (`$`). I do not need to restore `.env` — it was never changed during this session. Terminal 3 is done.

---

### Step 26: Close the SSM tunnel

**I click on Terminal 2 (Tunnel)** where the tunnel is still showing `Waiting for connections...`

**In Terminal 2 (Tunnel), press:** `Ctrl + C`

The tunnel closes:
```
Closing session with SessionId: dev-admin-0abc123...
Exiting session with sessionId dev-admin-0abc123...
```

Terminal 2 (Tunnel) now shows a regular prompt. The tunnel is fully closed.

---

### Step 27: Destroy the AWS infrastructure

Now I go back to Terminal 1 to destroy everything Terraform created. Terraform reads the same [terraform-platform-infra-live](https://github.com/enterprise-data-platform-emeka/terraform-platform-infra-live) code it used to apply and deletes every resource it built.

**I click on Terminal 1 (Terraform).**

**In Terminal 1 (Terraform), run:**
```bash
cd /Users/chuquemeka/enterprise-data-platform/terraform-platform-infra-live
make destroy dev
```

Terraform asks for confirmation:
```
Do you really want to destroy all resources?
  Terraform will destroy all your managed infrastructure, as shown above.
  There is no undo. Only 'yes' will be accepted to confirm.

  Enter a value:
```

I type `yes` and press Enter.

Terraform now deletes every resource it created. I'll see lines like:
```
aws_dms_replication_task.cdc: Destroying...
aws_dms_replication_task.cdc: Destruction complete after 8s
aws_db_instance.source: Destroying...
aws_db_instance.source: Still destroying... [5m0s elapsed]
aws_db_instance.source: Destruction complete after 7m12s
```

When it finishes:
```
Destroy complete! Resources: 47 destroyed.
```

---

### Step 28: Verify everything is gone

**In Terminal 1 (Terraform), run:**
```bash
aws rds describe-db-instances \
  --query 'DBInstances[*].DBInstanceIdentifier' \
  --output text \
  --profile dev-admin
```

**Expected output:** nothing (empty). This confirms the RDS database no longer exists and I'm no longer being charged for it.

---

## Part 9: Troubleshooting

### "could not connect to server: Connection refused" during make schema or make seed

**Terminal to check: Terminal 2 (Tunnel)**

The SSM tunnel is not running. I click on Terminal 2. If the tunnel is still showing `Waiting for connections...`, it's fine — the issue is something else. If Terminal 2 shows an error or a regular prompt (`$`), the tunnel died. I go back to Step 14 and reopen it.

---

### "An error occurred (TargetNotConnected)" when opening the SSM tunnel

**Terminal this happens in: Terminal 2 (Tunnel)**

The bastion EC2 instance hasn't finished starting up yet. The SSM agent on the bastion needs a minute or two after first boot to register itself with AWS. I wait 2 minutes and run the tunnel command again.

---

### "Port 5433 is already in use"

**Terminal this happens in: Terminal 2 (Tunnel)**

A previous SSM tunnel is still running from an earlier session. I find and close it:

**In Terminal 1 (Terraform), run:**
```bash
lsof -i :5433
```

This lists what is using port 5433. I'll see a row with a number in the `PID` column:
```
COMMAND   PID   USER   ...
ssh      1234   ...
```

I stop it:
```bash
kill 1234
```

I replace `1234` with the actual PID from the output. Then I go back to Terminal 2 and try the tunnel command again.

---

### DMS task shows "failed" status

**Terminal to check: Terminal 1 (Terraform)**

The most common cause is that the RDS reboot in Step 11 wasn't done, or RDS wasn't fully `available` before I started the DMS task.

**In Terminal 1 (Terraform), check the RDS status:**
```bash
aws rds describe-db-instances \
  --db-instance-identifier edp-dev-source-db \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text \
  --profile dev-admin
```

If it says anything other than `available`, I wait. Once it's `available`, I restart the DMS task by running Step 21 again with `--start-replication-task-type reload-target` instead of `start-replication`.

---

### "make destroy" fails partway through

**Terminal this happens in: Terminal 1 (Terraform)**

Sometimes a resource can't be deleted because another one still depends on it. I run `make destroy dev` again — Terraform picks up exactly where it left off and retries. If the same resource keeps failing, I delete it manually in the AWS console at `console.aws.amazon.com`, then run `make destroy dev` one more time.

---

## Quick reference card

```
TERMINAL 1 (Terraform)        TERMINAL 2 (Tunnel)           TERMINAL 3 (Simulator)
──────────────────────────    ─────────────────────────     ──────────────────────────

# Name this terminal:         # Name this terminal:         # Name this terminal:
printf '\033]0;Terraform\007' printf '\033]0;Tunnel\007'    printf '\033]0;Simulator\007'

# One-time tool checks:       # Open AFTER apply            # Open AFTER tunnel is ready
aws --version                 # and RDS is available:
aws sts get-caller-identity \
  --profile dev-admin         aws ssm start-session \       cd .../platform-cdc-simulator
terraform --version             --target <bastion_id> \
session-manager-plugin \        --document-name \
  --version                     AWS-StartPortForwarding... \
                                --parameters \               # No .env editing needed.
# Set passwords:                'host=<rds_endpoint>,...'   # Makefile fetches password
export TF_VAR_db_password=...   --profile dev-admin         # from SSM automatically.
export TF_VAR_redshift_...=...
                              # LEAVE THIS RUNNING           make schema ENV=dev
# Apply infra:                # DO NOT CLOSE                 make seed ENV=dev
cd .../terraform-platform-..                                 make simulate ENV=dev
make init dev
make plan dev
make apply dev   # ~15-20min
# COPY ssm_tunnel_command

# Reboot RDS:
aws rds reboot-db-instance \
  --db-instance-identifier \
  edp-dev-source-db \
  --profile dev-admin

# Check RDS is available:
aws rds describe-db-instances \
  ...DBInstanceStatus...
# Wait for: available

# Start DMS (after seed done):
aws dms start-replication-task \
  --replication-task-arn <arn> \
  --start-replication-task-type \
  start-replication \
  --profile dev-admin

# Verify S3:
aws s3 ls \
  s3://edp-dev-<acct>-bronze/raw/ \
  --recursive --profile dev-admin

──────────── TEARDOWN ─────────────────────────────────────────────────────────────────

# Step 25: (Terminal 3)       Ctrl+C                          stop simulator
# Step 26: (Terminal 2)       Ctrl+C                          close the tunnel
# Step 27: (Terminal 1)   make destroy dev                    destroy everything
# Step 28: (Terminal 1)   aws rds describe-db-instances ...   confirm RDS is gone
```
