# Local Setup Guide

This guide walks through everything I need to do to run the CDC (Change Data Capture) simulator on my Mac from scratch. I wrote this assuming I know very little about the tools involved, so I explain what each tool is and why I need it before asking myself to install it.

By the end of this guide, I'll have a PostgreSQL (a database software) running on my Mac inside Docker (a tool that runs software in isolated boxes), the simulator seeding and generating data into it, and all the tests passing.

If I want to understand how all the Python files relate to and depend on each other, I can check the dependency map in `README.md` under the "How the files depend on each other" section. That section has a visual diagram showing which files import which.

---

## What I need before I start

- A Mac (this guide is written for macOS)
- Docker Desktop installed (I already have this)
- An internet connection
- Terminal open (press `Cmd + Space`, type `Terminal`, press Enter)

---

## Part 1: One-time setup

I only do this part once. After this, I skip straight to Part 2 every time I want to run the simulator.

---

### Step 1: Open Docker Desktop

Docker Desktop is the application that lets me run software in containers. A container is like a sealed box that has everything a piece of software needs to run, completely separate from the rest of my Mac. I use it here to run a PostgreSQL database without installing PostgreSQL directly on my Mac.

1. Press `Cmd + Space` to open Spotlight
2. Type `Docker` and press Enter
3. A small whale icon appears in my Mac menu bar at the top right of my screen
4. The whale icon animates (moves) while Docker is starting up
5. I wait until the whale stops animating. That means Docker is ready
6. If a Terms of Service window appears, I accept it

I need to open Docker Desktop every time I restart my Mac before I can use any Docker commands. It runs quietly in the background like Spotify or Slack.

---

### Step 2: Check if Homebrew is installed

Homebrew is a package manager for Mac. A package manager is a tool that installs other tools for me, so I don't have to download and configure them manually.

In Terminal, I run this from anywhere:

```bash
brew --version
```

If I see something like `Homebrew 4.x.x`, Homebrew is already installed and I move to Step 3.

If I see `command not found: brew`, I install Homebrew first:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

The installer will ask for my Mac password. I type it (nothing shows on screen while I type, that's normal) and press Enter. I follow any prompts it shows me.

---

### Step 3: Install pyenv

pyenv is a tool that manages Python versions. Python is the programming language the simulator is written in. The problem pyenv solves is this: different projects need different versions of Python, and without pyenv, switching between them is messy. With pyenv, I install as many Python versions as I want and each project automatically uses the right one.

I run this from anywhere in Terminal:

```bash
brew install pyenv
```

This takes a minute or two. When it's done I add pyenv to my shell configuration. The shell is the program that reads my commands in Terminal. I need to tell it where pyenv lives so it can find it every time I open a new Terminal window:

```bash
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init -)"' >> ~/.zshrc
```

These three commands add configuration lines to a file called `.zshrc`. This file runs automatically every time I open a new Terminal window. Now I reload it so the changes take effect in the current Terminal window without me having to close and reopen it:

```bash
source ~/.zshrc
```

I verify pyenv is working:

```bash
pyenv --version
```

Expected output: `pyenv 2.x.x`

---

### Step 4: Install Python 3.11.8

Now I install the specific Python version this project uses:

```bash
pyenv install 3.11.8
```

This downloads and compiles Python 3.11.8. Compiling means converting the Python source code into something my Mac can run. It takes 2 to 5 minutes and prints a lot of output. I wait until the command finishes and I get my prompt back (the `$` symbol at the start of a new line).

---

### Step 5: Navigate to the simulator project folder

From this point forward, every command I run must be inside the simulator folder. This is important. If I run commands from the wrong folder, they will not work.

```bash
cd /Users/chuquemeka/enterprise-data-platform/platform-cdc-simulator
```

`cd` stands for "change directory". It moves me into the specified folder.

I verify pyenv automatically picked up the correct Python version by checking the `.python-version` file in this folder:

```bash
python --version
```

Expected output: `Python 3.11.8`

If I see a different version, I run this to set it manually:

```bash
pyenv local 3.11.8
```

Then I check the version again.

---

### Step 6: Check that make is available

`make` is a tool that runs predefined commands for me. Instead of typing long commands, I type `make test` or `make simulate`. It's already installed on most Macs, but I verify:

```bash
make --version
```

If I see `GNU Make 3.x.x` or similar, I'm good.

If I see `command not found: make`, I install Apple's Xcode (a set of developer tools) command line tools, which includes make:

```bash
xcode-select --install
```

A window will pop up asking me to install the tools. I click Install and wait for it to finish.

---

### Step 7: Create the virtual environment

A virtual environment (venv) is an isolated folder that contains only the Python packages this project needs. Without it, installing packages for this project would mix with packages for other projects on my Mac, causing conflicts. With it, everything this project needs lives in a `.venv` folder inside the project, completely separate.

I make sure I'm inside the simulator folder (from Step 5), then run:

```bash
make setup
```

This creates `.venv/`, installs all the packages listed in `requirements.txt` and `requirements-dev.txt`, and tells me when it's done.

Expected final output:
```
Setup complete.
Next: copy .env.example to .env and fill in your database credentials.
```

**Why I don't need to activate the venv manually:**

The `Makefile` always calls the Python binary using its full path inside `.venv/`, so it never depends on the venv being activated:

```
make simulate  →  runs .venv/bin/python main.py simulate  (correct)
make test      →  runs .venv/bin/pytest tests/            (correct)
```

This means I can run `make simulate` or `make test` from a fresh Terminal window without doing anything extra. The Makefile handles it.

**When I do need to activate the venv:**

If I ever want to run `python` or `pytest` directly without `make`, I need to activate the venv first so Terminal uses the right Python:

```bash
source .venv/bin/activate
```

After activating, my Terminal prompt changes to show `(.venv)` at the start, confirming it's active:

```
(.venv) chuquemeka@mac platform-cdc-simulator %
```

Now plain `python` and `pytest` commands use the venv:

```bash
python main.py simulate     # works because venv is active
pytest tests/               # works because venv is active
```

To deactivate the venv when I'm done:

```bash
deactivate
```

The prompt goes back to normal. For day-to-day use I stick with `make` commands and skip all of this.

---

### Step 8: Create the .env file

The `.env` file holds configuration values the simulator needs to run, like the database password and which environment I'm in (dev, staging, or prod). I never commit this file to git because it can contain passwords.

I create it by copying the example file:

```bash
cp .env.example .env
```

`cp` means "copy". This copies `.env.example` to a new file called `.env`.

For local development, the `.env.example` file already has the right values pointing at the Docker database I'm about to start. I don't need to change anything right now.

I can open the file to see what's inside:

```bash
cat .env
```

The most important lines are:

```
ENVIRONMENT=dev
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ecommerce
DB_USER=postgres
DB_PASSWORD=localpass
TEST_DB_NAME=ecommerce_test
```

`ENVIRONMENT=dev` tells the simulator it's in the development environment, which means it will create a maximum of 5,000 orders. `DB_HOST=localhost` means the database is on my own Mac (not in the cloud).

`TEST_DB_NAME=ecommerce_test` is worth understanding. When I run the integration tests (Part 3), they need to create and then delete database tables as part of their setup and teardown. Without this separation, the tests would destroy the data I just seeded into `ecommerce`. By giving the test suite its own database (`ecommerce_test`), I can run tests as many times as I want without ever losing my simulator data. Both databases are created automatically when I first start Docker in Step 9.

---

## Part 2: Running the simulator

I do these steps every time I want to run the simulator. I start at Step 9 because Steps 1 to 8 are already done.

The only thing from Part 1 I repeat each session is Step 1: opening Docker Desktop if it's not already running.

---

### Step 9: Start the local PostgreSQL database

PostgreSQL is the database software the simulator writes data into. I run it inside Docker so I don't have to install PostgreSQL directly on my Mac.

First I make sure Docker Desktop is running (whale icon in menu bar, not animating).

Then, from inside the simulator folder:

```bash
make docker-up
```

Expected output:
```
Starting local PostgreSQL...
[+] Running 1/1
 ✔ Container platform-cdc-simulator-postgres-1  Started
Waiting for PostgreSQL to be ready...
PostgreSQL is ready.
```

I can verify the database container is running:

```bash
docker ps
```

This lists all running containers. I should see one with a name containing `postgres` and status `Up`.

---

### Step 10: Create the database schema

The schema is the structure of the database: the six tables, their columns, the indexes, and the triggers. I only need to run this once per fresh database. If I run `make reset` later, I'll need to run it again.

```bash
make schema
```

Expected output:
```
2024-01-15 10:23:01  INFO  main  Environment: dev | Max orders: 5000 | ...
2024-01-15 10:23:01  INFO  main  Applying schema
2024-01-15 10:23:01  INFO  main  Schema applied successfully
```

---

### Step 11: Seed historical data

Seeding means filling the database with realistic historical data before the live simulation starts. This gives the database context: customers who signed up in the past, orders that are already delivered, payments already processed. Without this, the database starts completely empty, which doesn't represent a real business.

```bash
make seed
```

This takes about 10 to 30 seconds. Expected output:

```
2024-01-15 10:23:05  INFO  simulator.seed  Seeding: 500 customers, 200 products, 2000 historical orders
2024-01-15 10:23:05  INFO  simulator.seed  Seeding 500 customers
2024-01-15 10:23:06  INFO  simulator.seed  Inserted 500 customers
2024-01-15 10:23:06  INFO  simulator.seed  Seeding 200 products
2024-01-15 10:23:06  INFO  simulator.seed  Inserted 200 products
2024-01-15 10:23:06  INFO  simulator.seed  Seeding 2000 historical orders
2024-01-15 10:23:18  INFO  simulator.seed  Inserted 2000 historical orders (with items, payments, shipments)
2024-01-15 10:23:18  INFO  simulator.seed  Seeding complete
```

---

### Step 12: Run the live simulation

This starts the continuous loop that keeps writing new orders, updating statuses, and generating the kind of activity a real e-commerce database sees every day.

```bash
make simulate
```

I'll see logs appearing every few seconds. Every 10 ticks (every 20 seconds by default), a stats line shows how many records are in each table:

```
2024-01-15 10:23:20  INFO  simulator.simulate  Simulator starting — environment limit: 5000 orders...
2024-01-15 10:23:22  INFO  simulator.simulate  New order 2001 placed for customer 147 (total: 89.97)
2024-01-15 10:23:24  INFO  simulator.simulate  Order 1843: processing → shipped
2024-01-15 10:24:00  INFO  simulator.simulate  [tick 10] customers=501  orders=2029  items=6234  payments=2021  shipments=1103
```

I press `Ctrl+C` to stop the simulation. I'll see:

```
2024-01-15 10:24:05  INFO  simulator.simulate  Simulator stopped after 12 ticks
```

The database keeps all the data even after the simulator stops. I can restart the simulation any time and it continues from where it left off.

---

## Part 3: Running the tests

Tests check that the code works correctly. I have two types:

- **Unit tests**: test individual pieces of code in isolation. No database needed. Fast.
- **Integration tests**: test the code working with a real database. Needs Docker Postgres running.

### Unit tests only (no database needed)

```bash
make test-unit
```

Expected output:
```
Running unit tests (no database required)...
collected 45 items

tests/test_config.py::TestOrderStatus::test_lifecycle_starts_with_pending PASSED
tests/test_config.py::TestOrderStatus::test_lifecycle_ends_with_delivered PASSED
tests/test_models.py::TestOrderItem::test_line_total_equals_quantity_times_unit_price PASSED
...
============================== 45 passed in 2.31s ==============================
```

### Integration tests (needs PostgreSQL running)

I make sure `make docker-up` was run first, then:

```bash
make test-integration
```

**Important:** the integration tests run against `ecommerce_test`, not `ecommerce`. They create all the tables, run the tests, and then drop all the tables when done. This is by design — it means running integration tests can never delete or corrupt the data in my main `ecommerce` database. Both databases exist in the same Docker container, they're just completely isolated from each other.

Expected output:
```
Running integration tests (requires a running PostgreSQL)...
collected 15 items

tests/test_db.py::TestDatabaseManagerConnect::test_connect_succeeds PASSED
tests/test_db.py::TestTransactionRollback::test_exception_in_cursor_rolls_back PASSED
...
============================== 15 passed in 3.44s ==============================
```

### All tests together

```bash
make test
```

This runs unit tests and integration tests together and shows a coverage report showing what percentage of the code is covered by tests.

---

## Part 4: Looking inside the database

This is useful for verifying the data looks right. My PostgreSQL container actually holds two databases: `ecommerce` (my simulator data) and `ecommerce_test` (only used by the test suite). I connect to the simulator's database:

```bash
docker exec -it platform-cdc-simulator-postgres-1 psql -U postgres -d ecommerce
```

Breaking this down:
- `docker exec` runs a command inside a running container
- `-it` means "interactive terminal" (lets me type commands)
- `platform-cdc-simulator-postgres-1` is the container name
- `psql -U postgres -d ecommerce` opens the PostgreSQL prompt for my database

I'll see a prompt like `ecommerce=#`. Now I can run SQL (Structured Query Language) queries:

```sql
-- Count records in each table
SELECT COUNT(*) FROM customers;
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM order_items;
SELECT COUNT(*) FROM payments;
SELECT COUNT(*) FROM shipments;

-- See the most recent 10 orders
SELECT order_id, customer_id, order_status, order_date
FROM orders
ORDER BY order_date DESC
LIMIT 10;

-- See how many orders are in each status
SELECT order_status, COUNT(*)
FROM orders
GROUP BY order_status
ORDER BY COUNT(*) DESC;

-- See a sample of customers with their countries
SELECT first_name, last_name, country, signup_date
FROM customers
LIMIT 5;
```

To exit the database prompt:

```sql
\q
```

---

## Part 5: Cleaning up

### Stop the database (data is kept)

```bash
make docker-down
```

The database container stops. All my data is preserved in a Docker volume (a named storage area). Next time I run `make docker-up`, the data is still there.

### Stop the database and delete all data

```bash
docker compose down -v
```

The `-v` flag removes the Docker volume, deleting all data permanently. This removes both the `ecommerce` and `ecommerce_test` databases. Next time I run `make docker-up`, the init script runs again and recreates both databases from scratch. I'll then need to run `make schema` and `make seed` again to restore my simulator data.

### Start fresh without recreating the Docker container

If the data is in a weird state and I want to wipe and reseed without touching Docker, I use:

```bash
make reset
```

This drops all tables, recreates the schema, and re-seeds with fresh data. The Docker container keeps running.

### Remove the virtual environment and start completely fresh

```bash
make clean
```

This removes `.venv/` and all Python cache files. I'll need to run `make setup` again before running any simulator commands.

---

## Part 6: Troubleshooting

### "Docker daemon is not running"

Docker Desktop is not open. I go to my Applications folder, open Docker Desktop, and wait for the whale icon to stop animating.

### "command not found: make"

Xcode command line tools are not installed. I run:

```bash
xcode-select --install
```

### "python --version shows the wrong version"

pyenv is not recognising the `.python-version` file. I run:

```bash
pyenv local 3.11.8
python --version
```

### "could not connect to server" when running make schema, seed, or simulate

The PostgreSQL database is not running. I run `make docker-up` first.

### "No such file or directory: .env"

I forgot to create the `.env` file. I run:

```bash
cp .env.example .env
```

### The simulation says "Order limit reached" immediately after starting

The database already has 5,000 or more orders from a previous session. This is not an error. The simulator keeps working, it just doesn't create new orders. If I want to reset the count, I run `make reset`.

---

## Quick reference

```
# Every session — open Docker Desktop first, then:
cd /Users/chuquemeka/enterprise-data-platform/platform-cdc-simulator
make docker-up        start PostgreSQL
make schema           create tables (first time only, or after reset)
make seed             fill with historical data (first time only, or after reset)
make simulate         run the simulator (Ctrl+C to stop)

# Tests
make test-unit        fast tests, no database needed
make test-integration needs make docker-up first
make test             everything together

# Inspect the database
docker exec -it platform-cdc-simulator-postgres-1 psql -U postgres -d ecommerce

# Clean up
make docker-down      stop database, keep data
docker compose down -v stop database, delete all data
make reset            wipe tables and reseed (database must be running)
make clean            remove .venv and cache files
```
