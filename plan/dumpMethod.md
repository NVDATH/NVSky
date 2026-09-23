import sys
import os
import json
import threading
from datetime import datetime

import wx
import ui
import globalPluginHandler
from scriptHandler import script

# ตำแหน่งโฟลเดอร์ globalPlugins
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    @script(
        description="Dump ALL available Bluesky SDK methods recursively",
        gesture="kb:NVDA+control+shift+d",
    )
    def script_dumpFeedApi(self, gesture):
        try:
            from NVSky import client as nvskyClient
        except Exception as e:
            ui.message(f"Could not import NVSky: {e}")
            return

        ui.message("Dumping ALL Bluesky API methods recursively, please wait...")

        def worker():
            try:
                atprotoClient = nvskyClient.get_client_for_active_account()
                
                # BUG FIX: the old inspect_is_namespace() checked
                # hasattr(item, "__dir__"), which is True for almost
                # everything in Python INCLUDING bound methods
                # themselves (functions inherit __dir__ from object) --
                # so "callable(attr) and not inspect_is_namespace(attr)"
                # was almost never True, and every real method got
                # miscategorized as a sub-namespace to recurse into
                # instead. Harmless for most SDK methods (their internal
                # attributes are all dunder-prefixed and get filtered
                # by the name.startswith("_") check, so the recursion
                # just dead-ends and gets silently dropped) but VERY
                # likely the cause of a past dump run coming back
                # chat-only: app.bsky.* is dramatically bigger than
                # chat.bsky.* (feed/graph/actor/notification/bookmark/
                # video/unspecced all live under it), and recursing this
                # way into every single one of its methods-as-fake-
                # namespaces is exactly the kind of thing that can throw
                # or hang long enough to get silently swallowed by the
                # outer "except Exception: continue" in script_dumpFeedApi,
                # while the much smaller chat.bsky tree finished fine.
                # Fixed by checking callable() FIRST and treating any
                # callable as a leaf method immediately -- no longer
                # tries to recurse into method internals at all.
                def explore_namespace(obj, prefix=""):
                    methods = []
                    sub_namespaces = {}

                    for name in dir(obj):
                        if name.startswith("_"):
                            continue
                        try:
                            attr = getattr(obj, name)
                        except Exception:
                            continue

                        if callable(attr):
                            methods.append(name)
                            continue

                        if hasattr(attr, "__dir__") and not isinstance(attr, (str, int, float, bool, list, dict, tuple, type(None))):
                            sub_name = f"{prefix}.{name}" if prefix else name
                            if name not in ["client", "_client", "session"]:
                                sub_res = explore_namespace(attr, sub_name)
                                if sub_res["methods"] or sub_res["namespaces"]:
                                    sub_namespaces[name] = sub_res

                    return {
                        "methods": sorted(methods),
                        "namespaces": sub_namespaces
                    }

                # เริ่มต้นกวาดจากจุดราก (Root) ของ Client ทั้งหมด
                result = {}
                root_namespaces = [n for n in dir(atprotoClient) if not n.startswith("_")]
                
                for ns_name in root_namespaces:
                    try:
                        ns_obj = getattr(atprotoClient, ns_name)
                        if hasattr(ns_obj, "__dir__"):
                            result[ns_name] = explore_namespace(ns_obj, ns_name)
                    except Exception:
                        continue

                outDir = os.path.join(CURRENT_DIR, "NVSky", "debug_dumps")
                os.makedirs(outDir, exist_ok=True)
                filename = f"all_bluesky_methods_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                
                with open(os.path.join(outDir, filename), "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                error = None
            except Exception as e:
                error = str(e)
            wx.CallAfter(self._onDone, error)

        threading.Thread(target=worker, daemon=True).start()

    def _onDone(self, error):
        if error:
            ui.message(f"Dump failed: {error}")
        else:
            ui.message("Done. Check NVSky's debug_dumps folder for the complete method map.")