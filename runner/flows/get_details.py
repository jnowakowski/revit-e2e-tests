"""Flow: Graftd -> Details -> Get Details"""

from runner.flows.common import run_graftd_command


def run(app, main_win, timeout=120, screenshots_dir=None):
    return run_graftd_command(
        app, main_win,
        panel_auto_id="CustomCtrl_%Graftd%Details",
        cmd_auto_id="GetDetailsCmd",
        result_title_match="GetDetails",
        timeout=timeout,
        screenshots_dir=screenshots_dir,
    )
