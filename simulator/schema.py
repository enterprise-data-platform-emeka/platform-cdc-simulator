"""
PostgreSQL DDL (Data Definition Language) for the e-commerce OLTP schema.

Design decisions:
- REPLICA IDENTITY FULL is set on every table so that AWS DMS captures the
  complete before-image on every UPDATE and DELETE, not just the primary key.
  Without this, the WAL entry for an UPDATE only contains changed columns plus
  the PK. DMS cannot tell what the old values were for unchanged columns.
- updated_at columns use DEFAULT now() and are updated via trigger so application
  code does not need to set them explicitly.
- All foreign keys have indexes to avoid sequential scans on joins.
- Sequences start at 1 and are owned by their column so DROP TABLE CASCADE
  also drops the sequence.
"""

from __future__ import annotations

# ── Schema creation ───────────────────────────────────────────────────────────

CREATE_UPDATED_AT_FUNCTION_SQL: str = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TABLES_SQL: str = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id  SERIAL       PRIMARY KEY,
    first_name   VARCHAR(100) NOT NULL,
    last_name    VARCHAR(100) NOT NULL,
    email        VARCHAR(255) NOT NULL UNIQUE,
    country      VARCHAR(100) NOT NULL,
    phone        VARCHAR(50),
    signup_date  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    product_id   SERIAL         PRIMARY KEY,
    name         VARCHAR(255)   NOT NULL,
    category     VARCHAR(100)   NOT NULL,
    brand        VARCHAR(100)   NOT NULL,
    unit_price   NUMERIC(10, 2) NOT NULL CHECK (unit_price > 0),
    stock_qty    INTEGER        NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    order_id     SERIAL       PRIMARY KEY,
    customer_id  INTEGER      NOT NULL REFERENCES customers(customer_id),
    order_date   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    order_status VARCHAR(50)  NOT NULL DEFAULT 'pending',
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id  SERIAL         PRIMARY KEY,
    order_id       INTEGER        NOT NULL REFERENCES orders(order_id),
    product_id     INTEGER        NOT NULL REFERENCES products(product_id),
    quantity       INTEGER        NOT NULL CHECK (quantity > 0),
    unit_price     NUMERIC(10, 2) NOT NULL CHECK (unit_price > 0),
    line_total     NUMERIC(10, 2) NOT NULL,
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id    SERIAL         PRIMARY KEY,
    order_id      INTEGER        NOT NULL REFERENCES orders(order_id),
    method        VARCHAR(50)    NOT NULL,
    amount        NUMERIC(10, 2) NOT NULL CHECK (amount > 0),
    status        VARCHAR(50)    NOT NULL DEFAULT 'pending',
    payment_date  TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ    NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id      SERIAL       PRIMARY KEY,
    order_id         INTEGER      NOT NULL REFERENCES orders(order_id),
    carrier          VARCHAR(100) NOT NULL,
    delivery_status  VARCHAR(50)  NOT NULL DEFAULT 'pending',
    shipped_date     TIMESTAMPTZ,
    delivered_date   TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""

CREATE_INDEXES_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_orders_customer_id        ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status             ON orders(order_status);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id      ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product_id    ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_payments_order_id         ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_order_id        ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_delivery_status ON shipments(delivery_status);
"""

# updated_at triggers, one per table
CREATE_TRIGGERS_SQL: str = """
DO $$ DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['customers','products','orders','order_items','payments','shipments']
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_set_updated_at ON %I;
             CREATE TRIGGER trg_set_updated_at
             BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
            t, t
        );
    END LOOP;
END $$;
"""

# REPLICA IDENTITY FULL tells PostgreSQL to write the full old row into the WAL
# on every UPDATE and DELETE. AWS DMS requires this to capture complete
# change events (otherwise updates only contain the changed columns).
SET_REPLICA_IDENTITY_SQL: str = """
ALTER TABLE customers   REPLICA IDENTITY FULL;
ALTER TABLE products    REPLICA IDENTITY FULL;
ALTER TABLE orders      REPLICA IDENTITY FULL;
ALTER TABLE order_items REPLICA IDENTITY FULL;
ALTER TABLE payments    REPLICA IDENTITY FULL;
ALTER TABLE shipments   REPLICA IDENTITY FULL;
"""

# ── Schema teardown ───────────────────────────────────────────────────────────

DROP_TABLES_SQL: str = """
DROP TABLE IF EXISTS shipments   CASCADE;
DROP TABLE IF EXISTS payments    CASCADE;
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders      CASCADE;
DROP TABLE IF EXISTS products    CASCADE;
DROP TABLE IF EXISTS customers   CASCADE;
"""

DROP_FUNCTION_SQL: str = """
DROP FUNCTION IF EXISTS set_updated_at() CASCADE;
"""

# ── Convenience groupings ─────────────────────────────────────────────────────
# Used by the Schemer class to apply or tear down the full schema in one call.

ALL_CREATE_STATEMENTS: tuple[str, ...] = (
    CREATE_UPDATED_AT_FUNCTION_SQL,
    CREATE_TABLES_SQL,
    CREATE_INDEXES_SQL,
    CREATE_TRIGGERS_SQL,
    SET_REPLICA_IDENTITY_SQL,
)

ALL_DROP_STATEMENTS: tuple[str, ...] = (
    DROP_TABLES_SQL,
    DROP_FUNCTION_SQL,
)
