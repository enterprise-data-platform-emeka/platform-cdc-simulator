# platform-cdc-simulator

This is the test data generator for the Enterprise Data Platform. Its job is to pretend to be a real e-commerce business by writing customer orders, payments, and shipments into a PostgreSQL database. Once the data is in the database, AWS DMS (Database Migration Service) picks it up and forwards it to the Bronze S3 (Simple Storage Service) layer, which is where the data pipeline begins.

Without this simulator, I'd have no source data to flow through the pipeline. It's the starting gun.

---

## What problem this solves

The data pipeline starts at a PostgreSQL database that belongs to an e-commerce business. In a real company, that database has thousands of records being written every hour by the website. In my project, I control everything, so I need to generate that activity myself.

The simulator does three things:

1. **Creates the database schema** — sets up six tables that look like a real e-commerce system.
2. **Seeds historical data** — fills the database with two years of past orders so the pipeline has something realistic to process from day one.
3. **Simulates live traffic** — continuously places new orders, moves orders through statuses (pending → confirmed → shipped → delivered), processes payments, and occasionally cancels or refunds orders.

Every write the simulator makes lands in PostgreSQL's WAL (Write-Ahead Log), which is PostgreSQL's internal diary of every change. AWS DMS reads that diary and sends each change to S3 as a Parquet file. That's CDC (Change Data Capture) in action.

---

## How much data it creates

The simulator respects environment-specific limits so it never creates more data than needed for that environment:

| Environment | Max orders | Seed customers | Seed products | Seed historical orders |
|---|---|---|---|---|
| `dev` | 5,000 | 500 | 200 | 2,000 |
| `staging` | 10,000 | 1,000 | 400 | 5,000 |
| `prod` | 15,000 | 2,000 | 800 | 10,000 |

Once the order limit is reached, the simulator stops creating new orders but keeps updating existing ones (status transitions, shipment tracking, refunds). This keeps the CDC stream active without growing the database indefinitely.

---

## Prerequisites

- Python 3.11.8 (managed by pyenv — see setup below)
- Docker Desktop (for the local PostgreSQL database)
- An internet connection to install Python packages

---

## First-time setup

**Step 1: Install pyenv** (if not already installed)

pyenv lets me install and switch between Python versions. The `.python-version` file in this project tells pyenv which version to use automatically.

```bash
brew install pyenv
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init -)"' >> ~/.zshrc
source ~/.zshrc
pyenv install 3.11.8
```

**Step 2: Create the virtual environment**

```bash
cd platform-cdc-simulator
make setup
```

This creates a `.venv` folder inside the project with all the Python packages installed. The packages are isolated here — they don't affect anything else on my Mac.

**Step 3: Configure the environment**

```bash
cp .env.example .env
```

The `.env` file already has the right values for local development. I don't need to change anything to get started.

**Step 4: Start the local PostgreSQL database**

```bash
make docker-up
```

This uses Docker to run a PostgreSQL database on my Mac. It's completely separate from any AWS resources — it costs nothing and I can delete it any time with `make docker-down`.

**Step 5: Create the schema, seed data, and simulate**

```bash
make schema     # create the six tables
make seed       # fill with 2 years of historical data
make simulate   # start the live traffic loop (Ctrl+C to stop)
```

---

## Commands

```bash
make help             # show all available commands
make setup            # create .venv and install dependencies
make lint             # check code style with ruff
make typecheck        # check types with mypy
make test             # run all tests
make test-unit        # run unit tests only (no database needed)
make test-integration # run integration tests (needs PostgreSQL)
make schema           # create database tables
make seed             # seed historical data
make simulate         # run the live simulation loop
make reset            # drop all tables, recreate, reseed (destroys data)
make docker-up        # start local PostgreSQL in Docker
make docker-down      # stop Docker containers
make docker-build     # build the simulator as a Docker image
make clean            # remove .venv and cache files
```

---

## Switching from local to AWS

To point the simulator at the AWS RDS (Relational Database Service) instance instead of Docker, I update three lines in `.env`:

```
DB_HOST=edp-dev-postgres.xxxxxx.eu-central-1.rds.amazonaws.com
DB_PORT=5432
DB_PASSWORD=<my RDS password>
```

No code changes. Everything else stays the same.

If the RDS instance is in a private VPC (Virtual Private Cloud) with no public access (which it is, by design), I connect via an SSM (Systems Manager) port-forwarding tunnel:

```bash
aws ssm start-session \
    --target i-<your-ec2-instance-id> \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "host=<rds-endpoint>,portNumber=5432,localPortNumber=5433" \
    --profile dev-admin
```

Then set `DB_HOST=localhost` and `DB_PORT=5433` in `.env`. The tunnel makes the remote RDS look like a local database.

---

## The data model

Six tables representing a European e-commerce business:

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
Terminal states (no further changes): `delivered`, `cancelled`, `refunded`

**Why `REPLICA IDENTITY FULL`?** PostgreSQL normally only writes changed columns to the WAL on an UPDATE. AWS DMS needs the complete old row to produce a full CDC event. Setting `REPLICA IDENTITY FULL` on every table tells PostgreSQL to write the entire old row, which is what DMS requires.

---

## CI/CD

Every push and pull request triggers the GitHub Actions workflow at `.github/workflows/ci.yml`:

```
Pull request
    → lint (ruff)
    → type check (mypy)
    → unit tests (pytest, no DB)
    → integration tests (pytest, with PostgreSQL service container)
    → build Docker image

Merge to main
    → all of the above
    → push Docker image to GitHub Container Registry
```

The integration tests run against a real PostgreSQL database that GitHub spins up automatically inside the CI pipeline. I don't need to provision anything.

---

## What each file does

### Configuration and entry point

**`.python-version`**
Contains just `3.11.8`. pyenv reads this file when I `cd` into the project and automatically uses Python 3.11.8. Anyone who clones this repo gets the same Python version without any manual steps.

**`pyproject.toml`**
Configuration for three tools in one file: ruff (linting), mypy (type checking), and pytest (testing). Centralising tool config here means I don't have scattered config files like `.flake8`, `mypy.ini`, and `pytest.ini` all over the place.

**`requirements.txt`**
The Python packages the simulator needs to run: psycopg2 (connects to PostgreSQL), faker (generates realistic fake data), python-dotenv (reads `.env` files), and tenacity (handles retries).

**`requirements-dev.txt`**
Extra packages only needed during development: ruff (linting), mypy (type checking), pytest (testing), and type stubs. These are not installed in the Docker image — they're only for local development and CI.

**`.env.example`**
A template showing every environment variable the simulator reads. I copy this to `.env` and fill in my values. The real `.env` is in `.gitignore` so passwords never end up in git.

**`Makefile`**
A shortcut file. Instead of remembering long commands like `python main.py simulate` or `.venv/bin/pytest tests/ -m "not integration"`, I run `make simulate` or `make test-unit`. The Makefile also handles virtual environment paths so commands work correctly regardless of whether the venv is activated.

**`main.py`**
The CLI (Command Line Interface) entry point. It parses the command (`schema`, `seed`, `simulate`, `reset`), loads all configuration from environment variables, raises a clear error if anything is missing, then calls the right function. It also sets up logging so every log line shows the time, level, and which module it came from.

**`Dockerfile`**
Packages the simulator as a Docker container image. Uses a two-stage build: stage one installs packages, stage two copies only the installed packages and source code (not the build tools). The final image runs as a non-root user for security. This image can be deployed to AWS ECS (Elastic Container Service) if I ever want to run the simulator in the cloud instead of on my laptop.

**`docker-compose.yml`**
Defines the local development stack: a PostgreSQL container and optionally the simulator container. Running `make docker-up` starts just the PostgreSQL container. Running `make docker-simulate` starts both together.

### The `simulator/` package

**`simulator/__init__.py`**
An empty file that tells Python "this folder is a package". Without it, Python wouldn't know to treat `simulator/` as importable code.

**`simulator/exceptions.py`**
Defines the custom exception types used throughout the project. Having named exceptions instead of generic `Exception` means:
- I can catch exactly the errors I expect and let everything else crash loudly.
- Error messages always say what kind of failure happened.
- Tests can verify that specific failure modes raise specific exceptions.

The hierarchy is: `SimulatorError` (base) → `ConfigurationError`, `DatabaseConnectionError`, `SchemaError`, `SeedError`, `SimulationError`.

**`simulator/config.py`**
Two things in one file: constants and configuration.

Constants are things that never change, like `OrderStatus.PENDING = "pending"` or the list of product categories with their price ranges. I define these as class attributes with `Final` type annotations so the IDE catches any accidental reassignment.

Configuration is loaded from environment variables at startup. I use frozen dataclasses (`DatabaseConfig`, `SeedConfig`, `SimulationConfig`, `RetryConfig`) so the config object is immutable once created. The `ENVIRONMENT` variable (`dev`, `staging`, or `prod`) automatically sets the record limits — I don't need to remember to change numbers when switching environments.

**`simulator/db.py`**
The `DatabaseManager` class. It handles everything related to talking to the database:
- Connecting with exponential backoff retry (if the database is briefly unavailable, it waits and tries again rather than crashing immediately)
- A cursor context manager that automatically commits on success and rolls back on failure — so a failed insert never leaves the database in a half-written state
- Helper methods for inserting multiple rows at once (`execute_many`), running single statements (`execute`), and reading data (`fetch_all`, `fetch_column`, `fetch_one`)

**`simulator/schema.py`**
All the SQL DDL (Data Definition Language) statements that create and drop the database schema. I keep the SQL here as string constants rather than scattering it through other files, so there's one place to look when I need to change a table definition. It also sets `REPLICA IDENTITY FULL` on every table and creates `updated_at` triggers so the column updates automatically on every row change.

**`simulator/models.py`**
Python dataclasses for each database table: `Customer`, `Product`, `Order`, `OrderItem`, `Payment`, `Shipment`. Each model has:
- A `generate(fake, ...)` classmethod that creates a realistic fake instance using the Faker library
- An `as_insert_tuple()` method that returns the values in the exact column order the INSERT statement expects

This keeps the mapping between Python objects and SQL in one place instead of splitting it between the model and the code that inserts it.

**`simulator/seed.py`**
The `Seeder` class fills the database with two years of historical data before the live simulation starts. It creates customers and products first, then creates historical orders with realistic lifecycle distributions — most old orders are delivered, a realistic fraction are cancelled or refunded, and a few recent ones are still in transit.

Every failure raises `SeedError` with a descriptive message. Nothing is skipped silently — a partial seed is worse than no seed because it produces misleading data downstream.

**`simulator/simulate.py`**
The `Simulator` class runs the continuous loop. Each tick:
1. Checks if the order count is below the environment limit, then places new orders.
2. Advances ~20% of in-progress orders by one lifecycle step (pending → confirmed, etc.).
3. Advances shipment delivery statuses.
4. Occasionally cancels a pending order or processes a refund.
5. Occasionally adds a new customer.
6. Sleeps until the next tick.

Database connection errors are caught and trigger a reconnect. All other errors crash the process — a loud crash is easier to diagnose than a silent failure producing wrong data.

### Tests

**`tests/conftest.py`**
Shared test fixtures. The `db` fixture creates a fresh schema before each integration test and drops it after, so every test starts with a clean database.

**`tests/test_exceptions.py`**
Verifies the exception hierarchy. Every custom exception must inherit from `SimulatorError`, messages must be preserved, and exceptions must be raiseable and catchable.

**`tests/test_config.py`**
Verifies the domain constants and config classes. Tests that lifecycle lists are in the right order, that per-environment limits increase from dev to prod, that passwords never appear in `repr()` output, and that missing environment variables raise `ConfigurationError` with a clear message.

**`tests/test_models.py`**
Verifies every model's `generate()` factory and `as_insert_tuple()` method. The most important test: `line_total` must exactly equal `quantity * unit_price` for every `OrderItem`, no rounding surprises.

**`tests/test_schema.py`**
Verifies the SQL strings without executing them. Checks that all six table names appear in the CREATE and DROP statements, that `REPLICA IDENTITY FULL` is set on every table, and that `CASCADE` is present in the DROP statement.

**`tests/test_db.py`**
Integration tests for `DatabaseManager`. These run against a real PostgreSQL database (the CI workflow provides one automatically). They verify connection, execute, fetch, bulk insert, and — most importantly — that a failed transaction actually rolls back and leaves the database unchanged.

### CI/CD

**`.github/workflows/ci.yml`**
The GitHub Actions pipeline that runs on every push and pull request. It runs four jobs in sequence: lint and type check (no DB needed), unit tests (no DB needed), integration tests (with a real PostgreSQL), and Docker image build. On merge to `main`, the Docker image is also pushed to GitHub Container Registry. This means I can always pull the latest tested image with one command.

---

## How DMS reads this data

AWS DMS connects to PostgreSQL as a replication client and subscribes to the WAL stream. For each change the simulator makes, DMS writes a Parquet file to the Bronze S3 bucket:

```
s3://edp-dev-{account-id}-bronze/
└── customers/
    └── year=2024/month=01/
        └── LOAD00000001.parquet       ← full table snapshot on first run
        └── 20240115-102301-0001.parquet  ← CDC events after that
```

The Bronze layer is append-only. Glue (in `platform-glue-jobs`) reads these files and resolves the CDC operations (INSERT/UPDATE/DELETE) into a clean Silver layer.
