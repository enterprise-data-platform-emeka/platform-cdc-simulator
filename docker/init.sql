-- This script runs automatically when the PostgreSQL container starts for the
-- first time. It creates both databases so the simulator and the test suite
-- each have their own isolated space and can never interfere with each other.
--
-- ecommerce      → used by the simulator (make schema / make seed / make simulate)
-- ecommerce_test → used by the integration test suite (make test-integration)
--
-- The main postgres user owns both databases.

CREATE DATABASE ecommerce;
CREATE DATABASE ecommerce_test;
