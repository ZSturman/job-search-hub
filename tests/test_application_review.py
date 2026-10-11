import pytest

from job_search_hub.cli import parser


def test_cli_exposes_user_tasks_without_writing_or_submission_commands():
    cli = parser()
    help_text = cli.format_help()
    for command in ("setup", "run", "jobs", "search", "resumes", "answers"):
        assert command in help_text
    for retired in ("ui", "resume-plan", "submit", "cover-letter", "tick"):
        with pytest.raises(SystemExit):
            cli.parse_args([retired])
    args = cli.parse_args(["jobs", "review", "application-id"])
    assert args.command == "jobs" and args.action == "review"
