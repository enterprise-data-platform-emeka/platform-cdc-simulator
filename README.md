# Module: platform-cdc-simulator

**Location:** `platform-cdc-simulator/`

**Role in the platform:** Generates a continuous stream of realistic OLTP (Online Transaction Processing) activity against the source PostgreSQL database. AWS DMS (Database Migration Service) watches this database and forwards every INSERT, UPDATE, and DELETE to the Bronze S3 layer in Parquet format.

---

## What this module does

The CDC simulator is a Python programme that does three things:

1. **Creates the database schema** — six normalised tables that represent a European e-commerce operation: `customers`, `products`, `orders`, `order_items`, `payments`, `shipments`.

2. **Seeds historical data** — populates the database with two years of realistic historical orders so that downstream queries are meaningful from day one rather than starting with an empty data set.

3. **Simulates live traffic** — runs a continuous loop that places new orders, advances orders through their lifecycle (pending → confirmed → processing → shipped → delivered), tracks shipments, and generates cancellations and refunds at realistic rates.

Every write goes through psycopg2 directly to PostgreSQL, so it lands in the WAL (Write-Ahead Log). DMS reads the WAL and produces CDC (Change Data Capture) events.

---

## Prerequisites

- Python 3.11 or later
- Access to a PostgreSQL 14+ database with logical replication enabled
- The database user must have `REPLICATION` privilege or be a superuser

---

## Installation

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Copy the example environment file and fill in your credentials
cp .env.example .env
```

Edit `.env` with your database credentials:

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ecommerce
DB_USER=postgres
DB_PASSWORD=yourpassword
```

---

## Commands

### Create the schema

```bash
python main.py schema
```

Creates all six tables, adds indexes, attaches `updated_at` triggers, and sets `REPLICA IDENTITY FULL` on every table.

`REPLICA IDENTITY FULL` is a PostgreSQL setting that tells the WAL to record the complete old row on every UPDATE and DELETE, not just the primary key. AWS DMS requires this to produce complete CDC events.

### Seed historical data

```bash
python main.py seed
```

Inserts customers, products, and two years of historical orders with realistic lifecycle distributions. The seed is deterministic — running it twice with the same `SEED_RANDOM_SEED` produces the same data.

### Run the live simulation

```bash
python main.py simulate
```

Starts the continuous loop. Press Ctrl+C to stop. Progress is logged every 10 ticks:

```
2024-01-15 10:23:01  INFO  simulator.simulate  [tick 10] customers=502  orders=4567  items=13842  payments=4521  shipments=3109
```

Use `--log-level DEBUG` to see every individual INSERT and UPDATE:

```bash
python main.py simulate --log-level DEBUG
```

### Reset (drop and reseed)

```bash
python main.py reset
```

Drops all tables, recreates the schema, and runs the seeder from scratch. **This destroys all existing data.** Use this during development to start with a clean state.

---

## Connecting to RDS inside the private VPC

The production RDS (Relational Database Service) instance runs in a private subnet with no public IP address. You cannot connect to it directly from your laptop.

Use AWS SSM (Systems Manager) Session Manager to open a port-forwarding tunnel through the bastion or directly to the RDS endpoint:

```bash
# Get the RDS endpoint from Terraform output
ENDPOINT=$(cd terraform-platform-infra-live && make output dev 2>/dev/null | grep rds_endpoint | awk '{print $3}')

# Or find it in the AWS console: RDS → Databases → edp-dev-postgres → Endpoint

# Start an SSM port-forwarding session
# Replace i-1234567890abcdef0 with your EC2 instance ID (the bastion or any EC2 in the VPC)
aws ssm start-session \
    --target i-1234567890abcdef0 \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "host=$ENDPOINT,portNumber=5432,localPortNumber=5433" \
    --profile dev-admin
```

Then point the simulator at localhost:5433:

```bash
# In .env
DB_HOST=localhost
DB_PORT=5433
```

---

## Configuration reference

All configuration is read from environment variables (or `.env`):

| Variable | Default | Description |
|---|---|---|
| `DB_HOST` | (required) | PostgreSQL hostname or IP |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | (required) | Database name |
| `DB_USER` | (required) | Database user |
| `DB_PASSWORD` | (required) | Database password |
| `SEED_CUSTOMERS` | `500` | Number of customers to seed |
| `SEED_PRODUCTS` | `200` | Number of products to seed |
| `SEED_HISTORICAL_ORDERS` | `2000` | Number of historical orders to seed |
| `SEED_RANDOM_SEED` | `42` | Random seed for reproducible data |
| `SIM_TICK_INTERVAL_SECONDS` | `2` | Pause between simulation ticks |
| `SIM_NEW_ORDERS_PER_TICK` | `3` | New orders placed each tick |
| `RETRY_MAX_ATTEMPTS` | `5` | Maximum reconnection attempts |
| `RETRY_WAIT_MIN_SECONDS` | `1` | Minimum wait before retry |
| `RETRY_WAIT_MAX_SECONDS` | `30` | Maximum wait before retry (exponential backoff) |

---

## The source data model

Six normalised tables representing an e-commerce operation:

```
customers ──< orders ──< order_items >── products
                  │
                  ├──< payments
                  │
                  └──< shipments
```

```sql
customers   (customer_id, first_name, last_name, email, country, phone, signup_date, updated_at)
products    (product_id,  name, category, brand, unit_price, stock_qty, updated_at)
orders      (order_id,    customer_id, order_date, order_status, updated_at)
order_items (order_item_id, order_id, product_id, quantity, unit_price, line_total, updated_at)
payments    (payment_id,  order_id, method, amount, status, payment_date, updated_at)
shipments   (shipment_id, order_id, carrier, delivery_status, shipped_date, delivered_date, updated_at)
```

**Order lifecycle:** `pending → confirmed → processing → shipped → delivered`

Terminal states: `delivered`, `cancelled`, `refunded`

**Shipment lifecycle:** `pending → in_transit → out_for_delivery → delivered`

Terminal states: `delivered`, `failed_delivery`, `returned`

---

## How DMS reads this data

AWS DMS connects to PostgreSQL as a replication client and subscribes to the WAL stream using logical replication. For each change event, DMS writes a Parquet file to the Bronze S3 bucket:

```
s3://edp-dev-{account-id}-bronze/
└── customers/
    └── year=2024/month=01/
        └── LOAD00000001.parquet    ← full-load snapshot
        └── 20240115-102301-0001.parquet  ← CDC events
```

The full-load run copies the entire table first. After that, DMS continuously writes incremental CDC files as changes arrive from the WAL.

The Bronze layer is append-only. AWS Glue (in the `platform-glue-jobs` repository) reads these files and resolves the CDC operations into a clean Silver layer.

---

## Project structure

```
platform-cdc-simulator/
├── requirements.txt        Dependencies
├── .env.example            Template for environment variables
├── .gitignore
├── main.py                 CLI entry point (schema | seed | simulate | reset)
└── simulator/
    ├── __init__.py
    ├── config.py           Frozen dataclasses for config + domain constants
    ├── db.py               DatabaseManager: connection, retry, cursor context manager
    ├── schema.py           SQL DDL: CREATE TABLE, indexes, triggers, REPLICA IDENTITY
    ├── models.py           Dataclasses: Customer, Product, Order, OrderItem, Payment, Shipment
    ├── seed.py             Seeder: two years of historical data
    └── simulate.py         Simulator: continuous CRUD loop
```
