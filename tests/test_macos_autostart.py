from scripts.macos_autostart import launch_agent


def test_launch_agent_runs_on_saturday_and_leaves_server_alive():
    agent = launch_agent("/tmp/horse app", "com.example.horse")

    assert agent["StartCalendarInterval"] == {"Weekday": 6, "Hour": 9, "Minute": 40}
    assert agent["AbandonProcessGroup"] is True
    assert agent["ProgramArguments"] == ["/tmp/horse app/scripts/open_keiba.sh"]
