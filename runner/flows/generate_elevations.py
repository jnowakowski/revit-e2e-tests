"""Flow: Graftd -> Elevations -> Generate Elevations"""

from runner.flows.common import run_graftd_command


def run(app, main_win, timeout=120, screenshots_dir=None):
    return run_graftd_command(
        app, main_win,
        panel_auto_id="CustomCtrl_%Graftd%Elevations",
        cmd_auto_id="GenerateElevationsCmd",
        result_title_match="GenerateElevations",
        timeout=timeout,
        screenshots_dir=screenshots_dir,
    )
