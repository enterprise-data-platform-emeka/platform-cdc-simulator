"""
Tests for the custom exception hierarchy.

These verify that:
- Every custom exception is a subclass of SimulatorError, so callers can
  catch SimulatorError to catch anything from this project.
- Each exception can be raised and caught correctly.
- Exception messages are preserved.
"""

import pytest

from simulator.exceptions import (
    ConfigurationError,
    DatabaseConnectionError,
    SchemaError,
    SeedError,
    SimulationError,
    SimulatorError,
)


class TestExceptionHierarchy:
    def test_configuration_error_is_simulator_error(self):
        assert issubclass(ConfigurationError, SimulatorError)

    def test_database_connection_error_is_simulator_error(self):
        assert issubclass(DatabaseConnectionError, SimulatorError)

    def test_schema_error_is_simulator_error(self):
        assert issubclass(SchemaError, SimulatorError)

    def test_seed_error_is_simulator_error(self):
        assert issubclass(SeedError, SimulatorError)

    def test_simulation_error_is_simulator_error(self):
        assert issubclass(SimulationError, SimulatorError)

    def test_all_exceptions_are_exceptions(self):
        """Verify the whole hierarchy roots in the built-in Exception."""
        for exc_class in (
            SimulatorError,
            ConfigurationError,
            DatabaseConnectionError,
            SchemaError,
            SeedError,
            SimulationError,
        ):
            assert issubclass(exc_class, Exception)


class TestExceptionMessages:
    def test_configuration_error_preserves_message(self):
        with pytest.raises(ConfigurationError, match="ENVIRONMENT variable is missing"):
            raise ConfigurationError("ENVIRONMENT variable is missing")

    def test_database_connection_error_preserves_message(self):
        with pytest.raises(DatabaseConnectionError, match="could not connect"):
            raise DatabaseConnectionError("could not connect to localhost:5432")

    def test_seed_error_preserves_message(self):
        with pytest.raises(SeedError, match="customers table is empty"):
            raise SeedError("customers table is empty")

    def test_simulator_error_can_wrap_cause(self):
        original = ValueError("bad value")
        with pytest.raises(DatabaseConnectionError) as exc_info:
            raise DatabaseConnectionError("wrapped") from original
        assert exc_info.value.__cause__ is original
