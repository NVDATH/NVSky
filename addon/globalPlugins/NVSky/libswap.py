"""
Developer-only: swaps a library upgrade prepared by Diagnostics (lib.new) into lib at NVDA start.
Left out of release builds (see excludedFiles in buildVars.py); __init__.py imports it inside try/except ImportError.
Must use the standard library only: it runs before lib is on sys.path.
"""
import os
import shutil


def apply_pending(addonDir, libDir):
    newDir = os.path.join(addonDir, "lib.new")
    if not os.path.isdir(newDir):
        return
    oldDir = os.path.join(addonDir, "lib.old")
    shutil.rmtree(oldDir, ignore_errors=True)
    os.makedirs(oldDir, exist_ok=True)

    def remove(path):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)

    swapped = []
    try:
        for name in os.listdir(newDir):
            target = os.path.join(libDir, name)
            if os.path.exists(target):
                shutil.move(target, os.path.join(oldDir, name))
            swapped.append(name)
            shutil.move(os.path.join(newDir, name), target)
    except Exception:
        for name in swapped:
            saved = os.path.join(oldDir, name)
            if os.path.exists(saved):
                remove(os.path.join(libDir, name))
                shutil.move(saved, os.path.join(libDir, name))
        shutil.rmtree(newDir, ignore_errors=True)
        import logHandler
        logHandler.log.error("NVSky: the prepared library upgrade could not be applied; the old lib was restored")
        return
    shutil.rmtree(oldDir, ignore_errors=True)
    shutil.rmtree(newDir, ignore_errors=True)
