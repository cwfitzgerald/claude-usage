from click.testing import CliRunner

from claude_usage.cli import cli


def test_click_cli_exposes_server_and_report_options() -> None:
    runner = CliRunner()

    root = runner.invoke(cli, ["--help"])
    assert root.exit_code == 0
    assert "serve" in root.output
    assert "report" in root.output

    serve = runner.invoke(cli, ["serve", "--help"])
    assert serve.exit_code == 0
    assert "--open / --no-open" in serve.output
    assert "--projects-dir" in serve.output

    report = runner.invoke(cli, ["report", "--help"])
    assert report.exit_code == 0
    assert "--json" in report.output
    assert "--since WHEN" in report.output
