import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/dispatch/scripts/dispatch.py"
spec = importlib.util.spec_from_file_location("dispatch_deepseek", SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DeepSeekTests(unittest.TestCase):
    def test_native_launcher_uses_explicit_script_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "path with spaces" / "dsh-tui.js"
            script.parent.mkdir()
            script.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {"PERSONAL_DISPATCH_DSH_TUI": str(script)}), patch.object(d, "executable", return_value="node.exe"):
                self.assertEqual(d.deepseek_tui_argv(), ["node.exe", str(script.resolve())])

    def test_missing_launcher_is_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PERSONAL_DISPATCH_DSH_TUI": str(Path(tmp) / "missing.js")}):
            with self.assertRaisesRegex(d.DispatchError, "dsh-tui"):
                d.deepseek_tui_argv()

    def test_prompt_and_workspace_are_separate_arguments(self):
        with patch.object(d, "deepseek_tui_argv", return_value=["node.exe", "dsh-tui.js"]):
            self.assertEqual(d.agent_argv("deepseek", Path("task"), "中文提示 & literal", Path("work space")),
                             ["node.exe", "dsh-tui.js", "--backend", "dsh", "work space", "--", "中文提示 & literal"])


if __name__ == "__main__":
    unittest.main()
