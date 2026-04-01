"""
Custom exception hierarchy for the CDC simulator.

Having named exception types instead of generic Exception means:
- Callers can catch exactly what they expect and let everything else bubble up.
- Log messages always say what kind of failure occurred, not just "something went wrong".
- The CI pipeline can verify that specific failure modes raise specific exceptions.

Hierarchy:
    SimulatorError                  ← base: catch this to catch anything from this project
        ConfigurationError          ← bad or missing environment variable
        DatabaseConnectionError     ← cannot reach PostgreSQL (retry makes sense)
        SchemaError                 ← CREATE TABLE / DROP TABLE failed
        SeedError                   ← historical data seeding failed
        SimulationError             ← a simulation tick failed in an unrecoverable way
"""


class SimulatorError(Exception):
    """
    Base class for all exceptions raised by this project.

    Never raise this directly. Always raise one of the subclasses so the
    caller knows what category of failure they are dealing with.
    """


class ConfigurationError(SimulatorError):
    """
    Raised when a required environment variable is missing or has an invalid value.

    Example: ENVIRONMENT=production instead of dev/staging/prod.
    """


class DatabaseConnectionError(SimulatorError):
    """
    Raised when the database is unreachable or the connection is dropped.

    This is the one error the simulator's main loop catches and retries.
    everything else is treated as a programming bug and crashes the process.
    """


class SchemaError(SimulatorError):
    """
    Raised when creating or dropping the database schema fails.

    This usually means the SQL is wrong or the database user lacks privileges.
    """


class SeedError(SimulatorError):
    """
    Raised when seeding historical data fails.

    Common causes: tables do not exist yet (run schema first), or the
    database already has data that conflicts with the seed (run reset instead).
    """


class SimulationError(SimulatorError):
    """
    Raised when a simulation tick fails in a way that cannot be recovered.

    Example: the orders table is missing mid-simulation, or a required
    lookup returns no rows when it always should.
    """
