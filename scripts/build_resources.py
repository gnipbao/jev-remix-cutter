"""Copy the editable decision resources into wheels without changing their bytes."""
from pathlib import Path

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def resource_files(self):
        root = Path(__file__).resolve().parents[1]
        return root, [root / "thresholds.toml", *[
            path for folder, pattern in (("questions", "*.yaml"), ("packs/tracks", "*.json"),
                ("packs/formats", "*.json"), ("packs/platform-profiles", "*.toml"),
                ("assets", "*.html"), ("assets", "*.txt"))
            for path in sorted((root / folder).glob(pattern))]]

    def run(self):
        super().run()
        root, resources = self.resource_files()
        for source in resources:
            target = Path(self.build_lib) / "jev_remix" / "_data" / source.relative_to(root)
            self.mkpath(str(target.parent))
            self.copy_file(str(source), str(target))

    def get_outputs(self, include_bytecode=1):
        root, resources = self.resource_files()
        generated = [str(Path(self.build_lib) / "jev_remix" / "_data" / source.relative_to(root))
                     for source in resources]
        return super().get_outputs(include_bytecode) + generated
