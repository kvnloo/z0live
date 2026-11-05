"""GPT role bindings run through the fx CLI, never inside Hermes (owner rule 2026-11-04)."""

import json
import os
import re
import shutil
import signal
import subprocess
import time

HARNESSES = {"hermes", "fx", "pi", "agy", "local"}
GPT = re.compile(r"^(gpt-|o[0-9]|codex)", re.I)


def is_gpt(binding):
    return binding.get("provider") in {"openai", "openai-codex"} or bool(
        GPT.match(binding.get("model", ""))
    )


def harness(binding):
    return binding.get("harness", "hermes")


def hermes_binding(binding):
    """The provider/model pair Hermes may run; refuses GPT and non-Hermes harnesses."""
    if harness(binding) != "hermes" or is_gpt(binding):
        raise RuntimeError(
            "binding %s/%s must not run inside Hermes"
            % (binding.get("provider"), binding.get("model"))
        )
    return {"provider": binding["provider"], "model": binding["model"]}


def fx_alive(run_id):
    try:
        os.kill(int(run_id.split(":", 1)[1]), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


class FxRun:
    """One `fx ask` child: explicit model and effort, nice 10, hard timeout, killable."""

    def __init__(self, binding, prompt, workdir, seconds):
        exe = shutil.which(os.environ.get("Z0LIVE_FX", "fx"))
        if not exe:
            raise FileNotFoundError("fx is not installed; GPT roles cannot run")
        self.model = binding["model"]
        self.argv = [
            "nice", "-n", "10", "timeout", "-k", "5", str(int(seconds) + 5), exe,
            "ask", "--json", "--no-save", "--model", binding["model"],
            "--effort", binding["effort"], "--", prompt,
        ]
        self.proc = subprocess.Popen(
            self.argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.run_id = "fx:" + str(self.proc.pid)

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def wait(self, cancelled, deadline):
        """Returns (status, output, usage); cancel/deadline kill the process group."""
        status = None
        while self.proc.poll() is None:
            if cancelled():
                status = "cancelled"
            elif time.monotonic() > deadline:
                status = "timeout"
            if status:
                self.stop()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait()
                return status, "", None
            time.sleep(0.2)
        out, err = self.proc.communicate()
        try:
            result = json.loads(out)
        except ValueError:
            return "failed", "fx exit %s: %s" % (self.proc.returncode, err[-300:]), None
        # The served model must be the bound one: no silent substitution.
        if self.proc.returncode or result.get("exit_code") or result.get("model") != self.model:
            return "failed", "fx exit %s model %s" % (self.proc.returncode, result.get("model")), result.get("usage")
        return "completed", result.get("final_output") or result.get("output") or "", result.get("usage")
