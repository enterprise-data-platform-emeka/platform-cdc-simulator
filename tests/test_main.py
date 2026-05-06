from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from main import build_parser, cmd_bootstrap, cmd_simulate


def test_parser_accepts_simulate_duration_seconds() -> None:
    args = build_parser().parse_args(["simulate", "--duration-seconds", "600"])

    assert args.command == "simulate"
    assert args.duration_seconds == 600


def test_parser_accepts_bootstrap_command() -> None:
    args = build_parser().parse_args(["bootstrap"])

    assert args.command == "bootstrap"


def test_cmd_bootstrap_creates_and_seeds_empty_database(dev_limits) -> None:
    from simulator.config import SeedConfig

    db = MagicMock()
    db.fetch_one.side_effect = [(False,), (0,)]
    seed_config = SeedConfig.from_env(dev_limits)

    with patch("main.cmd_schema") as cmd_schema, patch("main.cmd_seed") as cmd_seed:
        cmd_bootstrap(db, seed_config)

    cmd_schema.assert_called_once_with(db)
    cmd_seed.assert_called_once_with(db, seed_config)


def test_cmd_bootstrap_skips_seed_when_data_exists(dev_limits) -> None:
    from simulator.config import SeedConfig

    db = MagicMock()
    db.fetch_one.side_effect = [(True,), (12,)]
    seed_config = SeedConfig.from_env(dev_limits)

    with patch("main.cmd_schema") as cmd_schema, patch("main.cmd_seed") as cmd_seed:
        cmd_bootstrap(db, seed_config)

    cmd_schema.assert_not_called()
    cmd_seed.assert_not_called()


def test_cmd_simulate_passes_duration_to_runner(dev_limits) -> None:
    from simulator.config import SimulationConfig

    sim_config = SimulationConfig.from_env(dev_limits)
    db = MagicMock()

    with patch("main.Simulator") as simulator_cls:
        simulator = simulator_cls.return_value

        cmd_simulate(db, sim_config, duration_seconds=600)

    simulator_cls.assert_called_once_with(db, sim_config)
    simulator.run.assert_called_once_with(duration_seconds=600)


def test_parser_rejects_unknown_duration_flag_for_non_integer() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["simulate", "--duration-seconds", "ten"])
