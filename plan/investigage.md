import sys
import os
import json
import inspect
import threading
import importlib
import importlib.metadata
from datetime import datetime

import wx
import ui
import globalPluginHandler
from scriptHandler import script


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

    @script(
        description="Dump NVSky atproto/Pydantic diagnostic information",
        gesture="kb:NVDA+control+shift+d",
    )
    def script_dumpFeedApi(self, gesture):
        ui.message("Collecting atproto/Pydantic diagnostic information...")

        def worker():
            try:
                result = self._collectDiagnostics()

                outDir = os.path.join(
                    CURRENT_DIR,
                    "NVSky",
                    "debug_dumps",
                )
                os.makedirs(outDir, exist_ok=True)

                filename = (
                    f"atproto_pydantic_diagnostics_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                )

                path = os.path.join(outDir, filename)

                with open(path, "w", encoding="utf-8") as f:
                    json.dump(
                        result,
                        f,
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    )

                error = None

            except Exception as e:
                error = f"{type(e).__name__}: {e}"

            wx.CallAfter(self._onDone, error)

        threading.Thread(
            target=worker,
            daemon=True,
            name="NVSky-Diagnostics",
        ).start()

    def _collectDiagnostics(self):
        result = {
            "timestamp": datetime.now().isoformat(),
            "python": {},
            "modules": {},
            "packages": {},
            "models": {},
            "serialization": {},
            "source_locations": {},
        }

        # ==========================================================
        # Python runtime
        # ==========================================================

        result["python"] = {
            "version": sys.version,
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": getattr(sys, "base_prefix", None),
            "platform": sys.platform,
            "sys_path_first_20": sys.path[:20],
        }

        # ==========================================================
        # Import modules
        # ==========================================================

        modules_to_check = [
            "atproto",
            "atproto_client",
            "pydantic",
            "pydantic_core",
        ]

        imported = {}

        for name in modules_to_check:
            try:
                module = importlib.import_module(name)

                imported[name] = module

                result["modules"][name] = {
                    "imported": True,
                    "file": getattr(module, "__file__", None),
                    "package": getattr(module, "__package__", None),
                    "version": getattr(module, "__version__", None),
                }

            except Exception as e:
                result["modules"][name] = {
                    "imported": False,
                    "error": f"{type(e).__name__}: {e}",
                }

        # ==========================================================
        # Package metadata
        # ==========================================================

        package_names = [
            "atproto",
            "atproto-client",
            "pydantic",
            "pydantic-core",
            "typing-extensions",
            "httpx",
            "httpcore",
            "cryptography",
            "dnspython",
        ]

        for package_name in package_names:
            try:
                dist = importlib.metadata.distribution(package_name)

                requires = dist.requires or []

                result["packages"][package_name] = {
                    "version": dist.version,
                    "name": dist.metadata.get("Name"),
                    "requires_python": dist.metadata.get(
                        "Requires-Python"
                    ),
                    "requires": list(requires),
                    "location": str(dist.locate_file("")),
                    "files_count": (
                        len(list(dist.files))
                        if dist.files
                        else 0
                    ),
                }

            except Exception as e:
                result["packages"][package_name] = {
                    "available": False,
                    "error": f"{type(e).__name__}: {e}",
                }

        # ==========================================================
        # Search local lib for dist-info / egg-info
        # ==========================================================

        local_lib = None

        for path in sys.path:
            if path and os.path.isdir(path):
                if os.path.isdir(os.path.join(path, "atproto")):
                    local_lib = path
                    break

        if local_lib:
            metadata_dirs = []

            try:
                for name in os.listdir(local_lib):
                    lower = name.lower()

                    if (
                        "atproto" in lower
                        or "pydantic" in lower
                    ) and (
                        lower.endswith(".dist-info")
                        or lower.endswith(".egg-info")
                    ):
                        metadata_dirs.append(name)

            except Exception:
                pass

            result["packages"]["local_metadata"] = {
                "lib_path": local_lib,
                "metadata_dirs": sorted(metadata_dirs),
            }

        # ==========================================================
        # Model classes
        # ==========================================================

        model_names = [
            "ContentLabelPref",
            "InterestsPref",
            "BskyAppStatePref",
            "SavedFeedsPrefV2",
            "SavedFeedsPref",
            "SavedFeed",
        ]

        model_modules = [
            "atproto_client.models.app.bsky.actor.defs",
            "atproto_client.models.app.bsky.actor",
            "atproto.models.app.bsky.actor.defs",
            "atproto.models.app.bsky.actor",
        ]

        found_models = {}

        for module_name in model_modules:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue

            for model_name in model_names:
                if hasattr(module, model_name):
                    found_models[model_name] = getattr(
                        module,
                        model_name,
                    )

        for model_name in model_names:
            model = found_models.get(model_name)

            if model is None:
                result["models"][model_name] = {
                    "found": False,
                }
                continue

            info = {
                "found": True,
                "module": getattr(model, "__module__", None),
                "qualname": getattr(model, "__qualname__", None),
                "file": None,
                "fields": {},
            }

            try:
                info["file"] = inspect.getsourcefile(model)
            except Exception:
                pass

            # ------------------------------------------------------
            # Pydantic model fields
            # ------------------------------------------------------

            try:
                fields = getattr(model, "model_fields", {})

                for field_name, field_info in fields.items():

                    field_data = {
                        "field_name": field_name,
                        "type": str(
                            getattr(
                                field_info,
                                "annotation",
                                None,
                            )
                        ),
                        "default": repr(
                            getattr(
                                field_info,
                                "default",
                                None,
                            )
                        ),
                        "default_type": type(
                            getattr(
                                field_info,
                                "default",
                                None)
                            ).__name__,
                        "alias": getattr(
                            field_info,
                            "alias",
                            None,
                        ),
                        "alias_priority": getattr(
                            field_info,
                            "alias_priority",
                            None,
                        ),
                        "frozen": getattr(
                            field_info,
                            "frozen",
                            None,
                        ),
                        "field_info_type": type(
                            field_info
                        ).__name__,
                    }

                    # Specifically inspect py_type
                    try:
                        class_default = getattr(
                            model,
                            field_name,
                        )

                        field_data["class_attribute"] = repr(
                            class_default
                        )

                        field_data["class_attribute_type"] = (
                            type(class_default).__name__
                        )

                    except Exception as e:
                        field_data["class_attribute_error"] = (
                            f"{type(e).__name__}: {e}"
                        )

                    info["fields"][field_name] = field_data

            except Exception as e:
                info["fields_error"] = (
                    f"{type(e).__name__}: {e}"
                )

            # ------------------------------------------------------
            # Source excerpt
            # ------------------------------------------------------

            try:
                source = inspect.getsource(model)

                # Limit source so the diagnostic file stays small.
                if len(source) > 12000:
                    source = source[:12000] + (
                        "\n\n[TRUNCATED]\n"
                    )

                info["source"] = source

            except Exception as e:
                info["source_error"] = (
                    f"{type(e).__name__}: {e}"
                )

            result["models"][model_name] = info

        # ==========================================================
        # Serialization-related functions
        # ==========================================================

        serialization_targets = [
            (
                "atproto_client.models.utils",
                "get_model_as_json",
            ),
            (
                "atproto_client.client.base",
                "_handle_kwargs",
            ),
        ]

        for module_name, function_name in serialization_targets:

            key = f"{module_name}.{function_name}"

            try:
                module = importlib.import_module(
                    module_name
                )

                obj = getattr(
                    module,
                    function_name,
                )

                entry = {
                    "found": True,
                    "file": None,
                    "signature": None,
                    "source": None,
                }

                try:
                    entry["file"] = inspect.getsourcefile(obj)
                except Exception:
                    pass

                try:
                    entry["signature"] = str(
                        inspect.signature(obj)
                    )
                except Exception:
                    pass

                try:
                    source = inspect.getsource(obj)

                    if len(source) > 12000:
                        source = source[:12000] + (
                            "\n\n[TRUNCATED]\n"
                        )

                    entry["source"] = source

                except Exception as e:
                    entry["source_error"] = (
                        f"{type(e).__name__}: {e}"
                    )

                result["serialization"][key] = entry

            except Exception as e:
                result["serialization"][key] = {
                    "found": False,
                    "error": f"{type(e).__name__}: {e}",
                }

        # ==========================================================
        # Actor namespace methods
        # ==========================================================

        try:
            from NVSky import client as nvskyClient

            atproto_client = (
                nvskyClient.get_client_for_active_account()
            )

            actor = atproto_client.app.bsky.actor

            result["serialization"][
                "actor.put_preferences"
            ] = self._inspect_object(
                getattr(actor, "put_preferences", None)
            )

            result["serialization"][
                "actor.get_preferences"
            ] = self._inspect_object(
                getattr(actor, "get_preferences", None)
            )

        except Exception as e:
            result["serialization"]["actor_namespace_error"] = (
                f"{type(e).__name__}: {e}"
            )

        # ==========================================================
        # Direct FieldInfo diagnostic
        # ==========================================================

        try:
            from pydantic.fields import FieldInfo

            result["serialization"]["FieldInfo"] = {
                "module": FieldInfo.__module__,
                "qualname": FieldInfo.__qualname__,
                "file": inspect.getsourcefile(FieldInfo),
                "repr": repr(FieldInfo),
            }

        except Exception as e:
            result["serialization"]["FieldInfo"] = {
                "error": f"{type(e).__name__}: {e}",
            }

        return result

    def _inspect_object(self, obj):
        if obj is None:
            return {
                "found": False,
            }

        result = {
            "found": True,
            "type": type(obj).__name__,
            "module": type(obj).__module__,
            "repr": repr(obj),
        }

        try:
            result["signature"] = str(
                inspect.signature(obj)
            )
        except Exception:
            pass

        try:
            result["file"] = inspect.getsourcefile(obj)
        except Exception:
            pass

        try:
            source = inspect.getsource(obj)

            if len(source) > 12000:
                source = source[:12000] + (
                    "\n\n[TRUNCATED]\n"
                )

            result["source"] = source

        except Exception as e:
            result["source_error"] = (
                f"{type(e).__name__}: {e}"
            )

        return result

    def _onDone(self, error):
        if error:
            ui.message(
                f"Diagnostic dump failed: {error}"
            )
        else:
            ui.message(
                "Diagnostic dump complete. "
                "Check NVSky debug_dumps folder."
            )