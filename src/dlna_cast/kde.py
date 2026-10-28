import json
import shutil
import subprocess
import time

from dlna_cast.log import log


def arrange_outputs():
    """--extend on KDE: KWin may switch the laptop screen off when the virtual monitor appears
    (it applies a stored layout). Keeps real screens on and puts the virtual monitor to their right."""
    if not shutil.which("kscreen-doctor"):  # not KDE: leave the layout to the desktop
        return
    for _ in range(10):  # the virtual output may not exist right after Start
        try:
            outs = json.loads(subprocess.run(["kscreen-doctor", "-j"], check=False, capture_output=True,
                                             text=True, timeout=5).stdout)["outputs"]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            log("Could not read the screen layout:", e)
            return
        virt = [o for o in outs if o["name"].startswith("Virtual-")]
        real = [o for o in outs if o["connected"] and not o["name"].startswith("Virtual-")]
        if virt and real:
            break
        time.sleep(0.5)
    else:
        log("Virtual monitor not found, screen layout left unchanged.")
        return
    right = max(o["pos"]["x"] + round(o["size"]["width"] / (o["scale"] or 1)) for o in real)
    # Names may contain dots (Virtual-...org.kde.konsole), so kscreen-doctor gets numeric ids
    cmd = [f"output.{o['id']}.enable" for o in real if not o["enabled"]]
    cmd += [f"output.{o['id']}.position.{right},0" for o in virt]
    r = subprocess.run(["kscreen-doctor", *cmd], check=False, capture_output=True, text=True)
    log("Virtual monitor placed to the right of the screen." if r.returncode == 0
        else f"Could not arrange screens: {(r.stdout + r.stderr).strip()[:300]}")
