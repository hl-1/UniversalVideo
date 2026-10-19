import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core import paths
from backend.infrastructure import runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(runtime.os.environ, {
            "VIDEODREAM_FFMPEG_DIR": "", "VIDEODREAM_NODE_EXE": "", "VIDEODREAM_BROWSER_EXE": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        lookup = patch.object(runtime.shutil, "which", return_value=None)
        self.which = lookup.start()
        self.addCleanup(lookup.stop)

    def test_native_executable_names(self):
        for platform, suffix in (("win32", ".exe"), ("darwin", ""), ("linux", "")):
            with self.subTest(platform=platform), patch.object(runtime.sys, "platform", platform):
                self.assertEqual(runtime.executable_name("ffmpeg"), "ffmpeg" + suffix)
                self.assertEqual(runtime.executable_name("ffprobe"), "ffprobe" + suffix)

    def test_configured_ffmpeg_directory_takes_precedence_over_path(self):
        with patch.dict(runtime.os.environ, {"VIDEODREAM_FFMPEG_DIR": "custom/tools"}):
            self.assertEqual(runtime.ffmpeg_directory(), Path("custom/tools"))
        self.which.assert_not_called()

    def test_ffmpeg_uses_path_directory_without_resolving_symlink(self):
        self.which.side_effect = lambda name: "/custom/bin/ffmpeg" if name == "ffmpeg" else None
        self.assertEqual(runtime.ffmpeg_directory(), Path("/custom/bin"))

    def test_ffprobe_can_locate_media_directory_without_ffmpeg(self):
        self.which.side_effect = lambda name: "/custom/bin/ffprobe" if name == "ffprobe" else None
        self.assertEqual(runtime.ffmpeg_directory(), Path("/custom/bin"))

    def test_windows_retains_legacy_ffmpeg_directory(self):
        with patch.object(runtime.sys, "platform", "win32"):
            self.assertEqual(runtime.ffmpeg_directory(), Path(r"C:\softWare\environment\ffmpeg"))

    def test_macos_checks_apple_silicon_and_intel_homebrew_locations(self):
        for directory in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            with self.subTest(directory=directory), patch.object(runtime.sys, "platform", "darwin"), \
                    patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == directory / "ffmpeg"):
                self.assertEqual(runtime.ffmpeg_directory(), directory)

    def test_node_prefers_current_path(self):
        self.which.return_value = "/custom/node-v22/bin/node"
        self.assertEqual(runtime.node_executable(), Path("/custom/node-v22/bin/node"))

    def test_windows_retains_legacy_node_location(self):
        legacy = Path(r"C:\softWare\environment\nodejs\node.exe")
        with patch.object(runtime.sys, "platform", "win32"), \
                patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == legacy):
            self.assertEqual(runtime.node_executable(), legacy)

    def test_configured_node_and_browser_take_precedence(self):
        with patch.dict(runtime.os.environ, {"VIDEODREAM_NODE_EXE": "custom/node", "VIDEODREAM_BROWSER_EXE": "custom/browser"}):
            self.assertEqual(runtime.node_executable(), Path("custom/node"))
            self.assertEqual(runtime.browser_executable(), Path("custom/browser"))
        self.which.assert_not_called()

    def test_macos_reuses_installed_chrome_or_edge(self):
        for bundle, executable in (("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge")):
            browser = Path("/Applications") / f"{bundle}.app/Contents/MacOS/{executable}"
            with self.subTest(browser=browser), patch.object(runtime.sys, "platform", "darwin"), \
                    patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == browser):
                self.assertEqual(runtime.browser_executable(), browser)

    def test_windows_uses_current_users_browser_directory(self):
        with tempfile.TemporaryDirectory() as working:
            local = Path(working)
            chrome = local / "Google/Chrome/Application/chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            with patch.object(runtime.sys, "platform", "win32"), patch.dict(runtime.os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertEqual(runtime.browser_executable(), chrome)

    def test_browser_from_path_is_supported(self):
        self.which.side_effect = lambda name: "/custom/bin/chromium" if name == "chromium" else None
        with patch.object(Path, "is_file", return_value=False):
            self.assertEqual(runtime.browser_executable(), Path("/custom/bin/chromium"))

    def test_missing_browser_uses_playwright_default(self):
        with patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(runtime.browser_executable())


if __name__ == "__main__":
    unittest.main()
